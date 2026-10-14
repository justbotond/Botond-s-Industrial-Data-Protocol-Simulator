"""OPC DA 2.05 server (classic COM, Windows only) exposing simulated tag sets.

Implements IOPCServer, IOPCCommon, IOPCBrowseServerAddressSpace, IOPCItemProperties and the
IOPCShutdown connection point on the server object, and IOPCItemMgt, IOPCGroupStateMgt,
IOPCSyncIO, IOPCAsyncIO2 and the IOPCDataCallback connection point on groups.

Item IDs are "<group>.<tag>", e.g. "Random.Random0001"; browsing is hierarchical (group -> tags).

The server is registered per user (HKCU) or machine wide (HKLM, when running as Administrator)
with its own CLSID derived from the ProgID. While the simulator instance runs, its class object
is registered with COM, so clients connect to it. If a client activates the ProgID while the
instance is not running, COM starts this file in standalone mode (see __main__ below).

Cross-process use needs the OPC Core Components (proxy/stub DLLs) for the bitness of this Python.
"""

from __future__ import annotations

import asyncio
import ctypes
import fnmatch
import itertools
import logging
import sys
import threading
import time
import uuid
from ctypes import POINTER, byref, c_void_p, cast
from pathlib import Path

if __name__ == "__main__" and __package__ is None:  # started by COM: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "simulator"

from .common import CycleStats  # noqa: E402
from .tags import TagSet, TagSpec  # noqa: E402

log = logging.getLogger("opcda")

VT_OF = {"float32": 4, "float64": 5, "int16": 2, "uint16": 18, "int32": 3, "uint32": 19, "bool": 11}
MIN_UPDATE_MS = 100


def clsid_for(prog_id: str) -> str:
    return "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, "bidps-opcda:" + prog_id)).upper() + "}"


# ====================================================================== registration
def register_server(prog_id: str, clsid: str, description: str, command: str) -> str:
    """Register CLSID/ProgID/OPC categories. Machine wide if allowed, else for the current user."""
    import winreg
    from .opcda_com import CATID_OPCDAServer10, CATID_OPCDAServer20

    def write(root, base):
        def setkey(path, value=None, name=""):
            with winreg.CreateKeyEx(root, base + path, 0, winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as k:
                if value is not None:
                    winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        setkey(rf"CLSID\{clsid}", description)
        setkey(rf"CLSID\{clsid}\ProgID", prog_id)
        setkey(rf"CLSID\{clsid}\LocalServer32", command)
        setkey(rf"CLSID\{clsid}\Implemented Categories\{CATID_OPCDAServer10}")
        setkey(rf"CLSID\{clsid}\Implemented Categories\{CATID_OPCDAServer20}")
        setkey(rf"{prog_id}", description)
        setkey(rf"{prog_id}\CLSID", clsid)
        setkey(rf"{prog_id}\OPC\Vendor", "Botond's Industrial Data Protocol Simulator")

    try:
        write(winreg.HKEY_LOCAL_MACHINE, "Software\\Classes\\")
        return "machine"
    except PermissionError:
        write(winreg.HKEY_CURRENT_USER, "Software\\Classes\\")
        return "user"


def unregister_server(prog_id: str) -> None:
    import winreg
    clsid = clsid_for(prog_id)

    def delete_tree(root, path):
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS | winreg.KEY_WOW64_64KEY) as k:
                while True:
                    try:
                        sub = winreg.EnumKey(k, 0)
                    except OSError:
                        break
                    delete_tree(root, path + "\\" + sub)
            winreg.DeleteKeyEx(root, path, winreg.KEY_WOW64_64KEY)
        except OSError:
            pass

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        delete_tree(root, rf"Software\Classes\CLSID\{clsid}")
        delete_tree(root, rf"Software\Classes\{prog_id}")


# ====================================================================== data model
class Runtime:
    """Shared state of one OPC DA server process (address space + all client objects)."""

    def __init__(self, name: str, interval_s: float, tag_groups: list[dict]):
        self.name = name
        self.interval = interval_s
        self.tagsets = [TagSet(TagSpec(**g)) for g in tag_groups]
        self.stamp = [time.time()] * len(self.tagsets)
        self.items: dict[str, tuple[int, int]] = {}  # upper-case item id -> (group idx, tag idx)
        self.item_names: dict[str, str] = {}  # upper-case -> original spelling
        self.branches: dict[str, list[str]] = {}
        for gi, ts in enumerate(self.tagsets):
            names = ts.names()
            self.branches[ts.spec.name] = names
            for ti, n in enumerate(names):
                iid = f"{ts.spec.name}.{n}"
                self.items[iid.upper()] = (gi, ti)
                self.item_names[iid.upper()] = iid
        self.handles = itertools.count(1)
        self.servers: set = set()
        self.groups: dict[int, object] = {}
        self.started = time.time()
        self.callbacks = 0
        self.reads = 0
        self.last_update = time.time()

    def lookup(self, item_id: str):
        return self.items.get((item_id or "").upper())

    def vt(self, gi: int) -> int:
        return VT_OF[self.tagsets[gi].spec.data_type]

    def value(self, gi: int, ti: int):
        return self.tagsets[gi].values[ti], self.stamp[gi]

    def write(self, gi: int, ti: int, value) -> None:
        self.tagsets[gi].values[ti] = value

    def tick(self) -> None:
        now = time.time()
        for gi, ts in enumerate(self.tagsets):
            ts.tick()
            self.stamp[gi] = now
        self.last_update = now


RT: Runtime | None = None  # one OPC DA server instance per process


# ====================================================================== COM objects
def build_com_classes(clsid: str):
    """Define the COM classes (needs comtypes, imported on the COM thread)."""
    import comtypes
    from comtypes import COMObject, GUID, IUnknown
    from comtypes.automation import VARIANT

    from . import opcda_com as oc

    def herr(arr):
        """c_long array -> HRESULT* for out params (we keep plain ints internally)."""
        return cast(arr, POINTER(comtypes.HRESULT))

    def qi(obj, riid, ppv) -> int:
        return obj.IUnknown_QueryInterface(None, riid, cast(ppv, POINTER(c_void_p)))

    class EnumString(COMObject):
        _com_interfaces_ = [oc.IEnumString]

        def __init__(self, strings: list[str]):
            super().__init__()
            self.strings, self.pos = list(strings), 0

        def IEnumString_Next(self, this, celt, rgelt, pceltFetched):
            n = 0
            while n < celt and self.pos < len(self.strings):
                rgelt[n] = oc.co_alloc_str(self.strings[self.pos])
                n += 1
                self.pos += 1
            if pceltFetched:
                pceltFetched[0] = n
            return oc.S_OK if n == celt else oc.S_FALSE

        def IEnumString_Skip(self, this, celt):
            self.pos = min(len(self.strings), self.pos + celt)
            return oc.S_OK if self.pos < len(self.strings) else oc.S_FALSE

        def IEnumString_Reset(self, this):
            self.pos = 0

        def IEnumString_Clone(self, this, ppenum):
            e = EnumString(self.strings)
            e.pos = self.pos
            return qi(e, ctypes.pointer(oc.IEnumString._iid_), ppenum)

    class ConnectionPoint(COMObject):
        _com_interfaces_ = [oc.IConnectionPoint]

        def __init__(self, container, iface):
            super().__init__()
            self.container, self.iface = container, iface
            self.sinks: dict[int, object] = {}
            self.cookies = itertools.count(1)

        def IConnectionPoint_GetConnectionInterface(self, this, pIID):
            pIID[0] = self.iface._iid_

        def IConnectionPoint_GetConnectionPointContainer(self, this, ppCPC):
            return qi(self.container, ctypes.pointer(oc.IConnectionPointContainer._iid_), ppCPC)

        def IConnectionPoint_Advise(self, this, pUnkSink, pdwCookie):
            if not pUnkSink:
                return oc.E_POINTER
            if self.sinks:
                return oc.CONNECT_E_ADVISELIMIT
            # the [in] pointer is borrowed: take our own reference before wrapping it
            unk = cast(c_void_p(pUnkSink), POINTER(IUnknown))
            unk.AddRef()
            try:
                sink = unk.QueryInterface(self.iface)
            except comtypes.COMError:
                return oc.CONNECT_E_CANNOTCONNECT
            cookie = next(self.cookies)
            self.sinks[cookie] = sink
            pdwCookie[0] = cookie
            log.info("%s: client advised %s", RT.name, self.iface.__name__)

        def IConnectionPoint_Unadvise(self, this, dwCookie):
            if self.sinks.pop(dwCookie, None) is None:
                return oc.CONNECT_E_NOCONNECTION

        def sink(self):
            return next(iter(self.sinks.values()), None)

    # ------------------------------------------------------------------ group
    class Item:
        __slots__ = ("hserver", "hclient", "gi", "ti", "active", "req_vt", "last")

        def __init__(self, hserver, hclient, gi, ti, active, req_vt):
            self.hserver, self.hclient, self.gi, self.ti = hserver, hclient, gi, ti
            self.active, self.req_vt, self.last = active, req_vt, None

    class OpcGroup(COMObject):
        _com_interfaces_ = [oc.IOPCItemMgt, oc.IOPCGroupStateMgt, oc.IOPCSyncIO, oc.IOPCAsyncIO2,
                            oc.IConnectionPointContainer]

        def __init__(self, server, name, active, rate, hclient, deadband, lcid, timebias):
            super().__init__()
            self.server, self.name, self.active, self.rate = server, name, active, rate
            self.hclient, self.deadband, self.lcid, self.timebias = hclient, deadband, lcid, timebias
            self.hserver = next(RT.handles)
            self.items: dict[int, Item] = {}
            self.enabled = True
            self.next_due = 0.0
            self.cp = ConnectionPoint(self, oc.IOPCDataCallback)
            self.pending: list = []  # async operations to complete on the COM thread
            self.trans = itertools.count(1)

        # ---------- helpers
        def _results(self, n):
            return oc.co_alloc_array(ctypes.c_long, n)

        def _resolve(self, defs, n):
            out = []
            for i in range(n):
                d = defs[i]
                loc = RT.lookup(d.szItemID)
                if loc is None:
                    out.append((d, None, oc.OPC_E_UNKNOWNITEMID if d.szItemID else oc.OPC_E_INVALIDITEMID))
                    continue
                vt = RT.vt(loc[0])
                req = d.vtRequestedDataType
                if req not in (oc.VT_EMPTY, vt):
                    test = VARIANT()
                    hr = oc.set_variant(test, RT.value(*loc)[0], vt, req)
                    oc.variant_clear(test)
                    if hr != oc.S_OK:
                        out.append((d, None, oc.OPC_E_BADTYPE))
                        continue
                out.append((d, loc, oc.S_OK))
            return out

        def _fill_results(self, res_ptr, i, loc, hserver):
            r = res_ptr[i]
            r.hServer = hserver
            r.vtCanonicalDataType = RT.vt(loc[0]) if loc else oc.VT_EMPTY
            r.dwAccessRights = oc.OPC_READABLE | oc.OPC_WRITEABLE if loc else 0

        # ---------- IOPCItemMgt
        def IOPCItemMgt_AddItems(self, this, dwCount, pItemArray, ppAddResults, ppErrors):
            results = oc.co_alloc_array(oc.OPCITEMRESULT, dwCount)
            errors = self._results(dwCount)
            failed = False
            for i, (d, loc, hr) in enumerate(self._resolve(pItemArray, dwCount)):
                errors[i] = hr
                if loc is None:
                    failed = True
                    continue
                h = next(RT.handles)
                self.items[h] = Item(h, d.hClient, loc[0], loc[1], bool(d.bActive), d.vtRequestedDataType)
                self._fill_results(results, i, loc, h)
            ppAddResults[0], ppErrors[0] = results, herr(errors)
            self.next_due = 0  # send an initial update for the new items
            return oc.S_FALSE if failed else oc.S_OK

        def IOPCItemMgt_ValidateItems(self, this, dwCount, pItemArray, bBlobUpdate, ppValidationResults, ppErrors):
            results = oc.co_alloc_array(oc.OPCITEMRESULT, dwCount)
            errors = self._results(dwCount)
            failed = False
            for i, (d, loc, hr) in enumerate(self._resolve(pItemArray, dwCount)):
                errors[i] = hr
                failed |= loc is None
                if loc:
                    self._fill_results(results, i, loc, 0)
            ppValidationResults[0], ppErrors[0] = results, herr(errors)
            return oc.S_FALSE if failed else oc.S_OK

        def _per_handle(self, dwCount, phServer, ppErrors, fn):
            errors = self._results(dwCount)
            failed = False
            for i in range(dwCount):
                it = self.items.get(phServer[i])
                if it is None:
                    errors[i] = oc.OPC_E_INVALIDHANDLE
                    failed = True
                else:
                    errors[i] = fn(i, it) or oc.S_OK
                    failed |= errors[i] != oc.S_OK
            ppErrors[0] = herr(errors)
            return oc.S_FALSE if failed else oc.S_OK

        def IOPCItemMgt_RemoveItems(self, this, dwCount, phServer, ppErrors):
            return self._per_handle(dwCount, phServer, ppErrors, lambda i, it: self.items.pop(it.hserver) and None)

        def IOPCItemMgt_SetActiveState(self, this, dwCount, phServer, bActive, ppErrors):
            def f(i, it):
                if bActive and not it.active:
                    it.last = None  # becoming active triggers a callback
                it.active = bool(bActive)
            return self._per_handle(dwCount, phServer, ppErrors, f)

        def IOPCItemMgt_SetClientHandles(self, this, dwCount, phServer, phClient, ppErrors):
            def f(i, it):
                it.hclient = phClient[i]
            return self._per_handle(dwCount, phServer, ppErrors, f)

        def IOPCItemMgt_SetDatatypes(self, this, dwCount, phServer, pRequestedDatatypes, ppErrors):
            def f(i, it):
                req = pRequestedDatatypes[i]
                test = VARIANT()
                hr = oc.set_variant(test, RT.value(it.gi, it.ti)[0], RT.vt(it.gi), req)
                oc.variant_clear(test)
                if hr == oc.S_OK:
                    it.req_vt = req
                return hr
            return self._per_handle(dwCount, phServer, ppErrors, f)

        def IOPCItemMgt_CreateEnumerator(self, this, riid, ppUnk):
            return oc.E_NOTIMPL

        # ---------- IOPCGroupStateMgt
        def IOPCGroupStateMgt_GetState(self, this, pUpdateRate, pActive, ppName, pTimeBias, pPercentDeadband,
                                       pLCID, phClientGroup, phServerGroup):
            pUpdateRate[0], pActive[0] = self.rate, int(self.active)
            ppName[0] = oc.co_alloc_str(self.name)
            pTimeBias[0], pPercentDeadband[0], pLCID[0] = self.timebias, self.deadband, self.lcid
            phClientGroup[0], phServerGroup[0] = self.hclient, self.hserver

        def IOPCGroupStateMgt_SetState(self, this, pRequestedUpdateRate, pRevisedUpdateRate, pActive, pTimeBias,
                                       pPercentDeadband, pLCID, phClientGroup):
            hr = oc.S_OK
            if pRequestedUpdateRate:
                req = pRequestedUpdateRate[0]
                self.rate = max(MIN_UPDATE_MS, req)
                if self.rate != req:
                    hr = oc.OPC_S_UNSUPPORTEDRATE
            if pRevisedUpdateRate:
                pRevisedUpdateRate[0] = self.rate
            if pActive:
                if pActive[0] and not self.active:
                    for it in self.items.values():
                        it.last = None
                    self.next_due = 0
                self.active = bool(pActive[0])
            if pTimeBias:
                self.timebias = pTimeBias[0]
            if pPercentDeadband:
                self.deadband = pPercentDeadband[0]
            if pLCID:
                self.lcid = pLCID[0]
            if phClientGroup:
                self.hclient = phClientGroup[0]
            return hr

        def IOPCGroupStateMgt_SetName(self, this, szName):
            if any(g is not self and g.name == szName for g in self.server.groups.values()):
                return oc.OPC_E_DUPLICATENAME
            self.name = szName

        def IOPCGroupStateMgt_CloneGroup(self, this, szName, riid, ppUnk):
            return oc.E_NOTIMPL

        # ---------- values
        def _state(self, it: Item):
            value, ts = RT.value(it.gi, it.ti)
            return value, ts, oc.OPC_QUALITY_GOOD

        def _fill_variants(self, items, values, qualities, stamps, errors):
            for i, it in enumerate(items):
                value, ts, q = self._state(it)
                errors[i] = oc.set_variant(values[i], value, RT.vt(it.gi), it.req_vt)
                qualities[i] = q
                stamps[i] = oc.filetime_from_unix(ts)

        # ---------- IOPCSyncIO
        def IOPCSyncIO_Read(self, this, dwSource, dwCount, phServer, ppItemValues, ppErrors):
            states = oc.co_alloc_array(oc.OPCITEMSTATE, dwCount)
            errors = self._results(dwCount)
            failed = False
            for i in range(dwCount):
                it = self.items.get(phServer[i])
                if it is None:
                    errors[i], failed = oc.OPC_E_INVALIDHANDLE, True
                    continue
                value, ts, q = self._state(it)
                st = states[i]
                st.hClient, st.wQuality, st.ftTimeStamp = it.hclient, q, oc.filetime_from_unix(ts)
                errors[i] = oc.set_variant(st.vDataValue, value, RT.vt(it.gi), it.req_vt)
                failed |= errors[i] != oc.S_OK
            RT.reads += dwCount
            ppItemValues[0], ppErrors[0] = states, herr(errors)
            return oc.S_FALSE if failed else oc.S_OK

        def _write_one(self, it, v):
            try:
                RT.write(it.gi, it.ti, oc.variant_to_python(v, RT.vt(it.gi)))
                return oc.S_OK
            except OSError:
                return oc.OPC_E_BADTYPE

        def IOPCSyncIO_Write(self, this, dwCount, phServer, pItemValues, ppErrors):
            return self._per_handle(dwCount, phServer, ppErrors, lambda i, it: self._write_one(it, pItemValues[i]))

        # ---------- IOPCAsyncIO2
        def IOPCAsyncIO2_Read(self, this, dwCount, phServer, dwTransactionID, pdwCancelID, ppErrors):
            if self.cp.sink() is None:
                return oc.CONNECT_E_NOCONNECTION
            handles = [phServer[i] for i in range(dwCount)]
            hr = self._per_handle(dwCount, phServer, ppErrors, lambda i, it: None)
            cancel = next(self.trans)
            pdwCancelID[0] = cancel
            self.pending.append(("read", dwTransactionID, [self.items[h] for h in handles if h in self.items]))
            return hr

        def IOPCAsyncIO2_Write(self, this, dwCount, phServer, pItemValues, dwTransactionID, pdwCancelID, ppErrors):
            if self.cp.sink() is None:
                return oc.CONNECT_E_NOCONNECTION
            results = []
            hr = self._per_handle(dwCount, phServer, ppErrors, lambda i, it: None)
            for i in range(dwCount):
                it = self.items.get(phServer[i])
                if it is not None:
                    results.append((it.hclient, self._write_one(it, pItemValues[i])))
            pdwCancelID[0] = next(self.trans)
            self.pending.append(("write", dwTransactionID, results))
            return hr

        def IOPCAsyncIO2_Refresh2(self, this, dwSource, dwTransactionID, pdwCancelID):
            if self.cp.sink() is None:
                return oc.CONNECT_E_NOCONNECTION
            items = [it for it in self.items.values() if it.active]
            if not self.active or not items:
                return oc.E_FAIL
            pdwCancelID[0] = next(self.trans)
            self.pending.append(("refresh", dwTransactionID, items))

        def IOPCAsyncIO2_Cancel2(self, this, dwCancelID):
            return oc.E_FAIL  # operations complete immediately, nothing to cancel

        def IOPCAsyncIO2_SetEnable(self, this, bEnable):
            if self.cp.sink() is None:
                return oc.CONNECT_E_NOCONNECTION
            self.enabled = bool(bEnable)

        def IOPCAsyncIO2_GetEnable(self, this, pbEnable):
            if self.cp.sink() is None:
                return oc.CONNECT_E_NOCONNECTION
            pbEnable[0] = int(self.enabled)

        # ---------- IConnectionPointContainer
        def IConnectionPointContainer_FindConnectionPoint(self, this, riid, ppCP):
            if riid[0] != oc.IOPCDataCallback._iid_:
                return oc.CONNECT_E_NOCONNECTION
            return qi(self.cp, ctypes.pointer(oc.IConnectionPoint._iid_), ppCP)

        def IConnectionPointContainer_EnumConnectionPoints(self, this, ppEnum):
            return oc.E_NOTIMPL

        # ---------- callbacks (called from the COM thread loop)
        def _send(self, method: str, transid: int, items: list) -> None:
            sink = self.cp.sink()
            if sink is None or not items:
                return
            n = len(items)
            handles = (oc.OPCHANDLE * n)(*[it.hclient for it in items])
            values = (VARIANT * n)()
            qualities = (ctypes.c_ushort * n)()
            stamps = (oc.FILETIME * n)()
            codes = [0] * n
            self._fill_variants(items, values, qualities, stamps, codes)
            errors = (comtypes.HRESULT * n)(*codes)
            master_err = oc.S_FALSE if any(codes) else oc.S_OK
            master_q = oc.S_OK if all(q == oc.OPC_QUALITY_GOOD for q in qualities) else oc.S_FALSE
            try:
                getattr(sink, method)(transid, self.hclient, master_q, master_err, n, handles, values, qualities,
                                      stamps, errors)
                RT.callbacks += 1
            except comtypes.COMError as e:
                log.warning("%s: %s callback to client failed (%s); dropping the connection", RT.name, method, e)
                self.cp.sinks.clear()
            finally:
                for v in values:
                    oc.variant_clear(v)

        def service(self, now: float) -> None:
            for kind, transid, payload in self.pending:
                if kind == "write":
                    sink = self.cp.sink()
                    if sink is not None and payload:
                        n = len(payload)
                        try:
                            sink.OnWriteComplete(transid, self.hclient, oc.S_OK, n,
                                                 (oc.OPCHANDLE * n)(*[h for h, _ in payload]),
                                                 (comtypes.HRESULT * n)(*[hr for _, hr in payload]))
                        except comtypes.COMError:
                            self.cp.sinks.clear()
                else:
                    self._send("OnReadComplete" if kind == "read" else "OnDataChange", transid, payload)
                    if kind == "refresh":
                        for it in payload:
                            it.last = RT.value(it.gi, it.ti)[0]
            self.pending.clear()
            if not (self.active and self.enabled) or now < self.next_due or self.cp.sink() is None:
                return
            self.next_due = now + self.rate / 1000
            changed = []
            for it in self.items.values():
                if not it.active:
                    continue
                v = RT.value(it.gi, it.ti)[0]
                if it.last is None or self._exceeds_deadband(it, v):
                    changed.append(it)
                    it.last = v
            self._send("OnDataChange", 0, changed)

        def _exceeds_deadband(self, it, v) -> bool:
            if v == it.last:
                return False
            if not self.deadband or isinstance(v, bool):
                return True
            spec = RT.tagsets[it.gi].spec
            return abs(v - it.last) > (self.deadband / 100.0) * (spec.max - spec.min)

    # ------------------------------------------------------------------ server
    class OpcServer(COMObject):
        _com_interfaces_ = [oc.IOPCServer, oc.IOPCCommon, oc.IOPCBrowseServerAddressSpace, oc.IOPCItemProperties,
                            oc.IConnectionPointContainer]
        _reg_clsid_ = clsid
        _reg_clsctx_ = comtypes.CLSCTX_LOCAL_SERVER

        PROPS = [(1, "Item Canonical DataType", oc.VT_I2), (2, "Item Value", oc.VT_VARIANT),
                 (3, "Item Quality", oc.VT_I2), (4, "Item Timestamp", oc.VT_DATE),
                 (5, "Item Access Rights", oc.VT_I4), (6, "Server Scan Rate", oc.VT_R4)]

        def __init__(self):
            super().__init__()
            self.groups: dict[int, OpcGroup] = {}
            self.lcid = 0x0800
            self.client_name = ""
            self.path: list[str] = []
            self.cp = ConnectionPoint(self, oc.IOPCShutdown)
            RT.servers.add(self)
            log.info("%s: OPC DA client connected (%d connected)", RT.name, len(RT.servers))

        def _final_release_(self):
            RT.servers.discard(self)
            for g in self.groups.values():
                RT.groups.pop(g.hserver, None)
            self.groups.clear()
            log.info("%s: OPC DA client %s disconnected (%d connected)", RT.name, self.client_name or "",
                     len(RT.servers))
            super()._final_release_()

        # ---------- IOPCServer
        def IOPCServer_AddGroup(self, this, szName, bActive, dwRequestedUpdateRate, hClientGroup, pTimeBias,
                                pPercentDeadband, dwLCID, phServerGroup, pRevisedUpdateRate, riid, ppUnk):
            name = szName or ""
            if name and any(g.name == name for g in self.groups.values()):
                return oc.OPC_E_DUPLICATENAME
            if not name:
                k = 1
                while any(g.name == f"Group{k}" for g in self.groups.values()):
                    k += 1
                name = f"Group{k}"
            deadband = pPercentDeadband[0] if pPercentDeadband else 0.0
            if not 0 <= deadband <= 100:
                return oc.E_INVALIDARG
            rate = max(MIN_UPDATE_MS, dwRequestedUpdateRate)
            g = OpcGroup(self, name, bool(bActive), rate, hClientGroup, deadband, dwLCID,
                         pTimeBias[0] if pTimeBias else 0)
            hr = qi(g, riid, ppUnk)
            if hr != oc.S_OK:
                return hr
            self.groups[g.hserver] = g
            RT.groups[g.hserver] = g
            phServerGroup[0] = g.hserver
            pRevisedUpdateRate[0] = rate
            return oc.OPC_S_UNSUPPORTEDRATE if rate != dwRequestedUpdateRate else oc.S_OK

        def IOPCServer_GetErrorString(self, this, dwError, dwLocale, ppString):
            return self.IOPCCommon_GetErrorString(this, dwError, ppString)

        def IOPCServer_GetGroupByName(self, this, szName, riid, ppUnk):
            for g in self.groups.values():
                if g.name == szName:
                    return qi(g, riid, ppUnk)
            return oc.E_INVALIDARG

        def IOPCServer_GetStatus(self, this, ppServerStatus):
            st = oc.co_alloc_array(oc.OPCSERVERSTATUS, 1)
            s = st[0]
            s.ftStartTime = oc.filetime_from_unix(RT.started)
            s.ftCurrentTime = oc.filetime_now()
            s.ftLastUpdateTime = oc.filetime_from_unix(RT.last_update)
            s.dwServerState = oc.OPC_STATUS_RUNNING
            s.dwGroupCount = len(self.groups)
            s.dwBandWidth = 0xFFFFFFFF
            s.wMajorVersion, s.wMinorVersion, s.wBuildNumber = 2, 5, 1
            s.szVendorInfo = cast(oc.co_alloc_str("Botond's Industrial Data Protocol Simulator"), c_void_p).value
            ppServerStatus[0] = st

        def IOPCServer_RemoveGroup(self, this, hServerGroup, bForce):
            g = self.groups.pop(hServerGroup, None)
            if g is None:
                return oc.E_INVALIDARG
            RT.groups.pop(hServerGroup, None)
            g.cp.sinks.clear()

        def IOPCServer_CreateGroupEnumerator(self, this, dwScope, riid, ppUnk):
            return oc.E_NOTIMPL

        # ---------- IOPCCommon
        def IOPCCommon_SetLocaleID(self, this, dwLcid):
            self.lcid = dwLcid

        def IOPCCommon_GetLocaleID(self, this, pdwLcid):
            pdwLcid[0] = self.lcid

        def IOPCCommon_QueryAvailableLocaleIDs(self, this, pdwCount, pdwLcid):
            arr = oc.co_alloc_array(ctypes.c_ulong, 2)
            arr[0], arr[1] = 0x0800, 0x0409
            pdwCount[0], pdwLcid[0] = 2, arr

        def IOPCCommon_GetErrorString(self, this, dwError, ppString):
            text = oc.ERROR_TEXT.get(dwError)
            if text is None:
                try:
                    text = ctypes.FormatError(dwError & 0xFFFFFFFF).strip()
                except Exception:
                    text = ""
                if not text or text.startswith("<no description"):
                    return oc.E_INVALIDARG
            ppString[0] = oc.co_alloc_str(text)

        def IOPCCommon_SetClientName(self, this, szName):
            self.client_name = szName or ""
            log.info("%s: client name '%s'", RT.name, self.client_name)

        # ---------- IOPCBrowseServerAddressSpace
        def IOPCBrowseServerAddressSpace_QueryOrganization(self, this, pNameSpaceType):
            pNameSpaceType[0] = oc.OPC_NS_HIERARCHIAL

        def IOPCBrowseServerAddressSpace_ChangeBrowsePosition(self, this, dwBrowseDirection, szString):
            if dwBrowseDirection == oc.OPC_BROWSE_UP:
                if not self.path:
                    return oc.E_FAIL
                self.path.pop()
            elif dwBrowseDirection == oc.OPC_BROWSE_DOWN:
                if self.path or szString not in RT.branches:
                    return oc.E_INVALIDARG
                self.path = [szString]
            elif dwBrowseDirection == oc.OPC_BROWSE_TO:
                if not szString:
                    self.path = []
                elif szString in RT.branches:
                    self.path = [szString]
                else:
                    return oc.E_INVALIDARG
            else:
                return oc.E_INVALIDARG

        def IOPCBrowseServerAddressSpace_BrowseOPCItemIDs(self, this, dwBrowseFilterType, szFilterCriteria,
                                                         vtDataTypeFilter, dwAccessRightsFilter, ppIEnumString):
            if dwBrowseFilterType == oc.OPC_FLAT:
                groups = [self.path[0]] if self.path else list(RT.branches)
                names = [f"{g}.{t}" for g in groups for t in RT.branches[g]]
            elif dwBrowseFilterType == oc.OPC_BRANCH:
                names = [] if self.path else list(RT.branches)
            elif dwBrowseFilterType == oc.OPC_LEAF:
                names = list(RT.branches[self.path[0]]) if self.path else []
            else:
                return oc.E_INVALIDARG
            if szFilterCriteria:
                names = [n for n in names if fnmatch.fnmatchcase(n.upper(), szFilterCriteria.upper())]
            if dwAccessRightsFilter and not dwAccessRightsFilter & (oc.OPC_READABLE | oc.OPC_WRITEABLE):
                names = []
            hr = qi(EnumString(names), ctypes.pointer(oc.IEnumString._iid_), ppIEnumString)
            if hr != oc.S_OK:
                return hr
            return oc.S_OK if names else oc.S_FALSE

        def IOPCBrowseServerAddressSpace_GetItemID(self, this, szItemDataID, szItemID):
            name = szItemDataID or ""
            if not name:
                full = ".".join(self.path)
            elif RT.lookup(name) is not None or name in RT.branches:
                full = name
            elif self.path and name in RT.branches[self.path[0]]:
                full = f"{self.path[0]}.{name}"
            else:
                return oc.E_INVALIDARG
            szItemID[0] = oc.co_alloc_str(RT.item_names.get(full.upper(), full))

        def IOPCBrowseServerAddressSpace_BrowseAccessPaths(self, this, szItemID, ppIEnumString):
            return oc.E_NOTIMPL

        # ---------- IOPCItemProperties
        def IOPCItemProperties_QueryAvailableProperties(self, this, szItemID, pdwCount, ppPropertyIDs,
                                                         ppDescriptions, ppvtDataTypes):
            if RT.lookup(szItemID) is None:
                return oc.OPC_E_UNKNOWNITEMID
            n = len(self.PROPS)
            ids = oc.co_alloc_array(ctypes.c_ulong, n)
            descs = oc.co_alloc_array(ctypes.c_wchar_p, n)
            vts = oc.co_alloc_array(ctypes.c_ushort, n)
            loc = RT.lookup(szItemID)
            for i, (pid, desc, vt) in enumerate(self.PROPS):
                ids[i] = pid
                cast(descs, POINTER(c_void_p))[i] = cast(oc.co_alloc_str(desc), c_void_p)
                vts[i] = RT.vt(loc[0]) if pid == 2 else vt
            pdwCount[0] = n
            ppPropertyIDs[0] = ids
            ppDescriptions[0] = cast(descs, POINTER(ctypes.c_wchar_p))
            ppvtDataTypes[0] = vts

        def IOPCItemProperties_GetItemProperties(self, this, szItemID, dwCount, pdwPropertyIDs, ppvData, ppErrors):
            loc = RT.lookup(szItemID)
            if loc is None:
                return oc.OPC_E_UNKNOWNITEMID
            data = oc.co_alloc_array(VARIANT, dwCount)
            errors = oc.co_alloc_array(ctypes.c_long, dwCount)
            value, ts = RT.value(*loc)
            failed = False
            for i in range(dwCount):
                pid, v = pdwPropertyIDs[i], data[i]
                if pid == 1:
                    oc.set_variant(v, RT.vt(loc[0]), oc.VT_I2)
                elif pid == 2:
                    oc.set_variant(v, value, RT.vt(loc[0]))
                elif pid == 3:
                    oc.set_variant(v, oc.OPC_QUALITY_GOOD, oc.VT_I2)
                elif pid == 4:
                    v.vt = oc.VT_DATE
                    v._.VT_R8 = oc.vt_date_from_unix(ts)
                elif pid == 5:
                    oc.set_variant(v, oc.OPC_READABLE | oc.OPC_WRITEABLE, oc.VT_I4)
                elif pid == 6:
                    oc.set_variant(v, float(RT.interval * 1000), oc.VT_R4)
                else:
                    errors[i], failed = oc.OPC_E_INVALID_PID, True
            ppvData[0], ppErrors[0] = data, herr(errors)
            return oc.S_FALSE if failed else oc.S_OK

        def IOPCItemProperties_LookupItemIDs(self, this, szItemID, dwCount, pdwPropertyIDs, ppszNewItemIDs,
                                             ppErrors):
            if RT.lookup(szItemID) is None:
                return oc.OPC_E_UNKNOWNITEMID
            ids = oc.co_alloc_array(ctypes.c_wchar_p, dwCount)
            errors = oc.co_alloc_array(ctypes.c_long, dwCount)
            for i in range(dwCount):
                errors[i] = oc.OPC_E_INVALID_PID  # no property has its own item id
            ppszNewItemIDs[0], ppErrors[0] = ids, herr(errors)
            return oc.S_FALSE

        # ---------- IConnectionPointContainer
        def IConnectionPointContainer_FindConnectionPoint(self, this, riid, ppCP):
            if riid[0] != oc.IOPCShutdown._iid_:
                return oc.CONNECT_E_NOCONNECTION
            return qi(self.cp, ctypes.pointer(oc.IConnectionPoint._iid_), ppCP)

        def IConnectionPointContainer_EnumConnectionPoints(self, this, ppEnum):
            return oc.E_NOTIMPL

        def shutdown(self, reason: str) -> None:
            sink = self.cp.sink()
            if sink is not None:
                try:
                    sink.ShutdownRequest(reason)
                except comtypes.COMError:
                    pass

    return OpcServer


# ====================================================================== COM thread
class _ServerLock:
    """Stands in for comtypes' LocalServer lifetime object (we run our own message loop)."""

    def __init__(self):
        self.locks = 0

    def Lock(self):
        self.locks += 1

    def Unlock(self):
        self.locks -= 1


def com_thread_main(rt: Runtime, clsid: str, ready: threading.Event, stop: threading.Event,
                    errors: list, standalone_idle_exit: float | None = None) -> None:
    from ctypes import wintypes
    try:
        import comtypes
        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        from comtypes import COMObject
        from comtypes.server.localserver import ClassFactory
        global RT
        RT = rt
        OpcServer = build_com_classes(clsid)
        COMObject.__server__ = _ServerLock()
        factory = ClassFactory(OpcServer)
    except Exception as e:
        errors.append(e)
        ready.set()
        return
    ready.set()

    user32 = ctypes.windll.user32
    msg = wintypes.MSG()
    QS_ALLINPUT, PM_REMOVE = 0x04FF, 0x0001
    next_tick = time.monotonic()
    idle_since = time.monotonic()
    try:
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_tick:
                t0 = time.monotonic()
                rt.tick()
                rt.cycle.cycles += 1
                rt.cycle.last_ms = (time.monotonic() - t0) * 1000
                rt.cycle.max_ms = max(rt.cycle.max_ms, rt.cycle.last_ms)
                next_tick += rt.interval
                if next_tick < now:
                    rt.cycle.overruns += 1
                    next_tick = now + rt.interval
            for g in list(rt.groups.values()):
                g.service(now)
            if standalone_idle_exit is not None:
                if rt.servers:
                    idle_since = now
                elif now - idle_since > standalone_idle_exit:
                    log.info("%s: no clients for %.0f s, exiting", rt.name, standalone_idle_exit)
                    break
            due = min([next_tick] + [g.next_due for g in rt.groups.values() if g.active] or [next_tick])
            wait_ms = int(max(1, min(50, (due - time.monotonic()) * 1000)))
            user32.MsgWaitForMultipleObjects(0, None, False, wait_ms, QS_ALLINPUT)
            while user32.PeekMessageW(byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(byref(msg))
                user32.DispatchMessageW(byref(msg))
    finally:
        for s in list(rt.servers):
            s.shutdown("Simulator instance stopped")
        factory._revoke_class()
        comtypes.CoUninitialize()


# ====================================================================== simulator instance
class OpcDaSimServer:
    def __init__(self, name: str, prog_id: str, update_interval_ms: int, tag_groups: list[dict],
                 config_path: str | None = None, description: str | None = None):
        if sys.platform != "win32":
            raise RuntimeError("OPC DA needs Windows")
        if not prog_id or " " in prog_id or len(prog_id) > 39:
            raise ValueError("prog_id must be 1-39 characters without spaces, e.g. BIDPS.OpcDaSimulator.1")
        self.name, self.prog_id = name, prog_id
        self.clsid = clsid_for(prog_id)
        self.interval = update_interval_ms / 1000
        self.config_path = config_path
        self.description = description or f"BIDPS OPC DA Simulator ({name})"
        self.rt = Runtime(name, self.interval, tag_groups)
        self.rt.cycle = CycleStats(name, self.interval, log)
        self.ready = asyncio.Event()
        self.scope = None

    def command_line(self) -> str:
        if getattr(sys, "frozen", False):  # packaged build: the .exe itself has a standalone mode
            cmd = f'"{sys.executable}" --opcda-standalone --name "{self.name}"'
        else:
            exe = Path(sys.executable)
            pyw = exe.with_name("pythonw.exe")
            exe = pyw if pyw.exists() else exe
            cmd = f'"{exe}" "{Path(__file__).resolve()}" --name "{self.name}"'
        if self.config_path:
            cmd += f' --config "{Path(self.config_path).resolve()}"'
        return cmd

    def stats(self) -> dict:
        rt = self.rt
        return {**rt.cycle.stats(), "tags": len(rt.items), "clients": len(rt.servers), "groups": len(rt.groups),
                "items_subscribed": sum(len(g.items) for g in rt.groups.values()),
                "callbacks": rt.callbacks, "reads": rt.reads, "prog_id": self.prog_id, "clsid": self.clsid,
                "registered": self.scope, "phase": "running" if self.ready.is_set() else "registering"}

    def tag_map(self) -> list[dict]:
        rows = []
        for ts in self.rt.tagsets:
            for n in ts.names():
                rows.append({"server": self.name, "tag": n, "prog_id": self.prog_id, "clsid": self.clsid,
                             "item_id": f"{ts.spec.name}.{n}", "data_type": ts.spec.data_type})
        return rows

    def start_thread(self, standalone_idle_exit: float | None = None):
        self.scope = register_server(self.prog_id, self.clsid, self.description, self.command_line())
        ready, stop, errors = threading.Event(), threading.Event(), []
        th = threading.Thread(target=com_thread_main, name="opcda-com",
                              args=(self.rt, self.clsid, ready, stop, errors, standalone_idle_exit), daemon=True)
        th.start()
        ready.wait(30)
        if errors:
            raise errors[0]
        log.info("%s: OPC DA server '%s' (CLSID %s, registered for %s) serving %d items, %d ms", self.name,
                 self.prog_id, self.clsid, "all users" if self.scope == "machine" else "the current user",
                 len(self.rt.items), int(self.interval * 1000))
        return th, stop

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        th, stop = await loop.run_in_executor(None, self.start_thread)
        self.ready.set()
        try:
            while th.is_alive():
                await asyncio.sleep(0.5)
            raise RuntimeError("OPC DA COM thread stopped")
        finally:
            stop.set()
            th.join(5)


def _standalone() -> None:
    """Started by COM (LocalServer32) when a client activates the ProgID and the simulator isn't running."""
    import argparse
    import yaml
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    ap.add_argument("--config", default=str(base / "config.yaml"))
    ap.add_argument("-Embedding", "/Embedding", dest="embedding", action="store_true")
    args, _ = ap.parse_known_args()
    logging.basicConfig(level=logging.INFO, filename=str(Path(args.config).with_name("opcda_standalone.log")),
                        format="%(asctime)s %(levelname)-7s %(name)-11s %(message)s")
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    entry = next((c for c in cfg.get("opcda") or [] if c.get("name") == args.name), None)
    if entry is None:
        log.error("no opcda instance named %s in %s", args.name, args.config)
        sys.exit(1)
    kw = {k: v for k, v in entry.items() if k not in ("enabled", "tags")}
    srv = OpcDaSimServer(tag_groups=entry.get("tags", []), config_path=args.config, **kw)
    th, stop = srv.start_thread(standalone_idle_exit=30)
    th.join()


if __name__ == "__main__":
    _standalone()
