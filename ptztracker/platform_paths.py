"""Per-OS locations for settings, logs and bundled files, plus the camera backend."""
import os
import sys

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
APP_NAME = "PTZ Tracker"


def config_dir():
    if IS_WIN:
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
    if IS_MAC:
        return os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), APP_NAME)


def log_dir():
    if IS_WIN:
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME, "Logs")
    if IS_MAC:
        return os.path.expanduser(f"~/Library/Logs/{APP_NAME}")
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), APP_NAME)


def bundled_models_dir():
    """Folder holding head_640_v8s.onnx."""
    if getattr(sys, "frozen", False):
        if IS_MAC:  # inside the .app the model sits in Contents/Resources
            return os.path.join(os.path.dirname(sys.executable), "..", "Resources", "models")
        return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "models")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models")


def webcam_backend():
    import cv2

    if IS_MAC:
        return cv2.CAP_AVFOUNDATION
    if IS_WIN:
        return cv2.CAP_DSHOW
    return cv2.CAP_ANY


def onnx_providers():
    """Best execution providers first; CPU always last. Filtered to what this onnxruntime build has."""
    if IS_MAC:
        wanted = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    elif IS_WIN:  # DirectML runs on any DX12 GPU (NVIDIA / AMD / Intel)
        wanted = ["DmlExecutionProvider", "CPUExecutionProvider"]
    else:
        wanted = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    import onnxruntime as ort

    have = set(ort.get_available_providers())
    return [p for p in wanted if p in have] or ["CPUExecutionProvider"]
