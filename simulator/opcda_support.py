"""OPC DA (classic, COM/DCOM) prerequisite checks and the built-in OPC DA test client."""

from __future__ import annotations

import concurrent.futures
import struct
import sys

IID_IOPCServer = "{39C13A4D-011E-11D0-9675-0020AFD8ADB3}"
CLSID_OPCServerList = "{13486D51-4821-11D2-A494-3CB306C10000}"  # OPCEnum


def _interface_registered(iid: str, view: int) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"Interface\{iid}", 0, winreg.KEY_READ | view):
            return True
    except OSError:
        return False


def opcda_status() -> dict:
    """What is needed for OPC DA on this machine, and what is missing."""
    if sys.platform != "win32":
        return {"supported": False, "ready": False, "message": "OPC DA needs Windows (COM/DCOM)."}
    import winreg
    bits = struct.calcsize("P") * 8
    view = winreg.KEY_WOW64_64KEY if bits == 64 else winreg.KEY_WOW64_32KEY
    proxies = _interface_registered(IID_IOPCServer, view)
    proxies_other = _interface_registered(IID_IOPCServer, winreg.KEY_WOW64_32KEY if bits == 64
                                          else winreg.KEY_WOW64_64KEY)
    try:
        import comtypes  # noqa: F401
        comtypes_ok = True
    except ImportError:
        comtypes_ok = False
    missing = []
    if not comtypes_ok:
        missing.append("Python package 'comtypes' (pip install comtypes)")
    if not proxies:
        missing.append(f"{bits}-bit OPC Core Components (OPC Foundation proxy/stub DLLs) - "
                       + ("only the 32-bit version is installed" if proxies_other else "not installed"))
    return {"supported": True, "ready": not missing, "python_bits": bits, "proxies_registered": proxies,
            "proxies_other_bitness": proxies_other, "comtypes": comtypes_ok, "missing": missing,
            "message": "ready" if not missing else "missing: " + "; ".join(missing)}


from .opcda_client import OpcDaClient  # noqa: E402,F401  (re-export for the manager)
