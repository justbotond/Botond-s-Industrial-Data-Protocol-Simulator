"""Botond's Industrial Data Protocol Simulator - process runner.

    python -m simulator                       # uses config.yaml
    python -m simulator -c my.yaml
    python -m simulator --export-tags tagmaps

Every configured instance (each Modbus server, OPC UA server, MQTT broker and MQTT publisher)
runs in its own OS process, so instances scale across CPU cores and cannot slow each other down.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import multiprocessing as mp
import sys
import time
from pathlib import Path

import yaml

log = logging.getLogger("simulator")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-11s %(message)s"


def plan(cfg: dict) -> list[tuple[str, dict]]:
    """Turn the config into (kind, kwargs) jobs and validate names/ports."""
    jobs, ports, names = [], {}, set()
    for kind in ("mqtt_broker", "modbus", "opcua", "mqtt"):
        for c in cfg.get(kind) or []:
            if not c.get("enabled", True):
                continue
            c = {k: v for k, v in c.items() if k != "enabled"}
            name = c.setdefault("name", f"{kind}{len(jobs) + 1}")
            if name in names:
                raise ValueError(f"duplicate instance name '{name}'")
            names.add(name)
            if kind != "mqtt":
                port = c["port"]
                if port in ports:
                    raise ValueError(f"port {port} used by both '{ports[port]}' and '{name}'")
                ports[port] = name
            if "tags" in c:
                c["tag_groups"] = c.pop("tags")
            jobs.append((kind, c))
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
    from .mqtt_publisher import MqttSimPublisher
    return MqttSimPublisher(**kwargs)


def write_tag_map(obj, folder: Path) -> None:
    rows = obj.tag_map()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{obj.name}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    logging.getLogger(obj.name).info("wrote %d tags to %s", len(rows), path)


def run_instance(kind: str, kwargs: dict, export_dir: str | None, verbose: bool) -> None:
    """Process entry point: build one instance and run it on its own event loop."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=LOG_FORMAT)
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    obj = make(kind, kwargs)

    async def main():
        task = asyncio.create_task(obj.run())
        if export_dir and hasattr(obj, "tag_map"):
            await obj.ready.wait()  # OPC UA namespace index is only known after the server is built
            write_tag_map(obj, Path(export_dir))
        await task

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(prog="simulator", description="Botond's Industrial Data Protocol Simulator")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--export-tags", metavar="DIR", help="write a CSV tag map per instance into DIR")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    try:
        jobs = plan(cfg)
    except (ValueError, KeyError) as e:
        sys.exit(f"config error: {e}")

    procs = []
    for kind, kwargs in jobs:
        p = mp.Process(target=run_instance, args=(kind, kwargs, args.export_tags, args.verbose),
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


if __name__ == "__main__":
    main()
