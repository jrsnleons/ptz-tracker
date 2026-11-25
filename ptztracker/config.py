"""The settings file: defaults, load and save."""
import json
import logging
import os
from dataclasses import asdict


from . import motion, platform_paths
from .stage import StageSettings
from .tracking import TrackSettings

log = logging.getLogger("ptztracker")


CONFIG_DIR = platform_paths.config_dir()
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "video": "NDI",
    "ndi_name": "",
    "ndi_high_bandwidth": False,
    "rtsp_url": "rtsp://192.168.0.80/stream1",
    "webcam_index": 0,
    "control": "Canon API",
    "camera_ip": "192.168.254.122",  # LWNRA-PTZ_CAM (Canon CR-N300) on the church network
    "camera_user": "",  # the camera's web login, for the Canon API
    "camera_password": "",
    "visca_port": 52381,
    "visca_raw": False,
    "zoom_scaled_speed": True,  # slow pan/tilt down when zoomed in
    "keys": {},  # action -> [keys]; missing actions use keys.DEFAULTS
    "key_layouts": {},  # name -> saved "keys" dict
    "preset_names": {},  # "3": "Pulpit"; the camera's own names show when there is none
    "preset_speeds": {},  # "3": 40; recall speed in camera units (Canon API), else the camera's saved speed
    "picture": {},  # camera -> last picture values sent
    "looks": {},  # name -> picture values
    "update_auto": True,
    "update_skip": "",
    "auto_pick": True,
    "tracking": asdict(TrackSettings()),
    "stage": asdict(StageSettings()),
    "remote": {},  # phone/tablet remote control; remote_ui.DEFAULTS fills it in
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:  # -sig: Notepad/PowerShell add a BOM
            saved = json.load(f)
        tracking = {**cfg["tracking"], **saved.pop("tracking", {})}
        stage = {**cfg["stage"], **saved.pop("stage", {})}
        remote = saved.pop("remote", {})
        cfg.update(saved)
        cfg["tracking"] = tracking
        cfg["stage"] = stage
        cfg["remote"] = remote if isinstance(remote, dict) else {}
    except (OSError, ValueError):
        pass
    return motion.migrate_speed(cfg)


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
