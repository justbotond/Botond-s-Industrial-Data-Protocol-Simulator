"""Botond's Industrial Data Protocol Simulator - process runner.

Every configured instance (each Modbus server, OPC UA server, MQTT broker, MQTT publisher,
BACnet device, OPC DA server) runs in its own OS process, so instances scale across CPU cores
and cannot slow each other down. The web UI (manager.py) and the headless CLI both use this.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import logging.handlers
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import yaml

log = logging.getLogger("simulator")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-11s %(message)s"

# config section -> human readable protocol name
KINDS = {
    "modbus": "Modbus TCP server",
    "opcua": "OPC UA server",
    "opcda": "OPC DA server",
    "mqtt_broker": "MQTT broker",
    "mqtt": "MQTT publisher",
    "bacnet": "BACnet/IP device",
}
# brokers start first so publishers can connect
START_ORDER = ("mqtt_broker", "modbus", "opcua", "opcda", "bacnet", "mqtt")


def load_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def save_config(path: str | Path, cfg: dict) -> None:
    Path(path).write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")


def instance_kwargs(kind: str, c: dict) -> dict:
    """Config entry -> constructor kwargs."""
    c = {k: v for k, v in c.items() if k not in ("enabled", "description")}
    if "tags" in c:
        c["tag_groups"] = c.pop("tags")
    return c


def check_ports(cfg: dict, only: set[str] | None = None) -> None:
    """Raise ValueError if two instances that would run together share a port."""
    ports: dict[tuple[str, int], str] = {}
    for kind in START_ORDER:
        if kind in ("mqtt", "opcda"):
            continue  # publishers don't listen; OPC DA is reached through COM
        for c in cfg.get(kind) or []:
            if only is not None and c.get("name") not in only:
                continue
            proto = "udp" if kind == "bacnet" else "tcp"
            key = (proto, int(c["port"]))
            if key in ports:
                raise ValueError(f"{proto.upper()} port {key[1]} is used by both '{ports[key]}' and '{c['name']}'")
            ports[key] = c["name"]


def plan(cfg: dict) -> list[tuple[str, dict]]:
    """Turn the config into (kind, kwargs) jobs for all enabled instances and validate names/ports."""
    jobs, names = [], set()
    for kind in START_ORDER:
        for c in cfg.get(kind) or []:
            if not c.get("enabled", True):
                continue
            name = c.get("name")
            if not name:
                raise ValueError(f"an entry in '{kind}' has no name")
            if name in names:
                raise ValueError(f"duplicate instance name '{name}'")
            names.add(name)
            jobs.append((kind, instance_kwargs(kind, c)))
    check_ports(cfg, names)
    if not jobs:
        raise ValueError("nothing enabled in config")
    return jobs


def make(kind: str, kwargs: dict):
    if kind == "modbus":
        from .modbus_server import ModbusSimServer
        return ModbusSimServer(**kwargs)
    if kind == "opcua":
        from .opcua_server import OpcUaSimServer
        return OpcUaSimServer(**kwargs)
    if kind == "mqtt_broker":
        from .mqtt_broker import MqttBroker
        return MqttBroker(**kwargs)
    if kind == "mqtt":
        from .mqtt_publisher import MqttSimPublisher
        return MqttSimPublisher(**kwargs)
    if kind == "bacnet":
        from .bacnet_server import BacnetSimServer
        return BacnetSimServer(**kwargs)
    if kind == "opcda":
        from .opcda_server import OpcDaSimServer
        return OpcDaSimServer(**kwargs)
    raise ValueError(f"unknown kind '{kind}'")


def write_tag_map(obj, folder: Path) -> Path:
    rows = obj.tag_map()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{obj.name}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    logging.getLogger(obj.name).info("wrote %d tags to %s", len(rows), path)
    return path


def run_instance(kind: str, kwargs: dict, export_dir: str | None = None, verbose: bool = False,
                 queue: mp.Queue | None = None) -> None:
    """Process entry point: build one instance and run it on its own event loop.

    With a queue, log records and a stats message every second are sent to the parent.
    """
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if queue is not None:
        root.addHandler(logging.handlers.QueueHandler(queue))
    else:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(h)
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    logging.getLogger("bacpypes3").setLevel(logging.ERROR)
    name = kwargs.get("name", kind)

    if sys.platform == "win32" and kind == "bacnet":
        # bacpypes3 UDP endpoints are more reliable on the selector loop
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def main():
        obj = make(kind, kwargs)
        task = asyncio.create_task(obj.run())

        async def report():
            while True:
                if queue is not None and hasattr(obj, "stats"):
                    try:
                        queue.put_nowait({"type": "stats", "name": name, "data": obj.stats()})
                    except Exception:
                        pass
                await asyncio.sleep(1)

        async def watch_parent():
            # exit when the manager / CLI process dies, even if it was killed hard
            parent = mp.parent_process()
            while parent is not None:
                if not parent.is_alive():
                    logging.getLogger(name).warning("%s: parent process gone, exiting", name)
                    os._exit(0)
                await asyncio.sleep(2)

        reporter = asyncio.create_task(report())
        watcher = asyncio.create_task(watch_parent())
        if export_dir and hasattr(obj, "tag_map"):
            ready = asyncio.create_task(obj.ready.wait())
            await asyncio.wait({task, ready}, return_when=asyncio.FIRST_COMPLETED)
            if ready.done():
                write_tag_map(obj, Path(export_dir))
        try:
            await task
        finally:
            reporter.cancel()
            watcher.cancel()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.getLogger(name).exception("%s: stopped with error: %s", name, e)
        time.sleep(0.3)  # let the queue feeder thread flush
        sys.exit(1)


def headless(config: str, export_tags: str | None, verbose: bool) -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    try:
        jobs = plan(load_config(config))
    except (ValueError, KeyError) as e:
        sys.exit(f"config error: {e}")

    procs = []
    for kind, kwargs in jobs:
        if kind == "opcda":
            kwargs["config_path"] = str(Path(config).resolve())
        p = mp.Process(target=run_instance, args=(kind, kwargs, export_tags, verbose),
                       name=kwargs["name"], daemon=True)
        p.start()
        procs.append(p)
        if kind == "mqtt_broker":
            time.sleep(0.5)  # let brokers bind before publishers connect
    log.info("started %d instance process(es): %s - press Ctrl+C to stop", len(procs),
             ", ".join(p.name for p in procs))
    try:
        while True:
            for p in procs:
                if not p.is_alive():
                    log.error("instance '%s' exited (code %s); stopping", p.name, p.exitcode)
                    raise SystemExit(1)
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(5)
