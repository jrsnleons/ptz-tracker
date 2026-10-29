# Build with: packaging\build_win.ps1  (Windows only; PyInstaller cannot cross-compile)
import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    # cyndilib ships the NDI runtime DLL; onnxruntime-directml ships DirectML.dll
    binaries=collect_dynamic_libs("cyndilib") + collect_dynamic_libs("onnxruntime"),
    datas=[(os.path.join(ROOT, "models", "head_640_v8s.onnx"), "models")],
    hiddenimports=collect_submodules("cyndilib"),
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PTZ Tracker", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="PTZ Tracker")
