# Build with: packaging/build_app.sh
import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    binaries=collect_dynamic_libs("cyndilib"),
    # detector.default_model_path() looks in ../models next to the package
    datas=[(os.path.join(ROOT, "models", "head_640_v8s.onnx"), "models")],
    hiddenimports=collect_submodules("cyndilib"),
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PTZ Tracker", console=False, target_arch="arm64")
coll = COLLECT(exe, a.binaries, a.datas, name="PTZ Tracker")
app = BUNDLE(
    coll,
    name="PTZ Tracker.app",
    bundle_identifier="org.lwnra.ptztracker",
    info_plist={
        "CFBundleShortVersionString": "1.0",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSLocalNetworkUsageDescription": "PTZ Tracker finds NDI sources and controls the PTZ camera on your network.",
        "NSBonjourServices": ["_ndi._tcp"],
        "NSCameraUsageDescription": "PTZ Tracker can track from a local camera.",
    },
)
