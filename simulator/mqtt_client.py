"""Minimal, fast asyncio MQTT 3.1.1 client (publish + subscribe).

Much faster than paho for bulk publishing because it writes raw frames straight into the
socket buffer. QoS 1 publishes get packet ids; the broker's PUBACKs are read and discarded
(fire-and-forget, no redelivery). Incoming QoS 1 messages are acknowledged.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Callable

from .mqtt_broker import CONNACK, PINGREQ, PUBACK, PUBLISH, SUBSCRIBE, UNSUBSCRIBE, encode_length, mqtt_str


class MqttError(Exception):
    pass


class MqttClient:
    def __init__(self, host: str, port: int, client_id: str, username: str | None = None,
                 password: str | None = None, keepalive: int = 60,
                 on_message: Callable[[str, bytes, int, bool], None] | None = None):
        self.host, self.port, self.client_id = host, port, client_id
        self.username, self.password, self.keepalive = username, password, keepalive
        self.on_message = on_message
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._pid = 1
        self._tasks: list[asyncio.Task] = []
        self.closed = asyncio.Event()

    def _next_pid(self) -> bytes:
        pid = struct.pack(">H", self._pid)
        self._pid = self._pid % 65535 + 1
        return pid

    def _send(self, first: int, body: bytes) -> None:
        if self.writer is None or self.writer.is_closing():
            raise MqttError("not connected")
        self.writer.write(bytes([first]) + encode_length(len(body)) + body)

    async def connect(self) -> "MqttClient":
        try:
            self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 10)
        except (OSError, asyncio.TimeoutError) as e:
            raise MqttError(f"cannot connect to {self.host}:{self.port}: {e}") from e
        flags = 0x02  # clean session
        payload = mqtt_str(self.client_id)
        if self.username:
            flags |= 0x80
            payload += mqtt_str(self.username)
            if self.password is not None:
                flags |= 0x40
                payload += mqtt_str(self.password)
        self._send(0x10, mqtt_str("MQTT") + bytes([4, flags]) + struct.pack(">H", self.keepalive) + payload)
        await self.writer.drain()
        try:
            resp = await asyncio.wait_for(self.reader.readexactly(4), 10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError) as e:
            raise MqttError(f"no CONNACK from broker: {e}") from e
        if resp[0] >> 4 != CONNACK or resp[3] != 0:
            raise MqttError(f"connection refused by broker (code {resp[3]})")
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._ping())]
        return self

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.writer and not self.writer.is_closing():
            try:
                self.writer.write(b"\xe0\x00")  # DISCONNECT
                self.writer.close()
            except Exception:
                pass
        self.closed.set()

    async def __aenter__(self) -> "MqttClient":
        return await self.connect()

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _read_loop(self) -> None:
        try:
            while True:
                first = (await self.reader.readexactly(1))[0]
                mult, length = 1, 0
                while True:
                    b = (await self.reader.readexactly(1))[0]
                    length += (b & 0x7F) * mult
                    if not b & 0x80:
                        break
                    mult *= 128
                body = await self.reader.readexactly(length)
                if first >> 4 == PUBLISH:
                    qos, retain = (first >> 1) & 3, bool(first & 1)
                    tlen = struct.unpack(">H", body[:2])[0]
                    topic = body[2:2 + tlen].decode(errors="replace")
                    pos = 2 + tlen
                    if qos:
                        self._send(PUBACK << 4, body[pos:pos + 2])
                        pos += 2
                    if self.on_message:
                        self.on_message(topic, body[pos:], qos, retain)
                # CONNACK/SUBACK/PUBACK/PINGRESP need no handling here
        except (asyncio.IncompleteReadError, ConnectionError, MqttError):
            pass
        finally:
            self.closed.set()
            if self.writer:
                self.writer.close()

    async def _ping(self) -> None:
        while True:
            await asyncio.sleep(self.keepalive / 2)
            self._send(PINGREQ << 4, b"")

    def publish_nowait(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False) -> None:
        if isinstance(payload, str):
            payload = payload.encode()
        qos = min(qos, 1)
        body = mqtt_str(topic) + (self._next_pid() if qos else b"") + payload
        self._send((PUBLISH << 4) | (qos << 1) | int(retain), body)

    async def subscribe(self, topic_filter: str, qos: int = 0) -> None:
        self._send((SUBSCRIBE << 4) | 0x02, self._next_pid() + mqtt_str(topic_filter) + bytes([min(qos, 1)]))
        await self.flush()

    async def unsubscribe(self, topic_filter: str) -> None:
        self._send((UNSUBSCRIBE << 4) | 0x02, self._next_pid() + mqtt_str(topic_filter))
        await self.flush()

    async def flush(self) -> None:
        if self.closed.is_set():
            raise MqttError("connection closed")
        try:
            await self.writer.drain()
        except ConnectionError as e:
            raise MqttError(str(e)) from e
