# PyInstaller spec for Botond's Industrial Data Protocol Simulator.
# Build with:  .\build.ps1   (or: .venv\Scripts\pyinstaller simulator.spec --noconfirm)
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("simulator/web", "simulator/web")]
binaries = []
hiddenimports = collect_submodules("simulator")
# libraries with dynamic imports / data files
for pkg in ("asyncua", "bacpypes3", "comtypes", "aiohttp"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["run_simulator.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BotondsProtocolSimulator",
    console=True,  # shows the log; close the window or press Ctrl+C to stop everything
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="BotondsProtocolSimulator")
