"""Minimal, fast Modbus TCP server (slave) backed by simulated tag sets.

Supported function codes: 1, 2, 3, 4, 5, 6, 15, 16.
Each tag group is mapped onto a contiguous address range of one data area.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import sys
from array import array
from dataclasses import dataclass

from .tags import TagSet, TagSpec

log = logging.getLogger("modbus")

AREAS = ("coils", "discrete_inputs", "holding_registers", "input_registers")
AREA_SIZE = 65536
REG_WIDTH = {"int16": 1, "uint16": 1, "bool": 1, "int32": 2, "uint32": 2, "float32": 2, "float64": 4}
STRUCT_CODE = {"int16": "h", "uint16": "H", "int32": "i", "uint32": "I", "float32": "f", "float64": "d", "bool": "H"}

ILLEGAL_FUNCTION, ILLEGAL_ADDRESS, ILLEGAL_VALUE = 1, 2, 3


@dataclass
class ModbusTagGroup:
    tagset: TagSet
    area: str
    start: int
    width: int  # registers (or bits) per tag

    @property
    def end(self) -> int:
        return self.start + self.width * self.tagset.spec.count


class ModbusSimServer:
    def __init__(self, name: str, host: str, port: int, update_interval_ms: int,
                 tag_groups: list[dict], unit_id: int = 1, word_order: str = "big"):
        self.name, self.host, self.port = name, host, port
        self.interval = update_interval_ms / 1000
        self.unit_id = unit_id
        self.ready = asyncio.Event()
        self.word_swap = word_order == "little"
        self.regs = {"holding_registers": array("H", [0]) * AREA_SIZE,
                     "input_registers": array("H", [0]) * AREA_SIZE}
        self.bits = {"coils": bytearray(AREA_SIZE), "discrete_inputs": bytearray(AREA_SIZE)}
        self.groups: list[ModbusTagGroup] = []
        next_free = {a: 0 for a in AREAS}
        for g in tag_groups:
            g = dict(g)
            area = g.pop("area", "holding_registers")
            if area not in AREAS:
                raise ValueError(f"{name}: area must be one of {AREAS}")
            start = g.pop("start_address", None)
            spec = TagSpec(**g)
            if area in ("coils", "discrete_inputs"):
                spec.data_type = "bool"
                width = 1
            else:
                width = REG_WIDTH[spec.data_type]
            start = next_free[area] if start is None else int(start)
            grp = ModbusTagGroup(TagSet(spec), area, start, width)
            if grp.end > AREA_SIZE:
                raise ValueError(f"{name}/{spec.name}: needs addresses {start}..{grp.end - 1}, "
                                 f"exceeds the 65536-address limit of {area}")
            for other in self.groups:
                if other.area == area and start < other.end and other.start < grp.end:
                    raise ValueError(f"{name}/{spec.name}: overlaps {other.tagset.spec.name} in {area}")
            next_free[area] = max(next_free[area], grp.end)
            self.groups.append(grp)
        self._write_all()

    # ---------- value encoding ----------
    def _write_group(self, g: ModbusTagGroup) -> None:
        values = g.tagset.values
        if g.area in self.bits:
            self.bits[g.area][g.start:g.end] = bytes(1 if v else 0 for v in values)
            return
        dt = g.tagset.spec.data_type
        if dt == "bool":
            values = [1 if v else 0 for v in values]
        raw = struct.pack(f">{len(values)}{STRUCT_CODE[dt]}", *values)
        words = array("H", raw)
        if sys.byteorder == "little":
            words.byteswap()
        if self.word_swap and g.width > 1:
            w = g.width
            for k in range(w // 2):
                a, b = words[k::w], words[w - 1 - k::w]
                words[k::w], words[w - 1 - k::w] = b, a
        self.regs[g.area][g.start:g.end] = words

    def _write_all(self) -> None:
        for g in self.groups:
            self._write_group(g)

    async def _update_loop(self) -> None:
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        while True:
            for g in self.groups:
                g.tagset.tick()
            self._write_all()
            next_t += self.interval
            delay = next_t - loop.time()
            if delay < 0:
                log.warning("%s: update took longer than interval (%.0f ms behind)", self.name, -delay * 1000)
                next_t = loop.time()
                delay = 0
            await asyncio.sleep(delay)

    # ---------- protocol ----------
    def _handle_pdu(self, pdu: bytes) -> bytes:
        fc = pdu[0]
        try:
            if fc in (1, 2, 3, 4):
                addr, qty = struct.unpack(">HH", pdu[1:5])
                limit = 2000 if fc in (1, 2) else 125
                if not 1 <= qty <= limit:
                    return self._exc(fc, ILLEGAL_VALUE)
                if addr + qty > AREA_SIZE:
                    return self._exc(fc, ILLEGAL_ADDRESS)
                if fc in (1, 2):
                    bits = self.bits["coils" if fc == 1 else "discrete_inputs"][addr:addr + qty]
                    out = bytearray((qty + 7) // 8)
                    for i, b in enumerate(bits):
                        if b:
                            out[i >> 3] |= 1 << (i & 7)
                    return bytes([fc, len(out)]) + out
                words = self.regs["holding_registers" if fc == 3 else "input_registers"][addr:addr + qty]
                if sys.byteorder == "little":
                    words.byteswap()
                return bytes([fc, qty * 2]) + words.tobytes()
            if fc == 5:
                addr, val = struct.unpack(">HH", pdu[1:5])
                if val not in (0x0000, 0xFF00):
                    return self._exc(fc, ILLEGAL_VALUE)
                self.bits["coils"][addr] = 1 if val else 0
                return pdu[:5]
            if fc == 6:
                addr, val = struct.unpack(">HH", pdu[1:5])
                self.regs["holding_registers"][addr] = val
                return pdu[:5]
            if fc == 15:
                addr, qty, nbytes = struct.unpack(">HHB", pdu[1:6])
                if addr + qty > AREA_SIZE or not 1 <= qty <= 1968:
                    return self._exc(fc, ILLEGAL_ADDRESS)
                data = pdu[6:6 + nbytes]
                for i in range(qty):
                    self.bits["coils"][addr + i] = (data[i >> 3] >> (i & 7)) & 1
                return pdu[:5]
            if fc == 16:
                addr, qty, nbytes = struct.unpack(">HHB", pdu[1:6])
                if addr + qty > AREA_SIZE or not 1 <= qty <= 123:
                    return self._exc(fc, ILLEGAL_ADDRESS)
                words = array("H", pdu[6:6 + qty * 2])
                if sys.byteorder == "little":
                    words.byteswap()
                self.regs["holding_registers"][addr:addr + qty] = words
                return pdu[:5]
        except (struct.error, IndexError):
            return self._exc(fc, ILLEGAL_VALUE)
        return self._exc(fc, ILLEGAL_FUNCTION)

    @staticmethod
    def _exc(fc: int, code: int) -> bytes:
        return bytes([fc | 0x80, code])

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("%s: client connected %s", self.name, peer)
        try:
            while True:
                header = await reader.readexactly(7)
                tid, pid, length, uid = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                if pid != 0:
                    continue
                resp = self._handle_pdu(pdu)
                writer.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, uid) + resp)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            log.info("%s: client disconnected %s", self.name, peer)
            writer.close()

    def tag_map(self) -> list[dict]:
        rows = []
        offset = {"coils": 1, "discrete_inputs": 10001, "input_registers": 30001, "holding_registers": 40001}
        for g in self.groups:
            for i, n in enumerate(g.tagset.names()):
                addr = g.start + i * g.width
                rows.append({"server": self.name, "tag": n, "area": g.area, "address": addr,
                             "modicon_address": offset[g.area] + addr if addr < 9999 else "",
                             "registers": g.width, "data_type": g.tagset.spec.data_type,
                             "word_order": "little" if self.word_swap else "big"})
        return rows

    async def run(self) -> None:
        server = await asyncio.start_server(self._client, self.host, self.port)
        total = sum(g.tagset.spec.count for g in self.groups)
        log.info("%s: Modbus TCP listening on %s:%d (%d tags, %d ms)", self.name, self.host, self.port,
                 total, int(self.interval * 1000))
        for g in self.groups:
            log.info("%s:   %s x%d %s %s @ %d..%d", self.name, g.tagset.spec.name, g.tagset.spec.count,
                     g.tagset.spec.data_type, g.area, g.start, g.end - 1)
        self.ready.set()
        async with server:
            await asyncio.gather(server.serve_forever(), self._update_loop())
