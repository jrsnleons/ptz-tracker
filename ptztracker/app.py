"""PTZ Tracker — auto head tracking + manual PTZ control in one window (macOS and Windows)."""
import faulthandler
import logging
import os
import sys
import threading
import time
from dataclasses import asdict, fields

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import canon_http, keys, motion, picture, platform_paths
from .remote_ui import RemoteControl
from . import ptz as ptzmod
from .detector import HeadDetector, default_model_path
from .sources import HAVE_NDI, CVSource, NDIFinder, NDISource
from .stage import StageBounds, StageSettings
from .tracking import TrackSettings
from .config import load_config, save_config
from .video_view import VideoView
from .picture_panel import CanonPicturePanel
from .update_ui import UpdateMixin
from .widgets import MOD, AMBER, GREEN, INK2, INK3, KeyButton, PANEL, STYLE, TrackButton, dark_palette, dot_icon, hold_button, label, nofocus, resource_path, section, setting, slider
from .worker import TrackerWorker


log = logging.getLogger("ptztracker")

APP_LOG = os.path.join(platform_paths.log_dir(), "app.log")
NO_VIDEO_HINT = 6.0  # connected this long with no picture -> say what to check


# --------------------------------------------------------------------------- main window

TRACK_SLIDERS = {
    # attr: (title, help, min, max)
    "target_x": ("Face position, across", "Where the head sits left to right in the shot", 0.1, 0.9),
    "target_y": ("Face position, height", "Lower numbers keep more headroom", 0.1, 0.9),
    "target_height": ("Head size", "How big the head is when zoom follows", 0.03, 0.5),
    "ignore_below": ("Ignore heads below", "The audience zone, shaded on the video", 0.3, 1.0),
    "speed": ("Responsiveness", "How hard the camera chases the speaker", 0.1, 3.0),
    "max_speed": ("Top speed", "Caps how fast the camera can move", 0.05, 1.0),
    "deadzone_x": ("Still zone, width", "No pan while the head stays inside the dashed box", 0.0, 0.3),
    "deadzone_y": ("Still zone, height", "No tilt while the head stays inside the dashed box", 0.0, 0.3),
    "exponent": ("Ease near centre", "Higher is gentler for small moves", 1.0, 3.0),
    "smoothing": ("Smoothing", "Higher hides jitter but adds a little lag", 0.05, 1.0),
}
LOCKS = (("Don't pan (left/right)", "lock_horizontal"), ("Don't tilt (up/down)", "lock_vertical"), ("Don't zoom", "lock_zoom"))


class MainWindow(UpdateMixin, QMainWindow):
    canon_state = Signal(dict)  # the camera's info list: presets, picture values (from the Canon client's thread)
    update_checked = Signal(object, str, bool)  # release or None, error, was it a manual check
    update_progress = Signal(int)
    update_ready = Signal(str, str)  # downloaded file, error

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PTZ Tracker")
        self.cfg = load_config()
        self.settings = TrackSettings(
            **{k: v for k, v in self.cfg["tracking"].items() if k in {f.name for f in fields(TrackSettings)}}
        )
        self.stage = StageSettings(
            **{k: v for k, v in self.cfg["stage"].items() if k in {f.name for f in fields(StageSettings)}}
        )
        self.finder = NDIFinder() if HAVE_NDI else None
        self.source = None
        self.poller = None
        self.cam = ptzmod.PTZBase()
        self.held = {}  # manual direction keys/buttons currently held -> (pan, tilt)
        self.zoom_held = 0
        self.store_mode = False
        self.binds = keys.Binds(self.cfg.get("keys"))
        if not isinstance(self.cfg.get("key_layouts"), dict):
            self.cfg["key_layouts"] = {}
        self._relabels = []  # callables that rewrite key hints on buttons after a bind change
        self.pt_step = self.cfg["pt_step"]  # pan/tilt speed step 1-16, as in Canon's Remote Camera Control Application
        self.pan_speed = motion.pt_speed(self.pt_step)
        self.zoom_speed = self.cfg["zoom_speed"]  # 0.01-1, shown as 1-100 %
        self.ramp = motion.Ramp()
        self._ramp_t = 0.0
        self._sent_pt = (0.0, 0.0)
        self._widest_hfov = 0.0
        self.ramp_timer = QTimer(self, interval=33, timeout=self._manual_tick)  # ~30 Hz, only runs while moving
        self.bank = 0  # preset bank the number keys address: 0 = slots 1-10, 1 = 11-20
        self.pic = picture.clean(None)
        self.pic_key = ""
        self.pic_dirty = set()
        self.pic_timer = QTimer(self, singleShot=True, interval=100, timeout=self._pic_flush)
        self.canon_api = None  # Canon web API (presets, picture, position) whenever the Camera IP is set
        self.canon_info = {}
        self.release = None  # newer Release found by the update check
        self._checking = False
        self._updating = False
        self._perf_at = 0.0
        self._connected_at = 0.0

        self.detector = HeadDetector(default_model_path())
        self.worker = TrackerWorker(self.detector, self.settings, self.stage)
        self.worker.auto_pick = self.cfg["auto_pick"]

        self.view = VideoView(self.settings)
        self.view.clicked.connect(self.worker.click)
        self.view.drag_move.connect(lambda pan, tilt: self.manual_press("mouse", pan, tilt))
        self.view.drag_end.connect(lambda: self.manual_release("mouse"))
        self.view.wheel_zoom.connect(self._wheel_zoom)
        self.worker.frame_ready.connect(self._show_frame)
        self.worker.error.connect(self._status)
        self.worker.stage_event.connect(self._status)
        self.worker.tracking_stopped.connect(lambda: self.set_tracking(False))
        self.view.rect_drawn.connect(self.stage_drawn)
        self.view.bounds_edited.connect(self._bounds_edited)
        self.view.bounds_edit_done.connect(self._bounds_edit_done)

        self.canon_state.connect(self._canon_update)
        self.update_checked.connect(self._update_checked)
        self.update_progress.connect(self._update_progress)
        self.update_ready.connect(self._update_ready)
        self.remote = RemoteControl(self)
        self.settings_dialog = self._settings_dialog()  # builds the camera options the connect card refers to

        # video with the connect card floating over it until a camera is connected
        video = QWidget()
        vg = QGridLayout(video)
        vg.setContentsMargins(16, 16, 16, 8)
        vg.addWidget(self.view, 0, 0)
        self.connect_card = self._connect_card()
        vg.addWidget(self.connect_card, 0, 0, Qt.AlignCenter)
        tips = label("Click a head to follow it    Drag to move the camera    Scroll to zoom", "hint")
        tips.setContentsMargins(16, 0, 16, 12)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(0)
        left.addWidget(video, 1)
        left.addWidget(tips)

        self.rail = self._rail()
        scroll = QScrollArea()
        scroll.setObjectName("railScroll")
        scroll.setWidget(self.rail)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(360)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addLayout(left, 1)
        body.addWidget(scroll)

        central = QWidget()
        central.setObjectName("central")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._topbar())
        outer.addWidget(self._update_bar())
        outer.addLayout(body, 1)
        self.setCentralWidget(central)

        self.stage_status = label("", "muted")
        self.statusBar().addPermanentWidget(self.stage_status)
        self.statusBar().setSizeGripEnabled(False)
        self.resize(1440, 900)

        self._set_connected(False)
        self.set_tracking(False)
        self.set_speeds(self.pt_step, self.zoom_speed)
        self._relabel_all()
        self.status_timer = QTimer(self, interval=1000, timeout=self._refresh_status)
        self.status_timer.start()
        if self.finder:
            QTimer.singleShot(1500, self._refresh_ndi)
        if not os.environ.get("PTZ_NO_UPDATE_CHECK"):
            QTimer.singleShot(10_000, self._auto_check)  # not at launch, so it never slows startup
            QTimer(self, interval=6 * 3600 * 1000, timeout=self._auto_check).start()
        self.worker.start()
        self.statusBar().showMessage(f"Detector ready on {self.detector.provider.replace('ExecutionProvider', '')}")
        if self.cfg["remote"]["enabled"]:
            self.remote.start()
        QApplication.instance().installEventFilter(self)

    def _status(self, msg):
        self.statusBar().showMessage(msg)

    def _show_frame(self, data):
        try:
            self.view.show_data(data)
            self.view.repaint()
        except Exception:
            log.exception("could not draw a frame")
        finally:
            self.worker.gui_busy = False
        self.remote.frame(data)
        now = time.monotonic()
        if now - self._perf_at > 0.5:
            self._perf_at = now
            det_ms = data.get("timing", (0, 0, 0))[0]
            self.perf.setText(f"{data['fps']:.0f} fps · detect {det_ms:.0f} ms")

    # ---------------- top bar
    def _topbar(self):
        bar = QWidget()
        bar.setObjectName("topbar")
        bar.setFixedHeight(48)
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 0, 16, 0)
        self.chip = nofocus(QPushButton("Not connected"))
        self.chip.setObjectName("chip")
        menu = QMenu(self.chip)
        menu.addAction("Disconnect", self.disconnect_camera)
        menu.addAction("Camera settings…", lambda: self.open_settings(0))
        self.chip.setMenu(menu)
        h.addWidget(self.chip)
        h.addStretch(1)
        self.perf = label("", "perf")
        h.addWidget(self.perf)
        h.addSpacing(12)
        gear = nofocus(QPushButton("Settings"))
        gear.setToolTip(f"Settings ({MOD}+,)" if MOD == "Ctrl" else "Settings (⌘,)")
        gear.clicked.connect(lambda: self.open_settings())
        h.addWidget(gear)
        return bar

    def _set_connected(self, on):
        self.connect_card.setVisible(not on)
        self.rail.setEnabled(on)
        self.view.placeholder = "Waiting for video…" if on else ""
        if on:
            ctrl = self.control_kind.currentText()
            where = f" · {ctrl} {self.ip_edit.text().strip()}" if ctrl in ("VISCA over IP", "Canon API") else f" · {ctrl}"
            self.chip.setText(f"{self.source.name}{where}")
            self.chip.setIcon(dot_icon(GREEN))
            if QApplication.focusWidget():
                QApplication.focusWidget().clearFocus()  # so the camera keys work straight away
        else:
            self.chip.setText("Not connected")
            self.chip.setIcon(dot_icon("#5A5D63"))
        self.view.update()

    # ---------------- connection
    def _connect_card(self):
        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(440)
        v = QVBoxLayout(card)
        v.setContentsMargins(24, 24, 24, 24)
        v.setSpacing(14)
        v.addWidget(label("Connect to the camera", "cardTitle"))
        v.addWidget(label("Pick the video feed and how to steer the camera. This is remembered for next time.", "muted", wrap=True))

        self.video_kind = QComboBox()
        self.video_kind.addItems(["NDI", "RTSP", "Webcam"])
        self.video_kind.setCurrentText(self.cfg["video"])
        self.video_stack = QStackedWidget()
        ndi_row = QWidget()
        nl = QHBoxLayout(ndi_row)
        nl.setContentsMargins(0, 0, 0, 0)
        self.ndi_combo = QComboBox()
        self.ndi_combo.setEditable(True)
        self.ndi_combo.setCurrentText(self.cfg["ndi_name"])
        refresh = nofocus(QPushButton("Refresh"))
        refresh.setToolTip("Look for NDI sources again")
        refresh.clicked.connect(self._refresh_ndi)
        nl.addWidget(self.ndi_combo, 1)
        nl.addWidget(refresh)
        self.rtsp_edit = QLineEdit(self.cfg["rtsp_url"])
        self.webcam_spin = QSpinBox()
        self.webcam_spin.setRange(0, 9)
        self.webcam_spin.setValue(self.cfg["webcam_index"])
        for w in (ndi_row, self.rtsp_edit, self.webcam_spin):
            self.video_stack.addWidget(w)
        self.video_kind.currentIndexChanged.connect(self.video_stack.setCurrentIndex)
        self.video_stack.setCurrentIndex(self.video_kind.currentIndex())
        row = QHBoxLayout()
        row.addWidget(self.video_kind)
        row.addWidget(self.video_stack, 1)
        v.addWidget(label("Video", "section"))
        v.addLayout(row)

        self.control_kind = QComboBox()
        self.control_kind.addItems(["Canon API", "NDI PTZ", "VISCA over IP", "None"])
        self.control_kind.setCurrentText(self.cfg["control"])
        self.ip_edit = QLineEdit(self.cfg["camera_ip"])
        self.ip_edit.setPlaceholderText("Camera IP address")
        row = QHBoxLayout()
        row.addWidget(self.control_kind)
        row.addWidget(self.ip_edit, 1)
        v.addWidget(label("Camera control", "section"))
        v.addLayout(row)
        v.addWidget(label("The Camera IP also reads presets, picture settings and position. Login, port and NDI quality are in Settings.", "hint", wrap=True))

        go = nofocus(QPushButton("Connect"))
        go.setObjectName("primary")
        go.clicked.connect(self.connect_camera)
        v.addWidget(go)
        self.card_status = label("", "muted", wrap=True)
        v.addWidget(self.card_status)
        return card

    def _refresh_ndi(self):
        if not self.finder:
            self.card_status.setText("NDI isn't available in this build")
            return
        current = self.ndi_combo.currentText()
        names = self.finder.names()
        self.ndi_combo.clear()
        self.ndi_combo.addItems(names)
        if current:
            self.ndi_combo.setCurrentText(current)
        msg = f"Found {len(names)} NDI source{'s' if len(names) != 1 else ''} on the network"
        if not names and platform_paths.IS_WIN:
            msg += ". If the camera is on, allow PTZ Tracker through Windows Firewall, or type its name above."
        self.card_status.setText(msg)

    def connect_camera(self):
        self.disconnect_camera()
        kind = self.video_kind.currentText()
        try:
            if kind == "NDI":
                if not self.finder:
                    raise RuntimeError("NDI support is not installed")
                self.source = NDISource(self.finder, self.ndi_combo.currentText(), self.ndi_hq.isChecked())
            elif kind == "RTSP":
                self.source = CVSource(self.rtsp_edit.text().strip())
            else:
                self.source = CVSource(self.webcam_spin.value())

            ip = self.ip_edit.text().strip()
            if ip:
                self.poller = canon_http.CanonClient(ip, self.user_edit.text().strip(), self.pass_edit.text(), on_state=self.canon_state.emit)
                self.canon_api = canon_http.CanonAPI(self.poller, zoom_linked=lambda: self.cfg["zoom_scaled_speed"])
            ctrl = self.control_kind.currentText()
            if ctrl == "Canon API":
                if self.canon_api is None:
                    raise RuntimeError("Canon API control needs the Camera IP")
                self.cam = ptzmod.Guarded(self.canon_api)
            elif ctrl == "VISCA over IP":
                self.cam = ptzmod.Guarded(ptzmod.VISCAoverIP(self.ip_edit.text().strip(), self.port_spin.value(), self.raw_check.isChecked()))
            elif ctrl == "NDI PTZ":
                if not isinstance(self.source, NDISource):
                    raise RuntimeError("NDI PTZ needs the video source to be NDI")
                self.cam = ptzmod.Guarded(ptzmod.NDIPTZ(self.source))
            else:
                self.cam = ptzmod.PTZBase()
        except Exception as e:
            self.disconnect_camera()
            QMessageBox.warning(self, "Couldn't connect", str(e))
            return
        self.worker.set_io(self.source, self.cam, self.poller)
        self._widest_hfov = 0.0
        self._picture_connected(ip)
        self._save()
        self._set_connected(True)
        self._connected_at = time.monotonic()
        self.statusBar().showMessage(f"Connected to {self.source.name}")

    def disconnect_camera(self):
        self.set_tracking(False)
        self._manual_reset()
        self.held.clear()
        self.worker.set_io(None, None)
        time.sleep(0.05)
        for obj in (self.cam, self.source):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        if self.poller is not None:
            self.poller.close()
            self.poller = None
        self.cam = ptzmod.PTZBase()
        self.source = None
        self.canon_api = None
        self.canon_info = {}
        self.pic_panel.set_api(None)
        self._refresh_presets()
        self.view.image = None
        self.perf.setText("")
        self._picture_refresh()
        self._set_connected(False)

    def _no_video_hint(self):
        if self.source is None or self.view.image is not None:
            return
        if time.monotonic() - self._connected_at < NO_VIDEO_HINT:
            return
        if isinstance(self.source, NDISource):
            fw = " Allow PTZ Tracker through Windows Firewall (Private networks)." if platform_paths.IS_WIN else ""
            hint = f"No video from {self.source.name} yet. Check the camera is on and on this network.{fw}"
        else:
            hint = f"No video from {self.source.name} yet. Close other apps using it, or try another source."
        hint += f"\n\nDetails are logged in {APP_LOG}"
        if self.view.placeholder != hint:
            self.view.placeholder = hint
            self.view.update()
            log.warning("connected to %s but no frames after %.0f s", self.source.name, NO_VIDEO_HINT)

    def _refresh_status(self):
        self._no_video_hint()
        video = f"Video: {self.source.name}" if self.source else "Video: none"
        self.conn_status.setText(f"{video}\n{self.cam.status()}")
        pos = self.poller.status() if self.poller else "Camera position: not connected"
        b = self.stage.get_bounds()
        if b is None:
            stage = "No stage drawn yet"
        else:
            stage = f"Stage pan {b.pan_min:+.1f}° to {b.pan_max:+.1f}°, tilt {b.tilt_min:+.1f}° to {b.tilt_max:+.1f}°"
        self.stage_status.setText(f"{pos}    {stage}")
        self.remote.tick()

    # ---------------- control rail
    def _rail(self):
        rail = QWidget()
        rail.setObjectName("rail")
        v = QVBoxLayout(rail)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(22)
        v.addLayout(self._tracking_box())
        v.addLayout(self._stage_box())
        v.addLayout(self._preset_box())
        v.addLayout(self._manual_box())
        v.addLayout(self._picture_box())
        v.addStretch(1)
        stop = nofocus(QPushButton())
        stop.setObjectName("stop")
        stop.setMinimumHeight(48)
        stop.clicked.connect(self.emergency_stop)
        v.addWidget(stop)

        def relabel():
            k = self.binds.short("stop")
            stop.setText(f"Stop camera    {k}".rstrip())
            stop.setToolTip(f"Stops tracking and every camera move ({k})")

        self._on_binds(relabel)
        return rail

    # ---------------- tracking
    def _tracking_box(self):
        v = QVBoxLayout()
        v.setSpacing(10)
        self.track_btn = TrackButton()
        self.track_btn.toggled.connect(self.set_tracking)
        v.addWidget(self.track_btn)
        row = QHBoxLayout()
        self.auto_pick = QCheckBox("Auto-pick biggest head")
        self.auto_pick.setChecked(self.cfg["auto_pick"])
        self.auto_pick.toggled.connect(lambda on: setattr(self.worker, "auto_pick", on))
        self.auto_pick.toggled.connect(lambda _on: self.remote.push_state())
        rel = nofocus(QPushButton())
        rel.clicked.connect(self.worker.release_target)

        def relabel():
            k = self.binds.short("release")
            rel.setText(f"Release  {k}".rstrip())
            rel.setToolTip(f"Stop following this person so another can be picked ({k})")

        self._on_binds(relabel)
        self._on_binds(lambda: self.track_btn.key.setText(self.binds.short("track")))
        row.addWidget(self.auto_pick, 1)
        row.addWidget(rel)
        v.addLayout(row)
        return v

    def set_tracking(self, on):
        if self.track_btn.isChecked() != on:
            self.track_btn.setChecked(on)  # re-enters via toggled
            return
        self.worker.tracking_enabled = on
        if on:
            self.track_btn.set_text("Tracking", "Following the speaker · press to stop")
        else:
            self.track_btn.set_text("Start tracking", "Follows the speaker on stage")
        self.view.tracking = on
        self.view.update()
        self.remote.push_state()

    # ---------------- stage boundary
    def _stage_box(self):
        v = QVBoxLayout()
        v.setSpacing(8)
        draw = nofocus(QPushButton("Draw…"))
        draw.setObjectName("link")
        draw.setToolTip("Zoom out to the wide stage shot, then drag a box around the stage")
        draw.clicked.connect(self.start_stage_draw)
        self.edit_btn = nofocus(QPushButton("Edit edges"))
        self.edit_btn.setObjectName("link")
        self.edit_btn.setCheckable(True)
        self.edit_btn.setToolTip("Drag the stage edges or corners (or the middle to move the whole box) instead of redrawing it")
        self.edit_btn.toggled.connect(self.set_edit_stage)
        v.addLayout(section("Stage", draw, self.edit_btn))
        home = nofocus(QPushButton())
        home.setMinimumHeight(40)
        home.setStyleSheet("font-weight: 600;")
        home.clicked.connect(self.go_stage_home)
        v.addWidget(home)

        def relabel():
            k = self.binds.short("stage_shot")
            home.setText(f"Go to stage shot    {k}".rstrip())
            home.setToolTip(f"Stop tracking and go back to the wide stage shot ({k})")

        self._on_binds(relabel)
        self.stage_check = QCheckBox("Return to the stage shot when the speaker leaves")
        self.stage_check.setChecked(self.stage.enabled)
        self.stage_check.toggled.connect(lambda on: setattr(self.stage, "enabled", on))
        v.addWidget(self.stage_check)
        self.resume_check = QCheckBox("Resume tracking when someone is back on stage")
        self.resume_check.setChecked(self.stage.auto_resume)
        self.resume_check.toggled.connect(lambda on: setattr(self.stage, "auto_resume", on))
        v.addWidget(self.resume_check)
        return v

    def _stage_preset_changed(self, n):
        self.stage.home_preset = n
        self._refresh_presets()

    def start_stage_draw(self):
        pos = self.poller.latest() if self.poller else None
        if pos is None or self.view.image is None:
            QMessageBox.information(
                self, "Draw stage", "Connect to the camera first. The app needs video and the camera's position (Camera IP)."
            )
            return
        self.settings_dialog.hide()
        self.set_tracking(False)
        self.view.draw_mode = True
        self.view.setCursor(Qt.CrossCursor)
        self.statusBar().showMessage("Zoom out to the wide stage shot, then drag a box around the stage")

    def stage_drawn(self, x1, y1, x2, y2):
        pos = self.poller.latest() if self.poller else None
        if pos is None:
            self.statusBar().showMessage("Lost the camera position — stage not saved")
            return
        w, h = self.view.image.width(), self.view.image.height()
        bounds = StageBounds.from_rect(x1, y1, x2, y2, pos, w, h)
        self.stage.bounds = asdict(bounds)
        self.stage.home = {"pan": pos.pan, "tilt": pos.tilt, "hfov": pos.hfov}
        self.stage.enabled = True
        self.stage_check.setChecked(True)
        # the shot the stage was drawn on becomes the "go home" shot
        self.cam.store_preset(self.stage.home_preset)
        self._save()
        self._refresh_status()
        self.statusBar().showMessage(f"Stage saved, and this shot is stored as preset {self.stage.home_preset}")

    def set_edit_stage(self, on):
        if on and not self.stage.bounds:
            self.edit_btn.setChecked(False)
            QMessageBox.information(self, "Edit stage", "Draw the stage once first, then you can adjust its edges here.")
            return
        if on and (self.poller is None or self.poller.latest() is None):
            self.edit_btn.setChecked(False)
            QMessageBox.information(self, "Edit stage", "Connect to the camera first (the stage edges need its position).")
            return
        self.set_tracking(False) if on else None
        self.view.edit_mode = on
        self.worker.show_bounds = on
        self.edit_btn.setText("Done editing" if on else "Edit edges")
        self.view.setCursor(Qt.CrossCursor)
        self.view.update()
        self.statusBar().showMessage("Drag the yellow edges to adjust the stage" if on else "Stage edit finished")

    def _bounds_edited(self, new):
        self.stage.bounds = new

    def _bounds_edit_done(self):
        self._save()
        self.statusBar().showMessage("Stage updated")

    def _resume_move_toggled(self, on):
        if on and not self.stage.resume_shot:
            self.resume_move_check.setChecked(False)
            QMessageBox.information(
                self, "Resume shot", "Zoom to the level you want first, then press 'Set resume zoom from current view'."
            )
            return
        self.stage.resume_move = on

    def set_resume_shot(self):
        pos = self.poller.latest() if self.poller else None
        if pos is None:
            QMessageBox.information(self, "Resume shot", "Connect to the camera first (it needs the camera position).")
            return
        self.stage.resume_shot = {"pan": pos.pan, "tilt": pos.tilt, "hfov": pos.hfov}
        self.resume_move_check.setChecked(True)
        self._save()
        self.statusBar().showMessage(f"Resume zoom saved: view {pos.hfov:.1f}°")

    def go_stage_home(self):
        self.set_tracking(False)
        self.worker.request_home()

    # ---------------- key hints
    def _on_binds(self, fn):
        """Registers a function that writes key hints into widgets; it reruns whenever the binds change."""
        self._relabels.append(fn)
        fn()

    def _relabel_all(self):
        for fn in self._relabels:
            fn()

    def _binds_changed(self, notes=None):
        self._relabel_all()
        self._refresh_key_rows(notes)
        self._layout_refresh()
        self._refresh_presets()
        self._save()

    # ---------------- presets
    def _preset_box(self):
        v = QVBoxLayout()
        v.setSpacing(8)
        self.bank_hint = label("", "hint")
        v.addLayout(section("Presets", self.bank_hint))
        grid = QGridLayout()
        grid.setSpacing(6)
        self.preset_btns = []
        for i in range(20):
            b = nofocus(QPushButton(str(i + 1)))
            b.setObjectName("preset")
            b.setFixedHeight(46)
            b.clicked.connect(lambda _=False, n=i + 1: self.preset(n))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda _pos, n=i + 1: self._preset_menu(n))
            grid.addWidget(b, i // 5, i % 5)
            self.preset_btns.append(b)
        v.addLayout(grid)
        row = QHBoxLayout()
        self.store_btn = nofocus(QPushButton("Store"))
        self.store_btn.setObjectName("store")
        self.store_btn.setCheckable(True)
        self.store_btn.setMinimumHeight(40)
        self.store_btn.setToolTip("Then press a preset to save the current shot there (or hold Shift and press its number)")
        self.store_btn.toggled.connect(self._store_toggled)
        self.bank_btn = nofocus(QPushButton())
        self.bank_btn.setMinimumHeight(40)
        self.bank_btn.clicked.connect(self.toggle_bank)
        row.addWidget(self.store_btn, 1)
        row.addWidget(self.bank_btn, 1)
        v.addLayout(row)
        self._on_binds(self._refresh_presets)
        return v

    def preset_name(self, n):
        """The name you gave a preset here, else the camera's own."""
        return self.cfg["preset_names"].get(str(n)) or self.canon_info.get(f"p.{n}.name.utf8", "")

    def preset_speed(self, n):
        """Recall speed for preset n (camera units), or None for the speed the camera saved with it."""
        return self.cfg["preset_speeds"].get(str(n)) if "preset_speed" in self._caps() else None

    def _refresh_presets(self):
        if not hasattr(self, "preset_btns"):
            return
        stage_n = self.stage.home_preset
        for i, b in enumerate(self.preset_btns):
            n = i + 1
            own = self.preset_name(n)
            name = "Stage" if n == stage_n else own
            short = name if len(name) <= 8 else name[:7] + "…"
            b.setText(f"{n}\n{short}" if name else str(n))
            active = i // 10 == self.bank
            css = []
            if n == stage_n:
                css.append(f"border: 1px solid {AMBER}; color: {AMBER};")
            if not active:
                css.append(f"background: {PANEL}; " + ("" if n == stage_n else f"color: {INK3};"))
            b.setStyleSheet("QPushButton { %s }" % " ".join(css) if css else "")
            sp = self.cfg["preset_speeds"].get(str(n))
            tip = f"Preset {n}: click to recall, right-click to rename or set its speed"
            tip += "\nThis is the stage shot" if n == stage_n else ""
            tip += f"\n{own}" if own and n != stage_n else ""
            tip += f"\nRecall speed {sp}" if sp else ""
            b.setToolTip(tip)
        lo = self.bank * 10 + 1
        self.bank_hint.setText(f"Keys → {lo}–{lo + 9}   ⇧ stores")
        self.bank_btn.setText(f"Bank {self.bank + 1}   {self.binds.short('bank')}".rstrip())
        self.bank_btn.setToolTip("Switch which presets the number keys use (1–10 or 11–20)")

    def toggle_bank(self):
        self.bank = 1 - self.bank
        self._refresh_presets()
        lo = self.bank * 10 + 1
        self.view.toast(f"Preset keys {lo}–{lo + 9}")

    def _preset_menu(self, n):
        menu = QMenu(self)
        menu.addAction("Rename…", lambda: self._rename_preset(n))
        if str(n) in self.cfg["preset_names"]:
            menu.addAction("Clear name", lambda: self._set_preset_name(n, ""))
        if "preset_speed" in self._caps():
            menu.addAction("Recall speed…", lambda: self._ask_preset_speed(n))
            if str(n) in self.cfg["preset_speeds"]:
                menu.addAction("Use the camera's saved speed", lambda: self._set_preset_speed(n, None))
        menu.exec(QCursor.pos())

    def _rename_preset(self, n):
        text, ok = QInputDialog.getText(self, "Rename preset", f"Name for preset {n}", text=self.preset_name(n))
        if ok:
            self._set_preset_name(n, text)

    def _set_preset_name(self, n, text):
        text = text.strip()[:24]
        if text:
            self.cfg["preset_names"][str(n)] = text
        else:
            self.cfg["preset_names"].pop(str(n), None)
        self._refresh_presets()
        self._save()

    def _ask_preset_speed(self, n):
        lo = canon_http.num(self.canon_info, "p.ptzspeed.min", 1)
        hi = canon_http.num(self.canon_info, "p.ptzspeed.max", 100)
        cur = self.cfg["preset_speeds"].get(str(n)) or canon_http.num(self.canon_info, f"p.{n}.ptz.speed", hi)
        v, ok = QInputDialog.getInt(self, "Preset speed", f"Recall speed for preset {n} ({lo}-{hi}, higher is faster)", cur, lo, hi)
        if ok:
            self._set_preset_speed(n, v)

    def _set_preset_speed(self, n, v):
        if v is None:
            self.cfg["preset_speeds"].pop(str(n), None)
        else:
            self.cfg["preset_speeds"][str(n)] = int(v)
        self._refresh_presets()
        self._save()

    def _store_toggled(self, on):
        self.store_mode = on
        if on:
            self.statusBar().showMessage("Store mode: press a preset to save the current shot")

    def preset(self, n, store=None):
        store = self.store_mode if store is None else store
        if store:
            if n == self.stage.home_preset and QMessageBox.question(
                self, "Replace the stage shot?", f"Preset {n} is the stage shot. Storing here replaces it. Continue?"
            ) != QMessageBox.Yes:
                self.store_btn.setChecked(False)
                return
            sp = self.preset_speed(n)
            self.cam.store_preset(n, speed=sp) if sp else self.cam.store_preset(n)
            self.statusBar().showMessage(f"Stored preset {n}")
            self.store_btn.setChecked(False)
        else:
            self.set_tracking(False)
            self._manual_reset()
            sp = self.preset_speed(n)
            self.cam.recall_preset(n, speed=sp) if sp else self.cam.recall_preset(n)
            name = self.preset_name(n)
            self.statusBar().showMessage(f"Recalled preset {n}" + (f" ({name})" if name else ""))

    # ---------------- manual control
    def _manual_box(self):
        v = QVBoxLayout()
        v.setSpacing(8)
        self.manual_hint = label("", "hint")
        v.addLayout(section("Manual", self.manual_hint))
        top = QHBoxLayout()
        top.setSpacing(14)
        pad = QGridLayout()
        pad.setSpacing(4)
        dirs = {
            (0, 0): ("↖", -1, 1), (0, 1): ("↑", 0, 1), (0, 2): ("↗", 1, 1),
            (1, 0): ("←", -1, 0), (1, 2): ("→", 1, 0),
            (2, 0): ("↙", -1, -1), (2, 1): ("↓", 0, -1), (2, 2): ("↘", 1, -1),
        }  # fmt: skip
        for (r, c), (txt, px, ty) in dirs.items():
            key = f"btn{r}{c}"
            pad.addWidget(
                hold_button(txt, lambda k=key, px=px, ty=ty: self.manual_press(k, px, ty), lambda k=key: self.manual_release(k)),
                r,
                c,
            )
        home = nofocus(QPushButton("H"))
        home.setFixedSize(40, 40)
        home.setStyleSheet(f"QPushButton {{ background: {PANEL}; border-radius: 20px; color: {INK2}; }} QPushButton:disabled {{ color: #5A5D63; }}")
        home.clicked.connect(self.camera_home)
        pad.addWidget(home, 1, 1)
        top.addLayout(pad)

        side = QGridLayout()
        side.setSpacing(6)
        side.addWidget(label("Zoom", "muted"), 0, 0)
        zout = hold_button("", lambda: self.zoom_press(-1), self.zoom_release, 64, 34)
        zin = hold_button("", lambda: self.zoom_press(1), self.zoom_release, 64, 34)
        side.addWidget(zout, 0, 1)
        side.addWidget(zin, 0, 2)
        side.addWidget(label("Focus", "muted"), 1, 0)
        near = hold_button("", lambda: self.cam.focus(-0.5), lambda: self.cam.focus(0), 64, 34)
        far = hold_button("", lambda: self.cam.focus(0.5), lambda: self.cam.focus(0), 64, 34)
        side.addWidget(near, 1, 1)
        side.addWidget(far, 1, 2)
        af = nofocus(QPushButton("Auto"))
        af.setToolTip("Turn on continuous auto focus")
        af.clicked.connect(lambda: self.cam.autofocus(True))
        once = nofocus(QPushButton())
        once.clicked.connect(lambda: self.cam.one_push_af())
        side.addWidget(af, 2, 1)
        side.addWidget(once, 2, 2)
        top.addLayout(side, 1)
        v.addLayout(top)

        speeds = QGridLayout()
        speeds.setHorizontalSpacing(8)
        speeds.setVerticalSpacing(2)
        self.pan_slider = slider(1, motion.PT_STEPS, self.pt_step, lambda x: self.set_speeds(step=round(x)), 1, lambda x: str(round(x)))
        self.zoom_slider = slider(0.01, 1.0, self.zoom_speed, lambda x: self.set_speeds(zoom=x), 100, lambda x: f"{round(x * 100)} %")
        self.pan_slider.setToolTip("How fast the arrows and the pad pan and tilt: 1-16, as in Canon's camera app")
        self.zoom_slider.setToolTip("How fast the zoom buttons and keys zoom: 1-100 %, as in Canon's camera app")
        for r, (text, row) in enumerate((("Pan / tilt", self.pan_slider), ("Zoom", self.zoom_slider))):
            speeds.addWidget(label(text, "muted"), r, 0)
            speeds.addWidget(row, r, 1)
        speeds.setColumnStretch(1, 1)
        v.addLayout(speeds)
        self.speed_hint = label("", "hint")
        v.addWidget(self.speed_hint)

        k = self.binds.short
        al = self.binds.all_text

        def relabel():
            self.manual_hint.setText("Keys " + " ".join(k(a) for a in ("tilt_up", "pan_left", "tilt_down", "pan_right")))
            home.setToolTip(f"Camera home position ({k('home')})")
            zout.setText(f"−   {k('zoom_out')}")
            zout.setToolTip(f"Zoom out ({al('zoom_out')})")
            zin.setText(f"+   {k('zoom_in')}")
            zin.setToolTip(f"Zoom in ({al('zoom_in')})")
            near.setText(f"Near  {k('focus_near')}")
            near.setToolTip(f"Focus nearer ({al('focus_near')})")
            far.setText(f"Far  {k('focus_far')}")
            far.setToolTip(f"Focus farther ({al('focus_far')})")
            once.setText(f"Once  {k('focus_once')}".rstrip())
            once.setToolTip(f"Focus once on the current shot, then hold it ({al('focus_once')})")
            self.speed_hint.setText(f"Pan / tilt {k('pan_slower')} slower · {k('pan_faster')} faster    Zoom {k('zoom_slower')} slower · {k('zoom_faster')} faster")

        self._on_binds(relabel)
        return v

    def _picture_box(self):
        v = QVBoxLayout()
        v.setSpacing(8)
        toggle = nofocus(QPushButton())
        toggle.setObjectName("link")
        self.pic_panel = CanonPicturePanel()

        def show(on):
            self.pic_panel.setVisible(on)
            toggle.setText("Hide" if on else "Show")
            self.cfg["picture_open"] = on

        toggle.clicked.connect(lambda: show(not self.pic_panel.isVisible()))
        v.addLayout(section("Picture", toggle))
        v.addWidget(self.pic_panel)
        show(self.cfg.get("picture_open", True) is not False)
        return v

    def _canon_update(self, info):
        """New info list from the camera: picture values and preset names / speeds follow the camera."""
        if self.canon_api is None:
            return
        self.canon_info = info
        self.pic_panel.update_state(info)
        self._refresh_presets()

    def camera_home(self):
        self._manual_reset()
        self.cam.home()

    def set_speeds(self, step=None, zoom=None):
        """Sets the pan/tilt step (1-16) and / or zoom speed (0.01-1), moves the sliders, and applies it to a move in progress."""
        if step is not None:
            self.pt_step = self.cfg["pt_step"] = motion.clean_step(int(step))
            self.pan_speed = motion.pt_speed(self.pt_step)
            self.pan_slider.set(self.pt_step)
        if zoom is not None:
            self.zoom_speed = self.cfg["zoom_speed"] = motion.clean_speed(zoom)
            self.zoom_slider.set(self.zoom_speed)
        if self.zoom_held:
            self.cam.zoom(self.zoom_held * self.zoom_speed)
        if self.held or self.ramp.active:
            self._start_manual()
        self.remote.push_state()

    def step_speed(self, direction, toast=True):
        """Slower / Faster: one pan/tilt step down or up (no wraparound)."""
        self.set_speeds(step=self.pt_step + direction)
        if toast:
            self.view.toast(f"Pan / tilt speed {self.pt_step}")

    def step_zoom_speed(self, direction, toast=True):
        """Zoom slower / faster: 10 % down or up (1-100 %)."""
        self.set_speeds(zoom=self.zoom_speed + 0.1 * direction)
        if toast:
            self.view.toast(f"Zoom speed {round(self.zoom_speed * 100)} %")

    def _zoom_scale(self):
        pos = self.poller.latest() if self.poller else None
        if pos is None or not self.cfg["zoom_scaled_speed"] or "zoom_linked" in self._caps():  # the camera slows itself
            return 1.0
        self._widest_hfov = max(self._widest_hfov, pos.hfov, (self.stage.home or {}).get("hfov", 0.0))
        return motion.zoom_scale(pos.hfov, self._widest_hfov)

    def _manual_target(self):
        """Pan/tilt the held keys ask for right now: direction, speed level, diagonal and zoom scaling."""
        x, y = motion.normalise(sum(p for p, _ in self.held.values()), sum(t for _, t in self.held.values()))
        speed = self.pan_speed * self._zoom_scale()
        return x * speed, y * speed

    def _start_manual(self):
        self.worker.manual_active = True
        if not self.ramp_timer.isActive():
            self._ramp_t = time.monotonic() - 1 / 30
            self.ramp_timer.start()
        self._manual_tick()

    def _manual_tick(self):
        now = time.monotonic()
        dt, self._ramp_t = min(now - self._ramp_t, 0.1), now
        pan, tilt = self.ramp.step(dt, *self._manual_target())
        pt = (motion.floor_speed(pan), motion.floor_speed(tilt))
        if pt != self._sent_pt:
            self._sent_pt = pt
            self.cam.pan_tilt(*pt)
        moving = bool(self.held) or self.ramp.active
        self.worker.manual_active = moving or self.zoom_held != 0
        if not moving:
            self.ramp_timer.stop()

    def _manual_reset(self):
        """Forget any manual move in progress (a preset or home is about to take over), without sending a stop."""
        self.ramp.reset()
        self.ramp_timer.stop()
        self._sent_pt = (0.0, 0.0)
        self.worker.manual_active = bool(self.held) or self.zoom_held != 0

    def _wheel_zoom(self, direction):
        if direction:
            self.zoom_press(direction)
        else:
            self.zoom_release()

    def manual_press(self, key, pan, tilt):
        self.held[key] = (pan, tilt)
        self._start_manual()

    def manual_release(self, key):
        self.held.pop(key, None)
        self._start_manual()

    def zoom_press(self, direction):
        self.zoom_held = direction
        self.worker.manual_active = True
        self.cam.zoom(direction * self.zoom_speed)

    def zoom_release(self):
        self.zoom_held = 0
        self.cam.zoom(0)
        self.worker.manual_active = bool(self.held) or self.ramp.active

    def emergency_stop(self):
        self.worker.cancel_home()
        self.set_tracking(False)
        self.held.clear()
        self.zoom_held = 0
        self._manual_reset()  # no ramp: the stop below is immediate
        self.worker.manual_active = False
        self.cam.stop()
        self.statusBar().showMessage("Camera stopped")

    # ---------------- settings sheet
    def _settings_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Settings")
        dlg.setMinimumSize(780, 600)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(20, 16, 20, 16)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._camera_tab(), "Camera")
        self.tabs.addTab(self._tracking_tab(), "Tracking")
        self.tabs.addTab(self._stage_tab(), "Stage")
        self.tabs.addTab(self._shortcuts_tab(), "Shortcuts")
        self.tabs.addTab(self._picture_tab(), "Picture")
        self.tabs.addTab(self.remote.tab(), "Remote")
        self.tabs.addTab(self._about_tab(), "About")
        v.addWidget(self.tabs, 1)
        done = QPushButton("Done")
        done.setObjectName("primary")
        done.setFixedWidth(120)
        done.clicked.connect(dlg.accept)
        dlg.accepted.connect(self._save)
        self.save_btn = QPushButton("Save")
        self.save_btn.setFixedWidth(120)
        self.save_btn.setToolTip("Save now and keep Settings open")
        self.save_btn.clicked.connect(self._save_clicked)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.save_btn)
        row.addWidget(done)
        v.addLayout(row)
        return dlg

    def _save_clicked(self):
        try:
            self._save()
        except OSError as e:
            QMessageBox.warning(self.settings_dialog, "Could not save", str(e))
            return
        self.save_btn.setText("Saved ✓")
        QTimer.singleShot(1500, lambda: self.save_btn.setText("Save"))

    def open_settings(self, tab=None):
        if tab is not None:
            self.tabs.setCurrentIndex(tab)
        self.settings_dialog.show()
        self.settings_dialog.raise_()

    def _camera_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 16, 4, 4)
        v.setSpacing(16)
        self.ndi_hq = QCheckBox("Full-quality NDI")
        self.ndi_hq.setChecked(self.cfg["ndi_high_bandwidth"])
        v.addWidget(setting("NDI video", "Off uses the low-bandwidth stream, which tracks just as well with less delay and CPU", self.ndi_hq))
        self.user_edit = QLineEdit(self.cfg["camera_user"])
        self.user_edit.setPlaceholderText("User name (blank for none)")
        self.pass_edit = QLineEdit(self.cfg["camera_password"])
        self.pass_edit.setPlaceholderText("Password")
        self.pass_edit.setEchoMode(QLineEdit.Password)
        login = QWidget()
        lh = QHBoxLayout(login)
        lh.setContentsMargins(0, 0, 0, 0)
        lh.addWidget(self.user_edit)
        lh.addWidget(self.pass_edit)
        v.addWidget(setting("Camera login", "The camera's web login (the one Canon's camera app uses). Reading works without it; control may need it. Saved in this computer's settings file.", login))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self.cfg["visca_port"])
        self.port_spin.setFixedWidth(120)
        v.addWidget(setting("VISCA port", "52381 for most cameras, including the Canon CR-N300", self.port_spin))
        self.raw_check = QCheckBox("Raw VISCA (no IP header)")
        self.raw_check.setChecked(self.cfg["visca_raw"])
        v.addWidget(setting("VISCA format", "Only for cameras that reject the standard VISCA-over-IP header", self.raw_check))
        self.conn_status = label("", "muted", wrap=True)
        v.addWidget(setting("Status", "Changes here apply the next time you connect", self.conn_status))
        logs = nofocus(QPushButton("Open log folder"))
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(platform_paths.log_dir())))
        logs.setFixedWidth(160)
        v.addWidget(setting("Logs", "app.log has the details if the video or camera control isn't working", logs))
        v.addStretch(1)
        return w

    def _tracking_tab(self):
        w = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(4, 16, 4, 4)
        grid.setHorizontalSpacing(32)
        grid.setVerticalSpacing(14)
        s = self.settings
        self.track_rows = {}
        for attr, (title, help_text, lo, hi) in TRACK_SLIDERS.items():
            row = slider(lo, hi, getattr(s, attr), lambda val, a=attr: setattr(self.settings, a, val))
            self.track_rows[attr] = row
        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(label("Framing", "section"))
        for attr in ("target_x", "target_y", "target_height", "ignore_below"):
            left.addWidget(setting(TRACK_SLIDERS[attr][0], TRACK_SLIDERS[attr][1], self.track_rows[attr]))
        left.addSpacing(8)
        left.addWidget(label("Hold still", "section"))
        self.lock_checks = {}
        for text, attr in LOCKS:
            c = QCheckBox(text)
            c.setChecked(getattr(s, attr))
            c.toggled.connect(lambda on, a=attr: setattr(self.settings, a, on))
            self.lock_checks[attr] = c
            left.addWidget(c)
        left.addStretch(1)
        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(label("Movement", "section"))
        for attr in ("speed", "max_speed", "deadzone_x", "deadzone_y", "exponent", "smoothing"):
            right.addWidget(setting(TRACK_SLIDERS[attr][0], TRACK_SLIDERS[attr][1], self.track_rows[attr]))
        reset = QPushButton("Reset tracking to defaults")
        reset.setObjectName("link")
        reset.clicked.connect(self._reset_tracking)
        right.addWidget(reset, 0, Qt.AlignLeft)
        right.addStretch(1)
        grid.addLayout(left, 0, 0)
        grid.addLayout(right, 0, 1)
        scroll = QScrollArea()
        scroll.setWidget(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        return scroll

    def _reset_tracking(self):
        d = TrackSettings()
        for attr, row in self.track_rows.items():
            row.slider.setValue(int(getattr(d, attr) * row.scale))
        for attr, c in self.lock_checks.items():
            c.setChecked(getattr(d, attr))

    def _stage_tab(self):
        w = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(4, 16, 4, 4)
        grid.setHorizontalSpacing(32)
        grid.setVerticalSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(label("Stage shot", "section"))
        spin = QSpinBox()
        spin.setRange(1, 99)
        spin.setValue(self.stage.home_preset)
        spin.setFixedWidth(120)
        spin.valueChanged.connect(self._stage_preset_changed)
        left.addWidget(setting("Stage shot preset", "Drawing the stage stores the wide shot in this preset", spin))
        draw = QPushButton("Draw new stage…")
        draw.clicked.connect(self.start_stage_draw)
        left.addWidget(draw, 0, Qt.AlignLeft)
        left.addWidget(setting("Speed back to the stage shot", "How fast the camera returns", slider(0.1, 1.0, self.stage.home_speed, lambda x: setattr(self.stage, "home_speed", x))))
        left.addWidget(setting("Off stage for (seconds)", "How long the speaker can be off stage before the camera goes back", slider(0.5, 10, self.stage.leave_seconds, lambda x: setattr(self.stage, "leave_seconds", x))))
        left.addWidget(setting("Lost for (seconds)", "How long nobody can be seen before the camera goes back", slider(0.5, 10, self.stage.lost_seconds, lambda x: setattr(self.stage, "lost_seconds", x))))
        left.addStretch(1)
        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(label("When someone comes back", "section"))
        self.resume_move_check = QCheckBox("Ease to a set zoom while tracking resumes")
        self.resume_move_check.setChecked(self.stage.resume_move)
        self.resume_move_check.toggled.connect(self._resume_move_toggled)
        right.addWidget(self.resume_move_check)
        set_resume = QPushButton("Use the current zoom")
        set_resume.setToolTip("Zoom to the level you want when someone comes back, then press this. Only the zoom is used; the position follows the person")
        set_resume.clicked.connect(self.set_resume_shot)
        right.addWidget(set_resume, 0, Qt.AlignLeft)
        right.addWidget(setting("Resume zoom speed", "How fast it eases to that zoom", slider(0.1, 1.0, self.stage.resume_speed, lambda x: setattr(self.stage, "resume_speed", x))))
        right.addStretch(1)
        grid.addLayout(left, 0, 0)
        grid.addLayout(right, 0, 1)
        return w

    # ---------------- shortcuts tab
    def _shortcuts_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 16, 4, 4)
        v.setSpacing(6)
        v.addWidget(label("Click a key, then press the new one. Esc cancels, Backspace clears. Pressing a key that is already used moves it here.", "hint", wrap=True))
        layouts = QHBoxLayout()
        layouts.addWidget(label("Key layouts", "section"))
        self.layouts_combo = QComboBox()
        self.layouts_combo.setMinimumWidth(180)
        self.layouts_combo.activated.connect(self._layout_chosen)
        save = QPushButton("Save layout…")
        save.clicked.connect(self._layout_save)
        delete = QPushButton("Delete")
        delete.clicked.connect(self._layout_delete)
        layouts.addWidget(self.layouts_combo, 1)
        layouts.addWidget(save)
        layouts.addWidget(delete)
        v.addSpacing(4)
        v.addLayout(layouts)
        v.addWidget(label("Save the keys below under a name (one per operator, say) and switch between them here.", "hint", wrap=True))
        self.key_rows = {}
        groups = (("Held while pressed", keys.HELD), ("Pressed once", keys.PRESS))
        for title, kind in groups:
            v.addSpacing(8)
            v.addWidget(label(title, "section"))
            for action, text, k, _ in keys.ACTIONS:
                if k != kind or action.startswith("preset_"):
                    continue
                v.addLayout(self._key_row(action, text))
        v.addSpacing(8)
        v.addWidget(label("Presets: recall with these keys, hold Shift to store. B switches between slots 1–10 and 11–20.", "hint", wrap=True))
        grid = QGridLayout()
        grid.setSpacing(6)
        for i in range(1, 11):
            col = QVBoxLayout()
            col.setSpacing(2)
            col.addLayout(self._key_row(f"preset_{i}", f"Preset {i}", compact=True))
            grid.addLayout(col, (i - 1) // 5, (i - 1) % 5)
        v.addLayout(grid)
        restore = QPushButton("Restore default keys")
        restore.setObjectName("link")
        restore.clicked.connect(self._restore_keys)
        v.addWidget(restore, 0, Qt.AlignLeft)

        self.zoom_scale_check = QCheckBox("Slow down pan/tilt when zoomed in")
        self.zoom_scale_check.setChecked(self.cfg["zoom_scaled_speed"])
        self.zoom_scale_check.toggled.connect(lambda on: self.cfg.__setitem__("zoom_scaled_speed", on))
        v.addWidget(setting("Zoom-aware speed", "Keeps the picture moving at about the same rate at full zoom. Over the Canon API the camera does this itself (Canon's zoom-linked speed); otherwise it needs the Camera IP so the app can read the zoom.", self.zoom_scale_check))
        v.addStretch(1)
        self._refresh_key_rows()
        self._layout_refresh()
        scroll = QScrollArea()
        scroll.setWidget(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        return scroll

    def _key_row(self, action, text, compact=False):
        row = QHBoxLayout()
        row.setSpacing(6)
        name = label(text if not compact else text.replace("Preset ", ""))
        name.setFixedWidth(170 if not compact else 18)
        row.addWidget(name)
        btns = []
        for slot in range(keys.SLOTS if not compact else 1):
            b = KeyButton()
            b.captured.connect(lambda code, a=action, sl=slot: self._bind_key(a, sl, code))
            b.cleared.connect(lambda a=action, sl=slot: self._clear_key(a, sl))
            row.addWidget(b)
            btns.append(b)
        note = label("")
        note.setStyleSheet(f"color: {AMBER}; font-size: 11px;")
        reset = QPushButton("Reset")
        reset.setObjectName("link")
        reset.clicked.connect(lambda _=False, a=action: self._reset_key(a))
        if not compact:
            row.addWidget(reset)
            row.addWidget(note, 1)
        else:
            reset.hide()
            note.hide()
        self.key_rows[action] = (btns, note)
        return row

    def _refresh_key_rows(self, notes=None):
        for action, (btns, note) in self.key_rows.items():
            for slot, b in enumerate(btns):
                code = self.binds.keys[action][slot]
                b.set_key(keys.pretty(code) if code else "")
            note.setText((notes or {}).get(action, ""))

    def _bind_key(self, action, slot, code):
        lost = self.binds.assign(action, slot, code)
        self._binds_changed({lost: f"Key moved to {keys.LABELS[action]}"} if lost else None)

    def _clear_key(self, action, slot):
        self.binds.clear(action, slot)
        self._binds_changed()

    def _reset_key(self, action):
        lost = self.binds.reset(action)
        self._binds_changed({a: f"Key moved to {keys.LABELS[action]}" for a in lost})

    def _restore_keys(self):
        self.binds.reset_all()
        self._binds_changed()

    def _layout_refresh(self):
        """Lists the saved layouts and shows the one the current keys match, if any."""
        saved = {n: k for n, k in self.cfg["key_layouts"].items() if isinstance(k, dict)}
        now = self.binds.to_cfg()
        self.layouts_combo.clear()
        self.layouts_combo.addItem("Choose a layout…")
        self.layouts_combo.addItems(sorted(saved))
        match = next((n for n in sorted(saved) if keys.Binds(saved[n]).to_cfg() == now), "")
        if match:
            self.layouts_combo.setCurrentText(match)

    def _layout_chosen(self, index):
        name = self.layouts_combo.itemText(index)
        if index <= 0 or not isinstance(self.cfg["key_layouts"].get(name), dict):
            return
        self.binds.load(self.cfg["key_layouts"][name])
        self._binds_changed()
        self.statusBar().showMessage(f"Key layout: {name}")

    def _layout_save(self):
        current = self.layouts_combo.currentText() if self.layouts_combo.currentIndex() > 0 else ""
        name, ok = QInputDialog.getText(self.settings_dialog, "Save key layout", "Name for these keys (e.g. your name)", text=current)
        name = name.strip()[:40]
        if ok and name:
            self.cfg["key_layouts"][name] = self.binds.to_cfg()
            self._layout_refresh()
            self._save()

    def _layout_delete(self):
        name = self.layouts_combo.currentText()
        if self.layouts_combo.currentIndex() > 0 and self.cfg["key_layouts"].pop(name, None) is not None:
            self._layout_refresh()
            self._save()

    # ---------------- picture tab
    def _picture_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 16, 4, 4)
        v.setSpacing(10)
        self.pic_note = label("", "hint", wrap=True)
        v.addWidget(self.pic_note)
        self.pic_rows = {}
        self.pic_modes = {}

        def seg_row(title, field, options):
            row = QHBoxLayout()
            row.setSpacing(4)
            lbl = label(title, "section")
            lbl.setFixedWidth(100)
            row.addWidget(lbl)
            btns = {}
            for mode, text in options:
                b = nofocus(QPushButton(text))
                b.setObjectName("seg")
                b.setCheckable(True)
                b.setMinimumWidth(56)
                b.clicked.connect(lambda _=False, m=mode: self._pic_mode(field, m))
                row.addWidget(b)
                btns[mode] = b
            row.addStretch(1)
            self.pic_modes[field] = btns
            return row

        def pic_slider(field, title, fmt=None):
            r = slider(0, 1, self.pic[field], lambda x, f=field: self._pic_changed(f, x), 100.0, fmt)
            self.pic_rows[field] = r
            line = QHBoxLayout()
            lbl = label(title)
            lbl.setFixedWidth(100)
            line.addSpacing(16)
            line.addWidget(lbl)
            line.addWidget(r, 1)
            r.title = lbl
            return line

        v.addLayout(seg_row("Exposure", "exposure", [("auto", "Auto"), ("manual", "Manual")]))
        v.addLayout(pic_slider("brightness", "Brightness", picture.brightness_text))
        v.addLayout(pic_slider("iris", "Iris", picture.iris_text))
        v.addLayout(pic_slider("gain", "Gain", picture.gain_text))
        v.addLayout(pic_slider("shutter", "Shutter", picture.shutter_text))
        v.addSpacing(6)
        v.addLayout(seg_row("White balance", "wb", [("auto", "Auto"), ("indoor", "Indoor"), ("outdoor", "Outdoor"), ("once", "Once"), ("manual", "Manual")]))
        v.addLayout(pic_slider("red", "Red", picture.percent_text))
        v.addLayout(pic_slider("blue", "Blue", picture.percent_text))
        v.addSpacing(6)
        self.profile_box = QWidget()
        pb = QVBoxLayout(self.profile_box)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.setSpacing(10)
        for field in ("contrast", "sharpness", "saturation"):
            pb.addLayout(pic_slider(field, field.capitalize(), picture.percent_text))
        v.addWidget(self.profile_box)
        self.profile_note = label("", "hint", wrap=True)
        v.addWidget(self.profile_note)

        v.addSpacing(6)
        looks = QHBoxLayout()
        looks.addWidget(label("Looks", "section"))
        self.looks_combo = QComboBox()
        self.looks_combo.setMinimumWidth(180)
        self.looks_combo.activated.connect(self._look_chosen)
        save = QPushButton("Save look…")
        save.clicked.connect(self._look_save)
        delete = QPushButton("Delete")
        delete.clicked.connect(self._look_delete)
        looks.addWidget(self.looks_combo, 1)
        looks.addWidget(save)
        looks.addWidget(delete)
        v.addLayout(looks)
        self.pic_reset = QPushButton("Reset camera to auto")
        self.pic_reset.clicked.connect(self._pic_reset)
        v.addWidget(self.pic_reset, 0, Qt.AlignLeft)
        v.addWidget(label("For NDI PTZ and VISCA. Values start from what PTZ Tracker last sent this camera, since they can't read the picture back. With the Camera IP set, the Picture panel on the main window reads and changes the camera's own values.", "hint", wrap=True))
        v.addStretch(1)
        self._look_refresh()
        self._picture_refresh()
        scroll = QScrollArea()
        scroll.setWidget(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        return scroll

    def _caps(self):
        return getattr(self.cam, "caps", frozenset())

    def _pic_key_name(self, ip=None):
        ip = self.ip_edit.text().strip() if ip is None else ip
        return ip or (self.source.name if self.source else "") or "default"

    def _picture_connected(self, ip):
        """New connection: load this camera's saved picture values and start the Canon HTTP probe."""
        self.pic_key = self._pic_key_name(ip)
        self.pic = picture.clean(self.cfg["picture"].get(self.pic_key))
        self.pic_panel.set_api(self.canon_api)
        self._picture_refresh()

    def _picture_refresh(self):
        """Puts the stored values into the controls and greys out what this camera/backend can't do."""
        if not hasattr(self, "pic_rows"):
            return
        caps = self._caps()
        p = self.pic
        for field, row in self.pic_rows.items():
            row.set(p[field])
        for field, btns in self.pic_modes.items():
            for mode, b in btns.items():
                b.setChecked(p[field] == mode)
        have_exp, have_wb = "exposure" in caps, "wb" in caps
        manual_exp, manual_wb = p["exposure"] == "manual", p["wb"] == "manual"
        for f, on in (("brightness", have_exp and not manual_exp), ("iris", have_exp and manual_exp), ("gain", have_exp and manual_exp),
                      ("shutter", have_exp and manual_exp), ("red", have_wb and manual_wb), ("blue", have_wb and manual_wb)):  # fmt: skip
            self.pic_rows[f].setEnabled(on)
            self.pic_rows[f].title.setEnabled(on)
        for b in self.pic_modes["exposure"].values():
            b.setEnabled(have_exp)
        for b in self.pic_modes["wb"].values():
            b.setEnabled(have_wb)
        self.pic_reset.setEnabled(have_exp or have_wb)
        avail = {w: w == "sharpness" and "aperture" in caps for w in picture.HTTP_WORDS}
        for w in picture.HTTP_WORDS:
            self.pic_rows[w].title.setVisible(avail[w])
            self.pic_rows[w].setVisible(avail[w])
        self.profile_box.setVisible(any(avail.values()))
        if not caps:
            self.pic_note.setText("Connect to the camera to change its picture.")
            self.profile_note.setText("")
        else:
            self.pic_note.setText(f"Changes are sent live over {self.cam.name}. Watch the program feed while you drag.")
            if not all(avail.values()):
                missing = [w.capitalize() for w in picture.HTTP_WORDS if not avail[w]]
                where = "over NDI" if self.cam.name == "NDI PTZ" else f"over {self.cam.name}"
                self.profile_note.setText(f"{', '.join(missing)}: not available {where}. With the Camera IP set, the Picture panel on the main window has every setting the camera offers.")
            else:
                self.profile_note.setText("")

    def _pic_changed(self, field, value):
        self.pic[field] = value
        self.pic_dirty.add(field)
        self._pic_store()
        if not self.pic_timer.isActive():
            self.pic_timer.start()

    def _pic_store(self):
        if self.pic_key:
            self.cfg["picture"][self.pic_key] = dict(self.pic)

    def _pic_flush(self):
        """Sends what changed since the last flush: the newest value of each, at most every 100 ms."""
        dirty, self.pic_dirty = self.pic_dirty, set()
        caps = self._caps()
        groups = {picture.FIELD_GROUP[f] for f in dirty if f in picture.FIELD_GROUP}
        for g in groups:
            if picture.GROUPS[g][0] in caps:
                picture.send_group(self.cam, g, self.pic)

    def _pic_mode(self, field, mode):
        self.pic[field] = mode
        self._pic_store()
        if field == "exposure":
            self.cam.exposure_mode(mode)
            picture.send_group(self.cam, "exposure_fine" if mode == "manual" else "exposure_level", self.pic)
        else:
            self.cam.white_balance_mode(mode)
            if mode == "manual":
                picture.send_group(self.cam, "wb_rgb", self.pic)
        self._picture_refresh()

    def _pic_reset(self):
        self.pic = picture.reset_to_auto(self.pic)
        self._pic_store()
        self.cam.exposure_mode("auto")
        self.cam.white_balance_mode("auto")
        self._picture_refresh()
        self.statusBar().showMessage("Camera exposure and white balance set to auto")

    def _look_refresh(self, select=""):
        self.looks_combo.clear()
        self.looks_combo.addItem("Choose a look…")
        self.looks_combo.addItems(sorted(self.cfg["looks"]))
        if select:
            self.looks_combo.setCurrentText(select)

    def _look_chosen(self, index):
        name = self.looks_combo.itemText(index)
        if index <= 0 or name not in self.cfg["looks"]:
            return
        self.pic = picture.clean(self.cfg["looks"][name])
        self._pic_store()
        picture.send_all(self.cam, self.pic)
        self._picture_refresh()
        self.statusBar().showMessage(f"Picture look: {name}")

    def _look_save(self):
        name, ok = QInputDialog.getText(self.settings_dialog, "Save look", "Name for this look (e.g. Sunday morning)")
        name = name.strip()[:40]
        if ok and name:
            self.cfg["looks"][name] = dict(self.pic)
            self._look_refresh(name)
            self._save()

    def _look_delete(self):
        name = self.looks_combo.currentText()
        if self.looks_combo.currentIndex() > 0 and self.cfg["looks"].pop(name, None) is not None:
            self._look_refresh()
            self._save()

    # ---------------- keyboard
    def _press_actions(self):
        return {
            "track": lambda: self.set_tracking(not self.track_btn.isChecked()),
            "release": self.worker.release_target,
            "stop": self.emergency_stop,
            "home": self.camera_home,
            "stage_shot": self.go_stage_home,
            "focus_once": lambda: self.cam.one_push_af(),
            "pan_slower": lambda: self.step_speed(-1),
            "pan_faster": lambda: self.step_speed(1),
            "zoom_slower": lambda: self.step_zoom_speed(-1),
            "zoom_faster": lambda: self.step_zoom_speed(1),
            "bank": self.toggle_bank,
        }

    def eventFilter(self, obj, e):
        if e.type() not in (QEvent.KeyPress, QEvent.KeyRelease) or not self.isActiveWindow():
            return False
        if isinstance(QApplication.focusWidget(), (QLineEdit, QSpinBox, QComboBox)):
            return False
        press = e.type() == QEvent.KeyPress
        if e.isAutoRepeat():
            return True
        k = e.key()
        if press and k == Qt.Key_Comma and e.modifiers() & Qt.ControlModifier:  # ⌘, on a Mac; not rebindable
            self.open_settings()
            return True
        action, store = self.binds.resolve(k, bool(e.modifiers() & Qt.ShiftModifier))
        if action is None:
            return False
        if action in keys.HELD_DIRS:
            if press:
                self.manual_press(f"key{k}", *keys.HELD_DIRS[action])
            else:
                self.manual_release(f"key{k}")
        elif action in ("zoom_in", "zoom_out"):
            self.zoom_press(1 if action == "zoom_in" else -1) if press else self.zoom_release()
        elif action in ("focus_near", "focus_far"):
            self.cam.focus((-0.5 if action == "focus_near" else 0.5) if press else 0)
        elif not press:
            return False
        elif action.startswith("preset_"):
            self.preset(keys.preset_slot(action, self.bank), store=True if store else None)
        else:
            self._press_actions()[action]()
        return True

    # ---------------- lifecycle
    def _save(self):
        self.cfg.update(
            video=self.video_kind.currentText(),
            ndi_name=self.ndi_combo.currentText(),
            ndi_high_bandwidth=self.ndi_hq.isChecked(),
            rtsp_url=self.rtsp_edit.text(),
            webcam_index=self.webcam_spin.value(),
            control=self.control_kind.currentText(),
            camera_ip=self.ip_edit.text(),
            camera_user=self.user_edit.text().strip(),
            camera_password=self.pass_edit.text(),
            visca_port=self.port_spin.value(),
            visca_raw=self.raw_check.isChecked(),
            auto_pick=self.auto_pick.isChecked(),
            keys=self.binds.to_cfg(),
            tracking=asdict(self.settings),
            stage=asdict(self.stage),
        )
        save_config(self.cfg)

    def closeEvent(self, e):
        self.remote.shutdown()
        self._save()
        self.disconnect_camera()
        self.worker.stop()
        if self.finder:
            self.finder.close()
        super().closeEvent(e)


_crash_file = None


def setup_logging():
    """Errors (including ones in background threads) go to app.log, since the Windows app has no console."""
    try:
        os.makedirs(os.path.dirname(APP_LOG), exist_ok=True)
        handler = logging.FileHandler(APP_LOG, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    sys.excepthook = lambda *exc: log.critical("uncaught error", exc_info=exc)
    try:  # a hard crash (inside Qt, NDI or the GPU driver) writes every thread's stack here
        global _crash_file
        _crash_file = open(os.path.join(os.path.dirname(APP_LOG), "crash.log"), "a", buffering=1)
        _crash_file.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} start\n")
        faulthandler.enable(_crash_file, all_threads=True)
    except OSError:
        pass
    threading.excepthook = lambda a: log.critical("uncaught error in %s", a.thread, exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def windows_titlebar(widget):
    """Dark title bar in the app's own panel colour, so the window reads as one piece on Windows 10/11."""
    if not platform_paths.IS_WIN:
        return
    try:
        import ctypes

        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)  # DWMWA_USE_IMMERSIVE_DARK_MODE
        c = QColor(PANEL)
        colorref = ctypes.c_int(c.red() | c.green() << 8 | c.blue() << 16)
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(colorref), 4)  # DWMWA_CAPTION_COLOR (Windows 11)
    except Exception:
        log.debug("title bar styling not available", exc_info=True)


def main():
    setup_logging()
    log.info("PTZ Tracker starting (%s, frozen=%s)", sys.platform, getattr(sys, "frozen", False))
    if platform_paths.IS_WIN:
        try:  # own taskbar entry with the app icon, not the generic Python one
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LWNRA.PTZTracker")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("PTZ Tracker")
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setStyleSheet(STYLE)
    icon = resource_path("assets", "icon.png")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    if "--selftest" in sys.argv:
        from .selftest import run

        code = run(sys.argv[sys.argv.index("--selftest") + 1])
        logging.shutdown()
        os._exit(code)  # skip interpreter teardown: the test leaves NDI/ORT threads behind
    try:
        w = MainWindow()
    except Exception as e:
        log.exception("startup failed")
        QMessageBox.critical(None, "PTZ Tracker couldn't start", f"{e}\n\nDetails are in {APP_LOG}")
        sys.exit(1)
    windows_titlebar(w)
    windows_titlebar(w.settings_dialog)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
