"""MQTT data simulator: publishes simulated tag sets to a broker (built-in or external)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from .mqtt_client import MqttError, MqttPublisher
from .tags import TagSet, TagSpec

log = logging.getLogger("mqtt")

PAYLOAD_FORMATS = ("json", "plain")
MODES = ("per_tag", "batch")


class MqttSimPublisher:
    def __init__(self, name: str, broker_host: str, broker_port: int, update_interval_ms: int,
                 tag_groups: list[dict], topic_prefix: str | None = None, mode: str = "per_tag",
                 batch_size: int = 1000, payload_format: str = "json", qos: int = 0,
                 retain: bool = False, username: str | None = None, password: str | None = None):
        if mode not in MODES:
            raise ValueError(f"{name}: mode must be one of {MODES}")
        if payload_format not in PAYLOAD_FORMATS:
            raise ValueError(f"{name}: payload_format must be one of {PAYLOAD_FORMATS}")
        self.name = name
        self.broker_host, self.broker_port = broker_host, broker_port
        self.interval = update_interval_ms / 1000
        self.prefix = (topic_prefix or f"sim/{name}").rstrip("/")
        self.mode, self.batch_size = mode, max(1, batch_size)
        self.payload_format = payload_format
        self.qos, self.retain = qos, retain
        self.username, self.password = username, password
        self.tagsets = [TagSet(TagSpec(**g)) for g in tag_groups]
        self.names = [ts.names() for ts in self.tagsets]
        self.ready = asyncio.Event()
        self.ready.set()

    def _messages(self, ts_iso: str):
        for ts, names in zip(self.tagsets, self.names):
            values = ts.tick()
            base = f"{self.prefix}/{ts.spec.name}"
            if self.mode == "per_tag":
                for n, v in zip(names, values):
                    if self.payload_format == "json":
                        yield f"{base}/{n}", json.dumps({"value": v, "timestamp": ts_iso})
                    else:
                        yield f"{base}/{n}", str(v)
            else:
                for i in range(0, len(values), self.batch_size):
                    chunk = dict(zip(names[i:i + self.batch_size], values[i:i + self.batch_size]))
                    yield f"{base}/batch/{i // self.batch_size}", json.dumps(
                        {"timestamp": ts_iso, "values": chunk})

    def tag_map(self) -> list[dict]:
        rows = []
        for ts, names in zip(self.tagsets, self.names):
            for i, n in enumerate(names):
                topic = (f"{self.prefix}/{ts.spec.name}/{n}" if self.mode == "per_tag"
                         else f"{self.prefix}/{ts.spec.name}/batch/{i // self.batch_size}")
                rows.append({"server": self.name, "tag": n, "broker": f"{self.broker_host}:{self.broker_port}",
                             "topic": topic, "json_key": "value" if self.mode == "per_tag" else f"values.{n}",
                             "data_type": ts.spec.data_type})
        return rows

    async def run(self) -> None:
        total = sum(ts.spec.count for ts in self.tagsets)
        while True:
            try:
                async with MqttPublisher(self.broker_host, self.broker_port, f"bidps-{self.name}",
                                         self.username, self.password) as client:
                    log.info("%s: publishing %d tags to %s:%d under '%s/#' every %d ms (%s)", self.name, total,
                             self.broker_host, self.broker_port, self.prefix, int(self.interval * 1000), self.mode)
                    loop = asyncio.get_running_loop()
                    next_t = loop.time()
                    while True:
                        ts_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                        for k, (topic, payload) in enumerate(self._messages(ts_iso)):
                            client.publish_nowait(topic, payload, self.qos, self.retain)
                            if k % 5000 == 4999:
                                await client.flush()
                        await client.flush()
                        next_t += self.interval
                        delay = next_t - loop.time()
                        if delay < 0:
                            log.warning("%s: publishing took longer than interval (%.0f ms behind)",
                                        self.name, -delay * 1000)
                            next_t = loop.time()
                            delay = 0
                        await asyncio.sleep(delay)
            except MqttError as e:
                log.error("%s: broker connection lost (%s); retrying in 3 s", self.name, e)
                await asyncio.sleep(3)
