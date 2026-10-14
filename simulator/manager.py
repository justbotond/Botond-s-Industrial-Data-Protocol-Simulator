"""Web UI backend: edit the configuration, start/stop every simulator instance separately,
collect their logs and live statistics, and drive the built-in test clients."""

from __future__ import annotations

import asyncio
import copy
import itertools
import logging
import multiprocessing as mp
import platform
import socket
import sys
import threading
import time
from collections import deque
from pathlib import Path

from aiohttp import web

from . import clients
from .bacnet_server import primary_ipv4
from .runner import KINDS, START_ORDER, instance_kwargs, load_config, make, run_instance, save_config

log = logging.getLogger("manager")
WEB_DIR = Path(__file__).parent / "web"
LOG_LINES = 1000


class Instance:
    def __init__(self, name: str, kind: str):
        self.name, self.kind = name, kind
        self.proc: mp.Process | None = None
        self.state = "stopped"  # stopped | starting | running | stopping | error
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.stats: dict = {}
        self.config_snapshot: dict | None = None
        self.error: str | None = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()


class Manager:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.cfg = load_config(self.config_path)
        for kind in KINDS:
            self.cfg.setdefault(kind, [])
        self.instances: dict[str, Instance] = {}
        self.logs: deque = deque(maxlen=LOG_LINES)
        self.log_ids = itertools.count(1)
        self.ctx = mp.get_context("spawn")
        self.queue = self.ctx.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.opcua = clients.OpcUaClientPool()
        self.mqtt = clients.MqttMonitor()
        self.bacnet = clients.BacnetClient()
        self.opcda = None

    # ------------------------------------------------------------ config helpers
    def entries(self):
        for kind in KINDS:
            for c in self.cfg.get(kind) or []:
                yield kind, c

    def find(self, name: str) -> tuple[str, dict]:
        for kind, c in self.entries():
            if c.get("name") == name:
                return kind, c
        raise web.HTTPNotFound(text=f"no instance named '{name}'")

    def add_log(self, level: str, name: str, msg: str, ts: float | None = None) -> None:
        self.logs.append({"id": next(self.log_ids), "time": ts or time.time(), "level": level,
                          "source": name, "message": msg})

    # ------------------------------------------------------------ child process plumbing
    def _queue_reader(self) -> None:
        """Thread: forward log records / stats from child processes into the event loop."""
        while True:
            try:
                item = self.queue.get()
            except (EOFError, OSError):
                return
            if item is None:
                return
            self.loop.call_soon_threadsafe(self._on_child_message, item)

    def _on_child_message(self, item) -> None:
        if isinstance(item, dict) and item.get("type") == "stats":
            inst = self.instances.get(item["name"])
            if inst is not None and inst.alive():
                inst.stats = item["data"]
                if inst.state == "starting" and item["data"].get("phase", "running") == "running":
                    inst.state = "running"
            return
        if isinstance(item, logging.LogRecord):
            msg = item.getMessage()
            if item.exc_text:
                msg += "\n" + item.exc_text
            self.add_log(item.levelname, item.name, msg, item.created)

    async def _watchdog(self) -> None:
        while True:
            for inst in list(self.instances.values()):
                if inst.proc is not None and not inst.proc.is_alive() and inst.state in ("starting", "running"):
                    inst.exit_code = inst.proc.exitcode
                    inst.state = "error" if inst.exit_code else "stopped"
                    inst.error = f"process exited with code {inst.exit_code}"
                    self.add_log("ERROR", "manager", f"{inst.name}: {inst.error}")
                    inst.proc = None
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------ lifecycle
    def _port_conflict(self, kind: str, c: dict) -> str | None:
        if kind in ("mqtt", "opcda"):
            return None
        proto = "udp" if kind == "bacnet" else "tcp"
        for other in self.instances.values():
            if other.name == c["name"] or not other.alive():
                continue
            okind, oc = self.find(other.name)
            if okind in ("mqtt", "opcda"):
                continue
            if ("udp" if okind == "bacnet" else "tcp") == proto and int(oc["port"]) == int(c["port"]):
                return f"{proto.upper()} port {c['port']} is already used by running instance '{other.name}'"
        return None

    async def start(self, name: str) -> Instance:
        kind, c = self.find(name)
        inst = self.instances.get(name)
        if inst is not None and inst.alive():
            raise web.HTTPConflict(text=f"'{name}' is already running")
        conflict = self._port_conflict(kind, c)
        if conflict:
            raise web.HTTPConflict(text=conflict)
        kwargs = instance_kwargs(kind, c)
        if kind == "opcda":  # lets COM start the server standalone when the simulator isn't running
            kwargs["config_path"] = str(self.config_path.resolve())
        try:  # validate in a thread: building tag sets for big groups takes a moment
            await asyncio.get_running_loop().run_in_executor(None, make, kind, copy.deepcopy(kwargs))
        except Exception as e:
            raise web.HTTPBadRequest(text=f"invalid configuration for '{name}': {e}")
        inst = Instance(name, kind)
        inst.proc = self.ctx.Process(target=run_instance, args=(kind, kwargs, None, False, self.queue),
                                     name=name, daemon=True)
        inst.proc.start()
        inst.state = "starting" if kind in ("opcua", "bacnet", "opcda") else "running"
        inst.started_at = time.time()
        inst.config_snapshot = copy.deepcopy(c)
        self.instances[name] = inst
        self.add_log("INFO", "manager", f"started {KINDS[kind]} '{name}' (pid {inst.proc.pid})")
        return inst

    async def stop(self, name: str) -> None:
        inst = self.instances.get(name)
        if inst is None or not inst.alive():
            if inst is not None:
                inst.state = "stopped"
            return
        inst.state = "stopping"
        proc = inst.proc
        proc.terminate()
        await asyncio.get_running_loop().run_in_executor(None, proc.join, 5)
        if proc.is_alive():
            proc.kill()
        inst.proc = None
        inst.state = "stopped"
        inst.stats = {}
        self.add_log("INFO", "manager", f"stopped '{name}'")

    def status(self) -> dict:
        out = {}
        for kind, c in self.entries():
            name = c["name"]
            inst = self.instances.get(name)
            alive = inst is not None and inst.alive()
            out[name] = {
                "kind": kind,
                "state": inst.state if inst else "stopped",
                "pid": inst.proc.pid if alive else None,
                "uptime_s": int(time.time() - inst.started_at) if alive and inst.started_at else None,
                "stats": inst.stats if alive else {},
                "error": inst.error if inst and inst.state == "error" else None,
                "config_changed": bool(alive and inst.config_snapshot != c),
            }
        return out

    async def shutdown(self) -> None:
        for name in list(self.instances):
            await self.stop(name)
        await self.opcua.close_all()
        await self.mqtt.disconnect()
        self.bacnet.close()
        if self.opcda is not None:
            self.opcda.close()
        self.queue.put(None)


# ================================================================ HTTP handlers

routes = web.RouteTableDef()


def M(request) -> Manager:
    return request.app["manager"]


async def body(request) -> dict:
    try:
        return await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="expected a JSON body")


def guard(fn):
    """Turn client errors into JSON {error} responses instead of 500 pages."""
    async def wrapper(request):
        try:
            return await fn(request)
        except web.HTTPException:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            return web.json_response({"error": "timed out"}, status=504)
        except Exception as e:
            return web.json_response({"error": f"{type(e).__name__}: {e}" if not str(e) else str(e)}, status=502)
    return wrapper


@routes.get("/")
async def index(request):
    return web.FileResponse(WEB_DIR / "index.html")


@routes.get("/api/meta")
async def meta(request):
    from .opcda_support import opcda_status
    return web.json_response({
        "app": "Botond's Industrial Data Protocol Simulator",
        "kinds": KINDS, "start_order": START_ORDER,
        "hostname": socket.gethostname(), "primary_ip": primary_ipv4(),
        "python": platform.python_version(), "platform": platform.platform(),
        "config_path": str(M(request).config_path.resolve()),
        "opcda": opcda_status(),
    })


@routes.get("/api/config")
async def get_config(request):
    return web.json_response(M(request).cfg)


@routes.put("/api/config")
async def put_config(request):
    m = M(request)
    cfg = await body(request)
    names = set()
    for kind in KINDS:
        items = cfg.get(kind) or []
        if not isinstance(items, list):
            raise web.HTTPBadRequest(text=f"'{kind}' must be a list")
        for c in items:
            n = c.get("name")
            if not n:
                raise web.HTTPBadRequest(text=f"every {KINDS[kind]} needs a name")
            if n in names:
                raise web.HTTPBadRequest(text=f"duplicate instance name '{n}'")
            names.add(n)
    # instances that were removed from the config are stopped
    for n in list(m.instances):
        if n not in names:
            await m.stop(n)
            del m.instances[n]
    m.cfg = {k: cfg.get(k) or [] for k in KINDS}
    save_config(m.config_path, m.cfg)
    m.add_log("INFO", "manager", f"configuration saved to {m.config_path}")
    return web.json_response({"ok": True})


@routes.get("/api/status")
async def get_status(request):
    return web.json_response(M(request).status())


@routes.post("/api/instances/{name}/{action}")
async def instance_action(request):
    m, name, action = M(request), request.match_info["name"], request.match_info["action"]
    if action == "start":
        await m.start(name)
    elif action == "stop":
        await m.stop(name)
    elif action == "restart":
        await m.stop(name)
        await m.start(name)
    else:
        raise web.HTTPNotFound()
    return web.json_response({"ok": True, "status": m.status().get(name)})


@routes.post("/api/start_all")
async def start_all(request):
    m = M(request)
    kind = request.query.get("kind")
    errors = {}
    for k in START_ORDER:
        if kind and k != kind:
            continue
        for c in m.cfg.get(k) or []:
            if not c.get("enabled", True):
                continue
            inst = m.instances.get(c["name"])
            if inst is not None and inst.alive():
                continue
            try:
                await m.start(c["name"])
                if k == "mqtt_broker":
                    await asyncio.sleep(0.5)
            except web.HTTPException as e:
                errors[c["name"]] = e.text
    return web.json_response({"ok": not errors, "errors": errors})


@routes.post("/api/stop_all")
async def stop_all(request):
    m = M(request)
    kind = request.query.get("kind")
    for n, inst in list(m.instances.items()):
        if not kind or inst.kind == kind:
            await m.stop(n)
    return web.json_response({"ok": True})


@routes.get("/api/logs")
async def get_logs(request):
    m = M(request)
    since = int(request.query.get("since", 0))
    source = request.query.get("source")
    rows = [r for r in m.logs if r["id"] > since and (not source or r["source"] in (source, "manager")
                                                       or r["message"].startswith(source + ":"))]
    return web.json_response(rows[-500:])


@routes.get("/api/tagmap/{name}")
@guard
async def tagmap(request):
    import csv
    import io
    m = M(request)
    kind, c = m.find(request.match_info["name"])
    obj = await asyncio.get_running_loop().run_in_executor(None, make, kind, instance_kwargs(kind, c))
    if not hasattr(obj, "tag_map"):
        raise web.HTTPBadRequest(text="this instance has no tags")
    if kind == "opcua":
        obj.ns_idx = 2  # the simulator namespace is always the first one registered
    rows = obj.tag_map()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return web.Response(text=buf.getvalue(), content_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{c["name"]}_tags.csv"'})


# ---------------- test clients

@routes.post("/api/client/modbus/read")
@guard
async def modbus_read(request):
    b = await body(request)
    rows = await clients.modbus_read(b.get("host", "127.0.0.1"), int(b["port"]), int(b.get("unit", 1)),
                                     b["area"], int(b.get("address", 0)), int(b.get("count", 10)),
                                     b.get("data_type", "uint16"), b.get("word_order", "big"))
    return web.json_response({"rows": rows, "time": time.time()})


@routes.post("/api/client/opcua/{op}")
@guard
async def opcua_op(request):
    m, op = M(request), request.match_info["op"]
    b = await body(request)
    ep = b["endpoint"]
    if op == "browse":
        res = await m.opcua.browse(ep, b.get("node_id") or None, int(b.get("offset", 0)), int(b.get("limit", 200)))
    elif op == "read":
        res = {"rows": await m.opcua.read(ep, b["node_ids"]), "time": time.time()}
    elif op == "namespaces":
        res = {"namespaces": await m.opcua.namespaces(ep)}
    elif op == "disconnect":
        await m.opcua.drop(ep)
        res = {"ok": True}
    else:
        raise web.HTTPNotFound()
    return web.json_response(res)


@routes.post("/api/client/mqtt/{op}")
@guard
async def mqtt_op(request):
    m, op = M(request), request.match_info["op"]
    b = await body(request) if request.can_read_body else {}
    if op == "connect":
        await m.mqtt.connect(b.get("host", "127.0.0.1"), int(b.get("port", 1883)), b.get("username") or None,
                             b.get("password") or None)
        for f in b.get("filters", []):
            await m.mqtt.subscribe(f, int(b.get("qos", 0)))
    elif op == "disconnect":
        await m.mqtt.disconnect()
    elif op == "subscribe":
        await m.mqtt.subscribe(b["filter"], int(b.get("qos", 0)))
    elif op == "unsubscribe":
        await m.mqtt.unsubscribe(b["filter"])
    elif op == "publish":
        await m.mqtt.publish(b["topic"], b.get("payload", ""), int(b.get("qos", 0)), bool(b.get("retain")))
    elif op == "clear":
        m.mqtt.clear()
    else:
        raise web.HTTPNotFound()
    return web.json_response(m.mqtt.state())


@routes.get("/api/client/mqtt/state")
async def mqtt_state(request):
    return web.json_response(M(request).mqtt.state(request.query.get("filter", ""),
                                                   int(request.query.get("limit", 500))))


@routes.post("/api/client/bacnet/{op}")
@guard
async def bacnet_op(request):
    m, op = M(request), request.match_info["op"]
    b = await body(request)
    cport = int(b.get("client_port", 47899))
    if op == "whois":
        low, high = b.get("low"), b.get("high")
        res = {"devices": await m.bacnet.who_is(int(low) if low not in (None, "") else None,
                                                int(high) if high not in (None, "") else None,
                                                b.get("address") or None, float(b.get("timeout", 3)), cport)}
    elif op == "read_range":
        res = {"rows": await m.bacnet.read_range(b["address"], b["object_type"], int(b.get("start", 0)),
                                                 int(b.get("count", 10)), b.get("property", "present-value"),
                                                 cport), "time": time.time()}
    elif op == "read_property":
        res = {"value": await m.bacnet.read_property(b["address"], b["object"], b.get("property", "present-value"),
                                                     cport)}
    else:
        raise web.HTTPNotFound()
    return web.json_response(res)


@routes.post("/api/client/opcda/{op}")
@guard
async def opcda_op(request):
    from .opcda_support import OpcDaClient
    m, op = M(request), request.match_info["op"]
    b = await body(request)
    if m.opcda is None:
        m.opcda = OpcDaClient()
    loop = asyncio.get_running_loop()
    if op == "servers":
        res = {"servers": await loop.run_in_executor(m.opcda.executor, m.opcda.list_servers, b.get("host", ""))}
    elif op == "browse":
        res = await loop.run_in_executor(m.opcda.executor, m.opcda.browse, b["prog_id"], b.get("host", ""),
                                         b.get("path", ""))
    elif op == "read":
        res = {"rows": await loop.run_in_executor(m.opcda.executor, m.opcda.read, b["prog_id"],
                                                  b.get("host", ""), b["items"]), "time": time.time()}
    elif op == "disconnect":
        await loop.run_in_executor(m.opcda.executor, m.opcda.disconnect)
        res = {"ok": True}
    else:
        raise web.HTTPNotFound()
    return web.json_response(res)


# ================================================================ app

def build_app(config_path: str) -> web.Application:
    @web.middleware
    async def no_cache(request, handler):
        resp = await handler(request)
        resp.headers["Cache-Control"] = "no-cache"  # UI files change with the tool; always revalidate
        return resp

    app = web.Application(middlewares=[no_cache])
    m = Manager(config_path)
    app["manager"] = m

    async def on_startup(app):
        m.loop = asyncio.get_running_loop()
        threading.Thread(target=m._queue_reader, daemon=True).start()
        app["watchdog"] = asyncio.create_task(m._watchdog())
        m.add_log("INFO", "manager", f"web UI ready; configuration file {m.config_path.resolve()}")

    async def on_cleanup(app):
        app["watchdog"].cancel()
        await m.shutdown()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.add_routes(routes)
    app.router.add_static("/static/", WEB_DIR)
    return app


def serve(config_path: str, host: str, port: int) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-11s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    if sys.platform == "win32":
        # bacpypes3 (BACnet test client) needs UDP + the selector loop is the most compatible choice
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    print(f"\n  Botond's Industrial Data Protocol Simulator\n  Web UI: http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}\n")
    web.run_app(build_app(config_path), host=host, port=port, print=None)
