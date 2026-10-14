"""Built-in test clients used by the web UI to look at what the simulators (or any other
device on the network) are serving."""

from __future__ import annotations

import asyncio
import json
import struct
import time
from collections import deque

from .modbus_server import REG_WIDTH, STRUCT_CODE
from .mqtt_client import MqttClient, MqttError

# ---------------------------------------------------------------- Modbus


async def modbus_read(host: str, port: int, unit: int, area: str, address: int, count: int,
                      data_type: str = "uint16", word_order: str = "big", timeout: float = 3.0) -> list[dict]:
    """Read `count` values of `data_type` starting at `address` from a Modbus TCP server."""
    if count < 1 or count > 2000:
        raise ValueError("count must be 1..2000")
    bits = area in ("coils", "discrete_inputs")
    fc = {"coils": 1, "discrete_inputs": 2, "holding_registers": 3, "input_registers": 4}[area]
    width = 1 if bits else REG_WIDTH[data_type]
    total = count * width
    if address + total > 65536:
        raise ValueError("address range exceeds 65535")
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    try:
        raw: list[int] = []
        chunk = 2000 if bits else 124 - (124 % width) or width
        tid = 0
        pos = 0
        while pos < total:
            qty = min(chunk, total - pos)
            tid += 1
            writer.write(struct.pack(">HHHBBHH", tid, 0, 6, unit, fc, address + pos, qty))
            await writer.drain()
            hdr = await asyncio.wait_for(reader.readexactly(7), timeout)
            _, _, length, _ = struct.unpack(">HHHB", hdr)
            pdu = await asyncio.wait_for(reader.readexactly(length - 1), timeout)
            if pdu[0] & 0x80:
                codes = {1: "illegal function", 2: "illegal data address", 3: "illegal data value",
                         4: "server device failure", 6: "server busy", 11: "gateway target failed to respond"}
                raise RuntimeError(f"Modbus exception {pdu[1]} ({codes.get(pdu[1], 'unknown')}) "
                                   f"at address {address + pos}")
            data = pdu[2:]
            if bits:
                raw += [(data[i >> 3] >> (i & 7)) & 1 for i in range(qty)]
            else:
                raw += list(struct.unpack(f">{qty}H", data[:qty * 2]))
            pos += qty
    finally:
        writer.close()

    out = []
    for i in range(count):
        addr = address + i * width
        if bits:
            out.append({"address": addr, "raw": [raw[i]], "value": bool(raw[i])})
            continue
        words = raw[i * width:(i + 1) * width]
        ordered = list(reversed(words)) if word_order == "little" else words
        b = struct.pack(f">{width}H", *ordered)
        code = STRUCT_CODE[data_type]
        value = struct.unpack(f">{code}", b)[0] if data_type != "bool" else bool(words[0])
        if isinstance(value, float):
            value = round(value, 6)
        out.append({"address": addr, "raw": words, "value": value})
    return out


# ---------------------------------------------------------------- OPC UA


class OpcUaClientPool:
    """Keeps one connected asyncua client per endpoint."""

    def __init__(self):
        self.clients: dict[str, object] = {}
        self.lock = asyncio.Lock()

    async def get(self, endpoint: str):
        from asyncua import Client
        async with self.lock:
            c = self.clients.get(endpoint)
            if c is not None:
                try:
                    await c.check_connection()
                    return c
                except Exception:
                    await self.drop(endpoint)
            c = Client(endpoint, timeout=10)
            await c.connect()
            self.clients[endpoint] = c
            return c

    async def drop(self, endpoint: str) -> None:
        c = self.clients.pop(endpoint, None)
        if c is not None:
            try:
                await c.disconnect()
            except Exception:
                pass

    async def close_all(self) -> None:
        for ep in list(self.clients):
            await self.drop(ep)

    async def browse(self, endpoint: str, nodeid: str | None, offset: int = 0, limit: int = 200) -> dict:
        from asyncua import ua
        c = await self.get(endpoint)
        node = c.get_node(nodeid) if nodeid else c.nodes.objects
        refs = await node.get_references(refs=ua.ObjectIds.HierarchicalReferences,
                                         direction=ua.BrowseDirection.Forward)
        total = len(refs)
        refs = refs[offset:offset + limit]
        items = [{"node_id": r.NodeId.to_string(), "name": r.BrowseName.Name,
                  "node_class": r.NodeClass.name} for r in refs]
        var_idx = [i for i, it in enumerate(items) if it["node_class"] == "Variable"]
        if var_idx:
            nodes = [c.get_node(items[i]["node_id"]) for i in var_idx]
            dvs = await c.read_attributes(nodes, ua.AttributeIds.Value)
            for i, dv in zip(var_idx, dvs):
                items[i].update(_dv(dv))
        return {"node_id": node.nodeid.to_string(), "total": total, "offset": offset, "items": items}

    async def read(self, endpoint: str, nodeids: list[str]) -> list[dict]:
        from asyncua import ua
        c = await self.get(endpoint)
        dvs = await c.read_attributes([c.get_node(n) for n in nodeids], ua.AttributeIds.Value)
        return [{"node_id": n, **_dv(dv)} for n, dv in zip(nodeids, dvs)]

    async def namespaces(self, endpoint: str) -> list[str]:
        c = await self.get(endpoint)
        return await c.get_namespace_array()


def _dv(dv) -> dict:
    v = dv.Value.Value if dv.Value is not None else None
    if isinstance(v, float):
        v = round(v, 6)
    elif v is not None and not isinstance(v, (int, bool, str)):
        v = str(v)
    return {"value": v, "data_type": dv.Value.VariantType.name if dv.Value is not None else None,
            "status": dv.StatusCode.name if dv.StatusCode is not None else "Good",
            "source_timestamp": dv.SourceTimestamp.isoformat() if dv.SourceTimestamp else None}


# ---------------------------------------------------------------- MQTT


class MqttMonitor:
    """One subscribing MQTT client for the UI: keeps recent messages and the last value per topic."""

    MAX_TOPICS = 5000

    def __init__(self):
        self.client: MqttClient | None = None
        self.host = None
        self.port = None
        self.filters: list[str] = []
        self.recent: deque = deque(maxlen=200)
        self.latest: dict[str, dict] = {}
        self.count = 0
        self.rate = 0.0
        self._rate_t = time.monotonic()
        self._rate_n = 0
        self.error: str | None = None

    def _on_message(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        self.count += 1
        try:
            text = payload.decode()
        except UnicodeDecodeError:
            text = payload.hex()
        msg = {"topic": topic, "payload": text[:2000], "qos": qos, "retain": retain, "time": time.time()}
        self.recent.append(msg)
        if topic in self.latest or len(self.latest) < self.MAX_TOPICS:
            self.latest[topic] = msg

    def connected(self) -> bool:
        return self.client is not None and not self.client.closed.is_set()

    async def connect(self, host: str, port: int, username: str | None = None, password: str | None = None):
        await self.disconnect()
        self.client = MqttClient(host, port, f"bidps-ui-{int(time.time())}", username, password,
                                 on_message=self._on_message)
        await self.client.connect()
        self.host, self.port, self.error = host, port, None
        self.recent.clear()
        self.latest.clear()
        self.count = 0

    async def disconnect(self) -> None:
        if self.client is not None:
            await self.client.close()
        self.client = None
        self.filters = []

    async def subscribe(self, flt: str, qos: int = 0) -> None:
        if not self.connected():
            raise MqttError("not connected")
        await self.client.subscribe(flt, qos)
        if flt not in self.filters:
            self.filters.append(flt)

    async def unsubscribe(self, flt: str) -> None:
        if self.connected():
            await self.client.unsubscribe(flt)
        if flt in self.filters:
            self.filters.remove(flt)

    async def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        if not self.connected():
            raise MqttError("not connected")
        self.client.publish_nowait(topic, payload, qos, retain)
        await self.client.flush()

    def clear(self) -> None:
        self.recent.clear()
        self.latest.clear()
        self.count = 0

    def state(self, topic_filter: str = "", limit: int = 500) -> dict:
        now = time.monotonic()
        if now - self._rate_t >= 1:
            self.rate = (self.count - self._rate_n) / (now - self._rate_t)
            self._rate_t, self._rate_n = now, self.count
        latest = [m for t, m in sorted(self.latest.items()) if topic_filter in t][:limit]
        return {"connected": self.connected(), "broker": f"{self.host}:{self.port}" if self.host else None,
                "filters": self.filters, "count": self.count, "rate": round(self.rate, 1),
                "topics": len(self.latest), "latest": latest, "recent": list(self.recent)[-50:][::-1]}


# ---------------------------------------------------------------- BACnet


class BacnetClient:
    """A small BACnet/IP client application bound to its own UDP port."""

    def __init__(self):
        self.app = None
        self.address = None

    async def ensure(self, port: int = 47899, ip_address: str = "auto"):
        from .bacnet_server import resolve_address
        address = resolve_address(ip_address, port)
        if self.app is not None and self.address == address:
            return self.app
        self.close()
        import argparse
        from bacpypes3.app import Application
        args = argparse.Namespace(vendoridentifier=999, instance=4194300, name="BIDPS-UI-Client",
                                  address=address, network=0, foreign=None, bbmd=None, ttl=30)
        self.app = Application.from_args(args)
        self.address = address
        return self.app

    def close(self) -> None:
        if self.app is not None:
            try:
                self.app.close()
            except Exception:
                pass
        self.app = None

    async def who_is(self, low: int | None = None, high: int | None = None, address: str | None = None,
                     timeout: float = 3.0, client_port: int = 47899) -> list[dict]:
        from bacpypes3.pdu import Address
        app = await self.ensure(client_port)
        i_ams = await app.who_is(low, high, Address(address) if address else None, timeout=timeout)
        return [{"device_instance": i.iAmDeviceIdentifier[1], "address": str(i.pduSource),
                 "max_apdu": i.maxAPDULengthAccepted, "vendor_id": i.vendorID} for i in i_ams]

    async def read_range(self, address: str, object_type: str, start: int, count: int,
                         prop: str = "present-value", client_port: int = 47899) -> list[dict]:
        from bacpypes3.pdu import Address
        from bacpypes3.primitivedata import ObjectIdentifier
        app = await self.ensure(client_port)
        if not 1 <= count <= 500:
            raise ValueError("count must be 1..500")
        addr = Address(address)
        sem = asyncio.Semaphore(20)

        async def one(inst: int) -> dict:
            oid = ObjectIdentifier(f"{object_type},{inst}")
            async with sem:
                try:
                    v = await asyncio.wait_for(app.read_property(addr, oid, prop), 5)
                    name = await asyncio.wait_for(app.read_property(addr, oid, "object-name"), 5)
                    if object_type.startswith("binary") and prop == "present-value":
                        v = "active" if int(v) else "inactive"
                    elif isinstance(v, float):
                        v = round(v, 6)
                    elif not isinstance(v, (int, bool, str)):
                        v = str(v)
                    return {"object": f"{object_type},{inst}", "name": str(name), "value": v}
                except Exception as e:
                    return {"object": f"{object_type},{inst}", "name": None, "value": None,
                            "error": str(e) or type(e).__name__}

        return await asyncio.gather(*(one(i) for i in range(start, start + count)))

    async def read_property(self, address: str, obj: str, prop: str, client_port: int = 47899):
        from bacpypes3.pdu import Address
        from bacpypes3.primitivedata import ObjectIdentifier
        app = await self.ensure(client_port)
        v = await asyncio.wait_for(app.read_property(Address(address), ObjectIdentifier(obj), prop), 10)
        try:
            json.dumps(v)
            return v
        except TypeError:
            return str(v)
