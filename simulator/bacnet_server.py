"""BACnet/IP device simulator (bacpypes3) exposing tag groups as BACnet objects."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import socket

from .common import CycleStats
from .tags import TagSet, TagSpec

log = logging.getLogger("bacnet")

OBJECT_TYPES = ("analog-input", "analog-value", "binary-input", "binary-value")


def primary_ipv4() -> str:
    """IP of the interface used for the default route (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def resolve_address(ip_address: str, port: int) -> str:
    """'auto' or 'a.b.c.d' or 'a.b.c.d/nn' -> 'a.b.c.d/nn:port' (prefix needed for broadcasts)."""
    ip = primary_ipv4() if ip_address in ("", "auto", None) else ip_address
    if "/" not in ip:
        ip += "/24"
    ipaddress.ip_interface(ip)  # validate
    return f"{ip}:{port}"


class BacnetSimServer:
    def __init__(self, name: str, port: int, update_interval_ms: int, tag_groups: list[dict],
                 device_instance: int = 1001, ip_address: str = "auto", host: str | None = None,
                 vendor_identifier: int = 999):
        self.name, self.port = name, port
        self.interval = update_interval_ms / 1000
        self.device_instance = device_instance
        self.address = resolve_address(ip_address, port)
        self.vendor_identifier = vendor_identifier
        self.groups: list[tuple[TagSet, str, int]] = []
        used: dict[str, set] = {t: set() for t in OBJECT_TYPES}
        next_free = {t: 0 for t in OBJECT_TYPES}
        for g in tag_groups:
            g = dict(g)
            otype = g.pop("object_type", "analog-value")
            if otype not in OBJECT_TYPES:
                raise ValueError(f"{name}: object_type must be one of {OBJECT_TYPES}")
            start = g.pop("start_instance", None)
            g.pop("units", None)
            spec = TagSpec(**g)
            spec.data_type = "bool" if otype.startswith("binary") else "float32"
            start = next_free[otype] if start is None else int(start)
            rng = set(range(start, start + spec.count))
            if rng & used[otype]:
                raise ValueError(f"{name}/{spec.name}: {otype} instance numbers overlap another group")
            if start + spec.count > 4194303:
                raise ValueError(f"{name}/{spec.name}: instance numbers exceed 4194302")
            used[otype] |= rng
            next_free[otype] = max(next_free[otype], start + spec.count)
            self.groups.append((TagSet(spec), otype, start))
        self.objects: list[list] = []
        self.cycle = CycleStats(name, self.interval, log)
        self.ready = asyncio.Event()
        self.app = None

    def _build(self):
        from bacpypes3.app import Application
        from bacpypes3.local.analog import AnalogInputObject, AnalogValueObject
        from bacpypes3.local.binary import BinaryInputObject, BinaryValueObject

        classes = {"analog-input": AnalogInputObject, "analog-value": AnalogValueObject,
                   "binary-input": BinaryInputObject, "binary-value": BinaryValueObject}
        args = argparse.Namespace(vendoridentifier=self.vendor_identifier, instance=self.device_instance,
                                  name=self.name, address=self.address, network=0, foreign=None, bbmd=None,
                                  ttl=30)
        app = Application.from_args(args)
        for ts, otype, start in self.groups:
            cls = classes[otype]
            objs = []
            for i, (tag, v) in enumerate(zip(ts.names(), ts.values)):
                kw = dict(objectIdentifier=(otype, start + i), objectName=tag, statusFlags=[0, 0, 0, 0],
                          eventState="normal", outOfService=False)
                if otype.startswith("binary"):
                    kw.update(presentValue="active" if v else "inactive")
                    if otype == "binary-input":
                        kw.update(polarity="normal")
                else:
                    kw.update(presentValue=float(v), units="noUnits")
                o = cls(**kw)
                app.add_object(o)
                objs.append(o)
            self.objects.append(objs)
        return app

    async def _step(self) -> None:
        for (ts, otype, _), objs in zip(self.groups, self.objects):
            values = ts.tick()
            if otype.startswith("binary"):
                for o, v in zip(objs, values):
                    o.presentValue = "active" if v else "inactive"
            else:
                for o, v in zip(objs, values):
                    o.presentValue = v

    def stats(self) -> dict:
        return {**self.cycle.stats(), "tags": sum(ts.spec.count for ts, _, _ in self.groups),
                "address": self.address, "device_instance": self.device_instance,
                "phase": "running" if self.ready.is_set() else "building objects"}

    def tag_map(self) -> list[dict]:
        rows = []
        for ts, otype, start in self.groups:
            for i, tag in enumerate(ts.names()):
                rows.append({"server": self.name, "tag": tag, "device_instance": self.device_instance,
                             "address": self.address.split("/")[0] + ":" + str(self.port),
                             "object": f"{otype},{start + i}", "property": "present-value",
                             "data_type": ts.spec.data_type})
        return rows

    async def run(self) -> None:
        self.app = self._build()
        self.ready.set()
        log.info("%s: BACnet/IP device %d on %s (%d objects, %d ms)", self.name, self.device_instance,
                 self.address, sum(ts.spec.count for ts, _, _ in self.groups), int(self.interval * 1000))
        for ts, otype, start in self.groups:
            log.info("%s:   %s x%d %s %d..%d", self.name, ts.spec.name, ts.spec.count, otype, start,
                     start + ts.spec.count - 1)
        await self.cycle.run(self._step)
