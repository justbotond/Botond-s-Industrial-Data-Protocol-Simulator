"""Tiny built-in MQTT 3.1.1 broker so the simulator works without an external broker.

Supports CONNECT, PUBLISH (QoS 0/1, retain), SUBSCRIBE/UNSUBSCRIBE with + and # wildcards,
PINGREQ and DISCONNECT. Subscriptions are delivered at QoS 0 or 1 (min of publish/subscribe QoS).
No authentication, no persistence. Good enough for a test bench, not for production.
"""

from __future__ import annotations

import asyncio
import logging
import struct

log = logging.getLogger("mqtt-broker")

CONNECT, CONNACK, PUBLISH, PUBACK = 1, 2, 3, 4
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK = 8, 9, 10, 11
PINGREQ, PINGRESP, DISCONNECT = 12, 13, 14
MAX_BUFFER = 64 * 1024 * 1024


def topic_matches(flt: str, topic: str) -> bool:
    f, t = flt.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(f) == len(t)


def encode_length(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def mqtt_str(s: str) -> bytes:
    b = s.encode()
    return struct.pack(">H", len(b)) + b


class _Session:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.client_id = ""
        self.subs: dict[str, int] = {}
        self.next_pid = 1
        self.alive = True

    def send(self, ptype_flags: int, body: bytes) -> None:
        if self.alive:
            self.writer.write(bytes([ptype_flags]) + encode_length(len(body)) + body)

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool = False) -> None:
        if self.writer.transport.get_write_buffer_size() > MAX_BUFFER:
            return  # slow subscriber: drop instead of growing memory without bound
        body = mqtt_str(topic)
        if qos:
            body += struct.pack(">H", self.next_pid)
            self.next_pid = self.next_pid % 65535 + 1
        self.send((PUBLISH << 4) | (qos << 1) | int(retain), body + payload)


class MqttBroker:
    def __init__(self, host: str = "0.0.0.0", port: int = 1883, name: str = "broker"):
        self.host, self.port, self.name = host, port, name
        self.sessions: set[_Session] = set()
        self.retained: dict[str, tuple[bytes, int]] = {}

    def _route(self, topic: str, payload: bytes, qos: int) -> None:
        for s in list(self.sessions):
            granted = max((q for f, q in s.subs.items() if topic_matches(f, topic)), default=None)
            if granted is not None:
                s.publish(topic, payload, min(qos, granted))

    async def _read_packet(self, reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
        first = (await reader.readexactly(1))[0]
        mult, length = 1, 0
        while True:
            b = (await reader.readexactly(1))[0]
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        return first >> 4, first & 0x0F, await reader.readexactly(length)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        s = _Session(writer)
        try:
            ptype, _, body = await self._read_packet(reader)
            if ptype != CONNECT:
                return
            plen = struct.unpack(">H", body[:2])[0]
            pos = 2 + plen + 4  # protocol name, level, flags, keepalive
            cid_len = struct.unpack(">H", body[pos:pos + 2])[0]
            s.client_id = body[pos + 2:pos + 2 + cid_len].decode(errors="replace")
            s.send(CONNACK << 4, b"\x00\x00")
            self.sessions.add(s)
            log.info("%s: client connected %s", self.name, s.client_id or writer.get_extra_info("peername"))
            while True:
                ptype, flags, body = await self._read_packet(reader)
                if ptype == PUBLISH:
                    qos, retain = (flags >> 1) & 3, flags & 1
                    tlen = struct.unpack(">H", body[:2])[0]
                    topic = body[2:2 + tlen].decode()
                    pos = 2 + tlen
                    if qos:
                        pid = body[pos:pos + 2]
                        pos += 2
                        s.send(PUBACK << 4, pid)  # QoS 2 is downgraded to 1
                    payload = body[pos:]
                    if retain:
                        if payload:
                            self.retained[topic] = (payload, min(qos, 1))
                        else:
                            self.retained.pop(topic, None)
                    self._route(topic, payload, min(qos, 1))
                elif ptype == SUBSCRIBE:
                    pid, pos, codes = body[:2], 2, bytearray()
                    new = []
                    while pos < len(body):
                        n = struct.unpack(">H", body[pos:pos + 2])[0]
                        flt = body[pos + 2:pos + 2 + n].decode()
                        q = min(body[pos + 2 + n] & 3, 1)
                        pos += 3 + n
                        s.subs[flt] = q
                        codes.append(q)
                        new.append((flt, q))
                    s.send((SUBACK << 4), pid + bytes(codes))
                    for flt, q in new:
                        for topic, (payload, rq) in self.retained.items():
                            if topic_matches(flt, topic):
                                s.publish(topic, payload, min(q, rq), retain=True)
                elif ptype == UNSUBSCRIBE:
                    pid, pos = body[:2], 2
                    while pos < len(body):
                        n = struct.unpack(">H", body[pos:pos + 2])[0]
                        s.subs.pop(body[pos + 2:pos + 2 + n].decode(), None)
                        pos += 2 + n
                    s.send(UNSUBACK << 4, pid)
                elif ptype == PINGREQ:
                    s.send(PINGRESP << 4, b"")
                elif ptype == DISCONNECT:
                    break
                # PUBACK from subscribers and anything else is ignored
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, struct.error, UnicodeDecodeError):
            pass
        finally:
            s.alive = False
            self.sessions.discard(s)
            log.info("%s: client disconnected %s", self.name, s.client_id)
            writer.close()

    async def run(self) -> None:
        server = await asyncio.start_server(self._client, self.host, self.port)
        log.info("%s: MQTT broker listening on %s:%d", self.name, self.host, self.port)
        async with server:
            await server.serve_forever()
