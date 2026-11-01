# Build with: packaging\build_win.ps1  (Windows only; PyInstaller cannot cross-compile)
import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
VERSION = os.environ.get("PTZ_VERSION", "1.1.1")
nums = tuple(int(x) for x in (VERSION.split(".") + ["0"] * 4)[:4])

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=nums, prodvers=nums),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "LWNRA"),
            StringStruct("FileDescription", "PTZ Tracker"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "PTZ Tracker"),
            StringStruct("OriginalFilename", "PTZ Tracker.exe"),
            StringStruct("ProductName", "PTZ Tracker"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)  # fmt: skip

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    # cyndilib ships the NDI runtime DLL; onnxruntime-directml ships DirectML.dll
    binaries=collect_dynamic_libs("cyndilib") + collect_dynamic_libs("onnxruntime"),
    datas=[
        (os.path.join(ROOT, "models", "head_640_v8s.onnx"), "models"),
        (os.path.join(ROOT, "assets", "icon.png"), "assets"),
        (os.path.join(ROOT, "assets", "icon.ico"), "assets"),
    ],
    hiddenimports=collect_submodules("cyndilib"),
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PTZ Tracker",
    console=False,
    icon=os.path.join(ROOT, "assets", "icon.ico"),
    version=version_info,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PTZ Tracker")
