"""Minimal, fast asyncio MQTT 3.1.1 publish-only client.

Much faster than paho for bulk publishing because it writes raw frames straight into the
socket buffer. QoS 1 messages get packet ids and the broker's PUBACKs are read and discarded
(fire-and-forget, no redelivery).
"""

from __future__ import annotations

import asyncio
import struct

from .mqtt_broker import CONNACK, PINGREQ, PUBLISH, encode_length, mqtt_str


class MqttError(Exception):
    pass


class MqttPublisher:
    def __init__(self, host: str, port: int, client_id: str, username: str | None = None,
                 password: str | None = None, keepalive: int = 60):
        self.host, self.port, self.client_id = host, port, client_id
        self.username, self.password, self.keepalive = username, password, keepalive
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._pid = 1
        self._tasks: list[asyncio.Task] = []

    async def __aenter__(self) -> "MqttPublisher":
        try:
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        except OSError as e:
            raise MqttError(f"cannot connect to {self.host}:{self.port}: {e}") from e
        flags = 0x02  # clean session
        payload = mqtt_str(self.client_id)
        if self.username is not None:
            flags |= 0x80
            payload += mqtt_str(self.username)
            if self.password is not None:
                flags |= 0x40
                payload += mqtt_str(self.password)
        body = mqtt_str("MQTT") + bytes([4, flags]) + struct.pack(">H", self.keepalive) + payload
        self.writer.write(bytes([0x10]) + encode_length(len(body)) + body)
        await self.writer.drain()
        try:
            resp = await asyncio.wait_for(self.reader.readexactly(4), 10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError) as e:
            raise MqttError(f"no CONNACK from broker: {e}") from e
        if resp[0] >> 4 != CONNACK or resp[3] != 0:
            raise MqttError(f"connection refused by broker (code {resp[3]})")
        self._tasks = [asyncio.create_task(self._drain_incoming()), asyncio.create_task(self._ping())]
        return self

    async def __aexit__(self, *exc) -> None:
        for t in self._tasks:
            t.cancel()
        if self.writer:
            try:
                self.writer.write(b"\xe0\x00")  # DISCONNECT
                self.writer.close()
            except Exception:
                pass

    async def _drain_incoming(self) -> None:
        # discard PUBACK / PINGRESP; EOF means the broker went away
        try:
            while await self.reader.read(65536):
                pass
        except ConnectionError:
            pass
        self.writer.close()

    async def _ping(self) -> None:
        while True:
            await asyncio.sleep(self.keepalive / 2)
            self.writer.write(bytes([PINGREQ << 4, 0]))

    def publish_nowait(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False) -> None:
        if self.writer is None or self.writer.is_closing():
            raise MqttError("not connected")
        if isinstance(payload, str):
            payload = payload.encode()
        body = mqtt_str(topic)
        qos = min(qos, 1)
        if qos:
            body += struct.pack(">H", self._pid)
            self._pid = self._pid % 65535 + 1
        body += payload
        self.writer.write(bytes([(PUBLISH << 4) | (qos << 1) | int(retain)]) + encode_length(len(body)) + body)

    async def flush(self) -> None:
        try:
            await self.writer.drain()
        except ConnectionError as e:
            raise MqttError(str(e)) from e
