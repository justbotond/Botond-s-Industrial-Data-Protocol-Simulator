"""OPC Data Access 2.05 custom COM interfaces and helpers (ctypes / comtypes declarations).

Interfaces, structures and GUIDs follow the OPC Foundation opcda.idl / opccomn.idl. Methods are
declared without paramflags, so both the server implementation and the client work with raw
ctypes arguments (pointers, out-arrays allocated with CoTaskMemAlloc, ...), exactly as in C++.
"""

from __future__ import annotations

import ctypes
from ctypes import (POINTER, Structure, byref, c_float, c_long, c_ulong, c_ushort, c_void_p, c_wchar_p,
                    cast, sizeof)
from ctypes.wintypes import BOOL, DWORD, FILETIME, LPCWSTR, LPWSTR, WORD

from comtypes import GUID, HRESULT, STDMETHOD, IUnknown
from comtypes.automation import VARIANT

OPCHANDLE = DWORD
VARTYPE = c_ushort
LCID = DWORD
REFIID = POINTER(GUID)

# ------------------------------------------------------------------ constants
OPC_DS_CACHE, OPC_DS_DEVICE = 1, 2
OPC_BRANCH, OPC_LEAF, OPC_FLAT = 1, 2, 3
OPC_NS_HIERARCHIAL, OPC_NS_FLAT = 1, 2
OPC_BROWSE_UP, OPC_BROWSE_DOWN, OPC_BROWSE_TO = 1, 2, 3
OPC_STATUS_RUNNING = 1
OPC_READABLE, OPC_WRITEABLE = 1, 2
OPC_QUALITY_GOOD = 0xC0
OPC_QUALITY_BAD = 0x00

S_OK, S_FALSE = 0, 1
E_FAIL = -2147467259  # 0x80004005
E_INVALIDARG = -2147024809  # 0x80070057
E_NOINTERFACE = -2147467262  # 0x80004002
E_NOTIMPL = -2147467263  # 0x80004001
E_POINTER = -2147467261  # 0x80004003
CONNECT_E_NOCONNECTION = -2147220992  # 0x80040200
CONNECT_E_ADVISELIMIT = -2147220991  # 0x80040201
CONNECT_E_CANNOTCONNECT = -2147220990  # 0x80040202


def _hr(code: int) -> int:
    """unsigned 32-bit HRESULT -> signed int (what ctypes HRESULT expects)."""
    return code - (1 << 32) if code & 0x80000000 else code


OPC_E_INVALIDHANDLE = _hr(0xC0040001)
OPC_E_BADTYPE = _hr(0xC0040004)
OPC_E_PUBLIC = _hr(0xC0040005)
OPC_E_BADRIGHTS = _hr(0xC0040006)
OPC_E_UNKNOWNITEMID = _hr(0xC0040007)
OPC_E_INVALIDITEMID = _hr(0xC0040008)
OPC_E_INVALIDFILTER = _hr(0xC0040009)
OPC_E_UNKNOWNPATH = _hr(0xC004000A)
OPC_E_RANGE = _hr(0xC004000B)
OPC_E_DUPLICATENAME = _hr(0xC004000C)
OPC_S_UNSUPPORTEDRATE = 0x0004000D
OPC_S_CLAMP = 0x0004000E
OPC_E_NOTFOUND = _hr(0xC0040011)
OPC_E_INVALID_PID = _hr(0xC0040203)

ERROR_TEXT = {
    S_OK: "The operation succeeded.",
    S_FALSE: "The operation completed with partial success.",
    E_FAIL: "Unspecified error.",
    E_INVALIDARG: "An argument is invalid.",
    E_NOTIMPL: "Not implemented.",
    E_NOINTERFACE: "No such interface supported.",
    OPC_E_INVALIDHANDLE: "The value of the handle is invalid.",
    OPC_E_BADTYPE: "The server cannot convert the data between the requested data type and the canonical data type.",
    OPC_E_PUBLIC: "The requested operation cannot be done on a public group.",
    OPC_E_BADRIGHTS: "The item's access rights do not allow the operation.",
    OPC_E_UNKNOWNITEMID: "The item is no longer available in the server address space.",
    OPC_E_INVALIDITEMID: "The item definition doesn't conform to the server's syntax.",
    OPC_E_INVALIDFILTER: "The filter string is not valid.",
    OPC_E_UNKNOWNPATH: "The item's access path is not known to the server.",
    OPC_E_RANGE: "The value was out of range.",
    OPC_E_DUPLICATENAME: "Duplicate name not allowed.",
    OPC_S_UNSUPPORTEDRATE: "The server does not support the requested data rate but will use the closest available rate.",
    OPC_S_CLAMP: "A value passed to write was accepted but the output was clamped.",
    OPC_E_NOTFOUND: "The requested object (e.g. a public group) was not found.",
    OPC_E_INVALID_PID: "The specified property ID is not valid for the item.",
}

# VARTYPEs
VT_EMPTY, VT_I2, VT_I4, VT_R4, VT_R8, VT_DATE, VT_BSTR, VT_BOOL = 0, 2, 3, 4, 5, 7, 8, 11
VT_VARIANT, VT_UI2, VT_UI4 = 12, 18, 19

CATID_OPCDAServer10 = "{63D5F430-CFE4-11d1-B2C8-0060083BA1FB}"
CATID_OPCDAServer20 = "{63D5F432-CFE4-11d1-B2C8-0060083BA1FB}"


# ------------------------------------------------------------------ structures
class OPCITEMDEF(Structure):
    _fields_ = [("szAccessPath", LPWSTR), ("szItemID", LPWSTR), ("bActive", BOOL), ("hClient", OPCHANDLE),
                ("dwBlobSize", DWORD), ("pBlob", c_void_p), ("vtRequestedDataType", VARTYPE),
                ("wReserved", WORD)]


class OPCITEMRESULT(Structure):
    _fields_ = [("hServer", OPCHANDLE), ("vtCanonicalDataType", VARTYPE), ("wReserved", WORD),
                ("dwAccessRights", DWORD), ("dwBlobSize", DWORD), ("pBlob", c_void_p)]


class OPCITEMSTATE(Structure):
    _fields_ = [("hClient", OPCHANDLE), ("ftTimeStamp", FILETIME), ("wQuality", WORD), ("wReserved", WORD),
                ("vDataValue", VARIANT)]


class OPCSERVERSTATUS(Structure):
    _fields_ = [("ftStartTime", FILETIME), ("ftCurrentTime", FILETIME), ("ftLastUpdateTime", FILETIME),
                ("dwServerState", DWORD), ("dwGroupCount", DWORD), ("dwBandWidth", DWORD),
                ("wMajorVersion", WORD), ("wMinorVersion", WORD), ("wBuildNumber", WORD), ("wReserved", WORD),
                ("szVendorInfo", c_void_p)]  # raw pointer: an LPWSTR field would read back as str


# ------------------------------------------------------------------ interfaces
class IEnumString(IUnknown):
    _iid_ = GUID("{00000101-0000-0000-C000-000000000046}")


IEnumString._methods_ = [
    STDMETHOD(HRESULT, "Next", [c_ulong, POINTER(LPWSTR), POINTER(c_ulong)]),
    STDMETHOD(HRESULT, "Skip", [c_ulong]),
    STDMETHOD(HRESULT, "Reset", []),
    STDMETHOD(HRESULT, "Clone", [POINTER(POINTER(IEnumString))]),
]


class IOPCCommon(IUnknown):
    _iid_ = GUID("{F31DFDE2-07B6-11D2-B2D8-0060083BA1FB}")
    _methods_ = [
        STDMETHOD(HRESULT, "SetLocaleID", [LCID]),
        STDMETHOD(HRESULT, "GetLocaleID", [POINTER(LCID)]),
        STDMETHOD(HRESULT, "QueryAvailableLocaleIDs", [POINTER(DWORD), POINTER(POINTER(LCID))]),
        STDMETHOD(HRESULT, "GetErrorString", [HRESULT, POINTER(LPWSTR)]),
        STDMETHOD(HRESULT, "SetClientName", [LPCWSTR]),
    ]


class IOPCShutdown(IUnknown):
    _iid_ = GUID("{F31DFDE1-07B6-11D2-B2D8-0060083BA1FB}")
    _methods_ = [STDMETHOD(HRESULT, "ShutdownRequest", [LPCWSTR])]


class IOPCServer(IUnknown):
    _iid_ = GUID("{39C13A4D-011E-11D0-9675-0020AFD8ADB3}")
    _methods_ = [
        STDMETHOD(HRESULT, "AddGroup", [LPCWSTR, BOOL, DWORD, OPCHANDLE, POINTER(c_long), POINTER(c_float), DWORD,
                                        POINTER(OPCHANDLE), POINTER(DWORD), REFIID, POINTER(POINTER(IUnknown))]),
        STDMETHOD(HRESULT, "GetErrorString", [HRESULT, LCID, POINTER(LPWSTR)]),
        STDMETHOD(HRESULT, "GetGroupByName", [LPCWSTR, REFIID, POINTER(POINTER(IUnknown))]),
        STDMETHOD(HRESULT, "GetStatus", [POINTER(POINTER(OPCSERVERSTATUS))]),
        STDMETHOD(HRESULT, "RemoveGroup", [OPCHANDLE, BOOL]),
        STDMETHOD(HRESULT, "CreateGroupEnumerator", [DWORD, REFIID, POINTER(POINTER(IUnknown))]),
    ]


class IOPCBrowseServerAddressSpace(IUnknown):
    _iid_ = GUID("{39C13A4F-011E-11D0-9675-0020AFD8ADB3}")
    _methods_ = [
        STDMETHOD(HRESULT, "QueryOrganization", [POINTER(DWORD)]),
        STDMETHOD(HRESULT, "ChangeBrowsePosition", [DWORD, LPCWSTR]),
        STDMETHOD(HRESULT, "BrowseOPCItemIDs", [DWORD, LPCWSTR, VARTYPE, DWORD, POINTER(POINTER(IEnumString))]),
        STDMETHOD(HRESULT, "GetItemID", [LPWSTR, POINTER(LPWSTR)]),
        STDMETHOD(HRESULT, "BrowseAccessPaths", [LPCWSTR, POINTER(POINTER(IEnumString))]),
    ]


class IOPCItemProperties(IUnknown):
    _iid_ = GUID("{39C13A72-011E-11D0-9675-0020AFD8ADB3}")
    _methods_ = [
        STDMETHOD(HRESULT, "QueryAvailableProperties", [LPWSTR, POINTER(DWORD), POINTER(POINTER(DWORD)),
                                                        POINTER(POINTER(LPWSTR)), POINTER(POINTER(VARTYPE))]),
        STDMETHOD(HRESULT, "GetItemProperties", [LPWSTR, DWORD, POINTER(DWORD), POINTER(POINTER(VARIANT)),
                                                 POINTER(POINTER(HRESULT))]),
        STDMETHOD(HRESULT, "LookupItemIDs", [LPWSTR, DWORD, POINTER(DWORD), POINTER(POINTER(LPWSTR)),
                                             POINTER(POINTER(HRESULT))]),
    ]


class IOPCGroupStateMgt(IUnknown):
    _iid_ = GUID("{39C13A50-011E-11D0-9675-0020AFD8ADB3}")
    _methods_ = [
        STDMETHOD(HRESULT, "GetState", [POINTER(DWORD), POINTER(BOOL), POINTER(LPWSTR), POINTER(c_long),
                                        POINTER(c_float), POINTER(DWORD), POINTER(OPCHANDLE), POINTER(OPCHANDLE)]),
        STDMETHOD(HRESULT, "SetState", [POINTER(DWORD), POINTER(DWORD), POINTER(BOOL), POINTER(c_long),
                                        POINTER(c_float), POINTER(DWORD), POINTER(OPCHANDLE)]),
        STDMETHOD(HRESULT, "SetName", [LPCWSTR]),
        STDMETHOD(HRESULT, "CloneGroup", [LPCWSTR, REFIID, POINTER(POINTER(IUnknown))]),
    ]


class IOPCSyncIO(IUnknown):
    _iid_ = GUID("{39C13A52-011E-11D0-9675-0020AFD8ADB3}")
    _methods_ = [
        STDMETHOD(HRESULT, "Read", [DWORD, DWORD, POINTER(OPCHANDLE), POINTER(POINTER(OPCITEMSTATE)),
                                    POINTER(POINTER(HRESULT))]),
        STDMETHOD(HRESULT, "Write", [DWORD, POINTER(OPCHANDLE), POINTER(VARIANT), POINTER(POINTER(HRESULT))]),
    ]


class IOPCItemMgt(IUnknown):
    _iid_ = GUID("{39C13A54-011E-11D0-9675-0020AFD8ADB3}")
    _methods_ = [
        STDMETHOD(HRESULT, "AddItems", [DWORD, POINTER(OPCITEMDEF), POINTER(POINTER(OPCITEMRESULT)),
                                        POINTER(POINTER(HRESULT))]),
        STDMETHOD(HRESULT, "ValidateItems", [DWORD, POINTER(OPCITEMDEF), BOOL, POINTER(POINTER(OPCITEMRESULT)),
                                             POINTER(POINTER(HRESULT))]),
        STDMETHOD(HRESULT, "RemoveItems", [DWORD, POINTER(OPCHANDLE), POINTER(POINTER(HRESULT))]),
        STDMETHOD(HRESULT, "SetActiveState", [DWORD, POINTER(OPCHANDLE), BOOL, POINTER(POINTER(HRESULT))]),
        STDMETHOD(HRESULT, "SetClientHandles", [DWORD, POINTER(OPCHANDLE), POINTER(OPCHANDLE),
                                                POINTER(POINTER(HRESULT))]),
        STDMETHOD(HRESULT, "SetDatatypes", [DWORD, POINTER(OPCHANDLE), POINTER(VARTYPE), POINTER(POINTER(HRESULT))]),
        STDMETHOD(HRESULT, "CreateEnumerator", [REFIID, POINTER(POINTER(IUnknown))]),
    ]


class IOPCDataCallback(IUnknown):
    _iid_ = GUID("{39C13A70-011E-11D0-9675-0020AFD8ADB3}")
    _methods_ = [
        STDMETHOD(HRESULT, "OnDataChange", [DWORD, OPCHANDLE, HRESULT, HRESULT, DWORD, POINTER(OPCHANDLE),
                                            POINTER(VARIANT), POINTER(WORD), POINTER(FILETIME), POINTER(HRESULT)]),
        STDMETHOD(HRESULT, "OnReadComplete", [DWORD, OPCHANDLE, HRESULT, HRESULT, DWORD, POINTER(OPCHANDLE),
                                              POINTER(VARIANT), POINTER(WORD), POINTER(FILETIME), POINTER(HRESULT)]),
        STDMETHOD(HRESULT, "OnWriteComplete", [DWORD, OPCHANDLE, HRESULT, DWORD, POINTER(OPCHANDLE),
                                               POINTER(HRESULT)]),
        STDMETHOD(HRESULT, "OnCancelComplete", [DWORD, OPCHANDLE]),
    ]


class IOPCAsyncIO2(IUnknown):
    _iid_ = GUID("{39C13A71-011E-11D0-9675-0020AFD8ADB3}")
    _methods_ = [
        STDMETHOD(HRESULT, "Read", [DWORD, POINTER(OPCHANDLE), DWORD, POINTER(DWORD), POINTER(POINTER(HRESULT))]),
        STDMETHOD(HRESULT, "Write", [DWORD, POINTER(OPCHANDLE), POINTER(VARIANT), DWORD, POINTER(DWORD),
                                     POINTER(POINTER(HRESULT))]),
        STDMETHOD(HRESULT, "Refresh2", [DWORD, DWORD, POINTER(DWORD)]),
        STDMETHOD(HRESULT, "Cancel2", [DWORD]),
        STDMETHOD(HRESULT, "SetEnable", [BOOL]),
        STDMETHOD(HRESULT, "GetEnable", [POINTER(BOOL)]),
    ]


class IConnectionPoint(IUnknown):
    _iid_ = GUID("{B196B286-BAB4-101A-B69C-00AA00341D07}")


class IConnectionPointContainer(IUnknown):
    _iid_ = GUID("{B196B284-BAB4-101A-B69C-00AA00341D07}")


IConnectionPoint._methods_ = [
    STDMETHOD(HRESULT, "GetConnectionInterface", [POINTER(GUID)]),
    STDMETHOD(HRESULT, "GetConnectionPointContainer", [POINTER(POINTER(IConnectionPointContainer))]),
    # sink passed as a raw address: the receiver AddRefs explicitly (no borrowed-wrapper Release)
    STDMETHOD(HRESULT, "Advise", [c_void_p, POINTER(DWORD)]),
    STDMETHOD(HRESULT, "Unadvise", [DWORD]),
    STDMETHOD(HRESULT, "EnumConnections", [POINTER(POINTER(IUnknown))]),
]
IConnectionPointContainer._methods_ = [
    STDMETHOD(HRESULT, "EnumConnectionPoints", [POINTER(POINTER(IUnknown))]),
    STDMETHOD(HRESULT, "FindConnectionPoint", [REFIID, POINTER(POINTER(IConnectionPoint))]),
]


# ------------------------------------------------------------------ memory helpers
_ole32 = ctypes.WinDLL("ole32")  # private handle: our restypes must not leak into comtypes
_ole32.CoTaskMemAlloc.restype = c_void_p
_ole32.CoTaskMemAlloc.argtypes = [ctypes.c_size_t]
_ole32.CoTaskMemFree.restype = None
_ole32.CoTaskMemFree.argtypes = [c_void_p]
_oleaut32 = ctypes.OleDLL("oleaut32")  # OleDLL: failing HRESULTs raise OSError
_oleaut32.VariantChangeType.argtypes = [POINTER(VARIANT), POINTER(VARIANT), c_ushort, VARTYPE]
_oleaut32.VariantClear.argtypes = [POINTER(VARIANT)]
_oleaut32.VariantCopy.argtypes = [POINTER(VARIANT), POINTER(VARIANT)]


def co_alloc_array(ctype, n: int):
    """Zeroed CoTaskMemAlloc'ed array of n ctype elements (caller of the COM method frees it)."""
    size = max(1, n) * sizeof(ctype)
    mem = _ole32.CoTaskMemAlloc(size)
    if not mem:
        raise MemoryError("CoTaskMemAlloc failed")
    ctypes.memset(mem, 0, size)
    return cast(mem, POINTER(ctype))


def co_alloc_str(s: str) -> LPWSTR:
    buf = ctypes.create_unicode_buffer(s)
    mem = _ole32.CoTaskMemAlloc(sizeof(buf))
    ctypes.memmove(mem, buf, sizeof(buf))
    return cast(mem, LPWSTR)


def co_free(ptr) -> None:
    if ptr:
        _ole32.CoTaskMemFree(cast(ptr, c_void_p))


def variant_clear(v: VARIANT) -> None:
    _oleaut32.VariantClear(byref(v))


VT_FIELD = {VT_R4: "VT_R4", VT_R8: "VT_R8", VT_I2: "VT_I2", VT_UI2: "VT_UI2", VT_I4: "VT_I4", VT_UI4: "VT_UI4"}


def set_variant(dst: VARIANT, value, vt: int, requested: int = VT_EMPTY) -> int:
    """Write value with canonical type vt into dst (which must be empty/zeroed), converting to
    `requested` if given. Returns an HRESULT (S_OK or OPC_E_BADTYPE)."""
    if vt == VT_BOOL:
        dst.vt = VT_BOOL
        dst._.VT_BOOL = -1 if value else 0
    else:
        dst.vt = vt
        setattr(dst._, VT_FIELD[vt], value)
    if requested not in (VT_EMPTY, vt):
        tmp = VARIANT()
        try:
            _oleaut32.VariantChangeType(byref(tmp), byref(dst), 0, requested)
        except OSError:
            return OPC_E_BADTYPE
        variant_clear(dst)
        ctypes.memmove(byref(dst), byref(tmp), sizeof(VARIANT))  # move ownership, don't clear tmp
        ctypes.memset(byref(tmp), 0, sizeof(VARIANT))
    return S_OK


def variant_to_python(v: VARIANT, vt: int):
    """Convert a client-supplied VARIANT to a python value of canonical type vt."""
    tmp = VARIANT()
    _oleaut32.VariantChangeType(byref(tmp), byref(v), 0, vt)  # raises OSError on bad type
    try:
        if vt == VT_BOOL:
            return bool(tmp._.VT_BOOL)
        return getattr(tmp._, VT_FIELD[vt])
    finally:
        variant_clear(tmp)


def variant_value(v: VARIANT):
    """Python value of an arbitrary VARIANT (client side)."""
    try:
        return v.value
    except Exception:
        return None


def filetime_now() -> FILETIME:
    import time
    t = int((time.time() + 11644473600) * 10_000_000)
    return FILETIME(t & 0xFFFFFFFF, t >> 32)


def filetime_from_unix(ts: float) -> FILETIME:
    t = int((ts + 11644473600) * 10_000_000)
    return FILETIME(t & 0xFFFFFFFF, t >> 32)


def filetime_to_unix(ft: FILETIME) -> float:
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 10_000_000 - 11644473600


def vt_date_from_unix(ts: float) -> float:
    return ts / 86400.0 + 25569.0  # OLE automation date


QUALITY_TEXT = {0xC0: "Good", 0x00: "Bad", 0x04: "Bad - config error", 0x08: "Bad - not connected",
                0x18: "Bad - comm failure", 0x40: "Uncertain", 0xD8: "Good - local override"}
