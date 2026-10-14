"""Built-in OPC DA test client used by the web UI. All COM calls run on one dedicated STA thread."""

from __future__ import annotations

import concurrent.futures
import ctypes
import datetime
from ctypes import POINTER, byref


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


class OpcDaClient:
    MAX_ITEMS = 200

    def __init__(self):
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="opcda",
                                                              initializer=self._init_thread)
        self.prog_id = None
        self.server = None
        self.group = None
        self.handles: dict[str, int] = {}

    @staticmethod
    def _init_thread():
        import comtypes
        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)

    # ---------------------------------------------------------- discovery
    def list_servers(self, host: str = "") -> list[str]:
        """ProgIDs of OPC servers registered on this PC (ProgID keys with 'OPC' and 'CLSID' sub keys)."""
        import winreg
        if host and host.lower() not in ("localhost", "127.0.0.1", "."):
            raise RuntimeError("listing remote OPC servers (OPCEnum over DCOM) is not supported; "
                               "type the ProgID and leave the host blank to use a local server")
        found = set()
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "") as root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if name.startswith((".", "{")) or "." not in name:
                    continue
                try:
                    winreg.OpenKey(root, name + r"\OPC").Close()
                    winreg.OpenKey(root, name + r"\CLSID").Close()
                    found.add(name)
                except OSError:
                    pass
        return sorted(found)

    # ---------------------------------------------------------- connection
    def _connect(self, prog_id: str, host: str = ""):
        import comtypes
        from . import opcda_com as oc
        if host and host.lower() not in ("localhost", "127.0.0.1", "."):
            raise RuntimeError("remote OPC DA (DCOM) is not supported by the built-in client")
        if self.server is not None and self.prog_id == prog_id:
            try:
                st = POINTER(oc.OPCSERVERSTATUS)()
                self.server.GetStatus(byref(st))
                oc.co_free(st.contents.szVendorInfo)
                oc.co_free(st)
                return self.server
            except Exception:
                self.disconnect()
        try:
            clsid = comtypes.GUID.from_progid(prog_id)
        except OSError:
            raise RuntimeError(f"ProgID '{prog_id}' is not registered on this PC")
        try:
            self.server = comtypes.CoCreateInstance(clsid, interface=oc.IOPCServer,
                                                    clsctx=comtypes.CLSCTX_LOCAL_SERVER)
        except OSError as e:
            if getattr(e, "winerror", None) == oc.E_NOINTERFACE or getattr(e, "hresult", None) == oc.E_NOINTERFACE:
                from .opcda_support import opcda_status
                st = opcda_status()
                raise RuntimeError(
                    "The server was reached, but Windows could not pass the OPC DA interfaces between "
                    "processes (E_NOINTERFACE). " + ("Install the 64-bit OPC Core Components (OPC Foundation "
                    "proxy/stub DLLs)." if not st.get("proxies_registered") else
                    "Check that the OPC Core Components are installed correctly.")) from e
            raise
        self.prog_id = prog_id
        try:
            self.server.QueryInterface(oc.IOPCCommon).SetClientName("BIDPS web UI")
        except Exception:
            pass
        return self.server

    def attach(self, server_ptr, prog_id: str = "in-process") -> None:
        """Use an existing IOPCServer pointer (used by tests)."""
        self.disconnect()
        self.server, self.prog_id = server_ptr, prog_id

    def disconnect(self) -> None:
        self.group = None
        self.handles = {}
        self.server = None
        self.prog_id = None

    def _srv(self, prog_id: str, host: str):
        if self.server is not None and prog_id == self.prog_id == "in-process":
            return self.server
        return self._connect(prog_id, host)

    def _ensure_group(self):
        from comtypes import IUnknown
        from . import opcda_com as oc
        if self.group is not None:
            return self.group
        hserver, revised = oc.DWORD(), oc.DWORD()
        unk = POINTER(IUnknown)()
        self.server.AddGroup("BIDPS_UI", True, 1000, 1, None, None, 0x0800, byref(hserver), byref(revised),
                             byref(oc.IOPCItemMgt._iid_), byref(unk))
        self.group = unk.QueryInterface(oc.IOPCItemMgt)
        self.handles = {}
        return self.group

    # ---------------------------------------------------------- browse / read
    @staticmethod
    def _enum_strings(penum) -> list[str]:
        from . import opcda_com as oc
        out = []
        buf = (oc.LPWSTR * 256)()
        raw = ctypes.cast(buf, POINTER(ctypes.c_void_p))
        fetched = ctypes.c_ulong()
        while True:
            hr = penum.Next(256, buf, byref(fetched))
            for i in range(fetched.value):
                out.append(buf[i])
                oc.co_free(raw[i])
            if hr != 0 or fetched.value == 0:
                return out

    def browse(self, prog_id: str, host: str = "", path: str = "") -> dict:
        from . import opcda_com as oc
        srv = self._srv(prog_id, host)
        b = srv.QueryInterface(oc.IOPCBrowseServerAddressSpace)
        b.ChangeBrowsePosition(oc.OPC_BROWSE_TO, "")
        for part in [p for p in (path or "").split(".") if p]:
            b.ChangeBrowsePosition(oc.OPC_BROWSE_DOWN, part)

        def listing(kind):
            pe = POINTER(oc.IEnumString)()
            b.BrowseOPCItemIDs(kind, "", oc.VT_EMPTY, 0, byref(pe))
            return self._enum_strings(pe) if pe else []

        branches, leaves = listing(oc.OPC_BRANCH), listing(oc.OPC_LEAF)
        items = []
        for name in leaves[:self.MAX_ITEMS]:
            out = oc.LPWSTR()
            b.GetItemID(name, byref(out))
            items.append({"name": name, "item_id": out.value})
            oc.co_free(out)
        values = {r["item_id"]: r for r in self._read([i["item_id"] for i in items])} if items else {}
        for it in items:
            it.update({k: v for k, v in values.get(it["item_id"], {}).items() if k != "item_id"})
        return {"path": path, "branches": branches, "items": items, "truncated": len(leaves) > self.MAX_ITEMS,
                "leaf_count": len(leaves)}

    def read(self, prog_id: str, host: str, item_ids: list[str]) -> list[dict]:
        self._srv(prog_id, host)
        return self._read(item_ids[:1000])

    def _read(self, item_ids: list[str]) -> list[dict]:
        from . import opcda_com as oc
        mgt = self._ensure_group()
        new = [i for i in item_ids if i not in self.handles]
        rows: dict[str, dict] = {}
        if new:
            defs = (oc.OPCITEMDEF * len(new))()
            for k, iid in enumerate(new):
                defs[k].szItemID, defs[k].bActive, defs[k].hClient = iid, True, k
            res, perrs = POINTER(oc.OPCITEMRESULT)(), POINTER(oc.HRESULT)()
            mgt.AddItems(len(new), defs, byref(res), byref(perrs))
            errs = ctypes.cast(perrs, POINTER(ctypes.c_long))
            for k, iid in enumerate(new):
                if errs[k] == 0:
                    self.handles[iid] = res[k].hServer
                else:
                    rows[iid] = {"item_id": iid, "value": None, "quality": None, "timestamp": None,
                                 "error": oc.ERROR_TEXT.get(errs[k], f"0x{errs[k] & 0xFFFFFFFF:08X}")}
            oc.co_free(res)
            oc.co_free(errs)
        known = [i for i in item_ids if i in self.handles]
        if known:
            sync = mgt.QueryInterface(oc.IOPCSyncIO)
            hs = (oc.OPCHANDLE * len(known))(*[self.handles[i] for i in known])
            states, perrs = POINTER(oc.OPCITEMSTATE)(), POINTER(oc.HRESULT)()
            sync.Read(oc.OPC_DS_DEVICE, len(known), hs, byref(states), byref(perrs))
            errs = ctypes.cast(perrs, POINTER(ctypes.c_long))
            for k, iid in enumerate(known):
                st = states[k]
                v = oc.variant_value(st.vDataValue)
                if isinstance(v, float):
                    v = round(v, 6)
                rows[iid] = {"item_id": iid, "value": v,
                             "quality": oc.QUALITY_TEXT.get(st.wQuality, f"0x{st.wQuality:02X}"),
                             "timestamp": _iso(oc.filetime_to_unix(st.ftTimeStamp)),
                             "error": None if errs[k] == 0 else f"0x{errs[k] & 0xFFFFFFFF:08X}"}
                oc.variant_clear(st.vDataValue)
            oc.co_free(states)
            oc.co_free(errs)
        return [rows[i] for i in item_ids if i in rows]

    def close(self):
        try:
            self.executor.submit(self.disconnect).result(5)
        except Exception:
            pass
        self.executor.shutdown(wait=False)
