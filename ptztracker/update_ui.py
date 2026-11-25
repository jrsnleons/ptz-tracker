"""The in-app update bar and its actions, mixed into MainWindow."""
import logging
import os
import sys
import threading

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import platform_paths, updater
from .version import __version__
from .widgets import label, nofocus, setting

log = logging.getLogger("ptztracker")


class UpdateMixin:
    """Needs self.cfg, self._save, and the update_* signals declared on MainWindow."""

    # ---------------- updates
    def _update_bar(self):
        bar = QFrame()
        bar.setObjectName("updatebar")
        bar.hide()
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 6, 16, 6)
        self.update_label = label("")
        h.addWidget(self.update_label)
        self.update_progress_bar = QProgressBar()
        self.update_progress_bar.setTextVisible(False)
        self.update_progress_bar.setFixedWidth(160)
        self.update_progress_bar.hide()
        h.addWidget(self.update_progress_bar)
        h.addStretch(1)
        self.update_btns = []
        for text, fn, name in (("What's new", self.show_update_notes, "link"), ("Update now", self.update_now, "chip"), ("Later", self.update_later, "link")):
            b = nofocus(QPushButton(text))
            b.setObjectName(name)
            b.clicked.connect(fn)
            h.addWidget(b)
            self.update_btns.append(b)
        self.update_bar = bar
        return bar

    def _about_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 16, 4, 4)
        v.setSpacing(16)
        v.addWidget(label(f"PTZ Tracker {__version__}", "cardTitle"))
        self.about_status = label("", "muted", wrap=True)
        check = QPushButton("Check for updates")
        check.setFixedWidth(160)
        check.clicked.connect(lambda: self.check_updates(manual=True))
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(check, 0, Qt.AlignLeft)
        bl.addWidget(self.about_status)
        v.addWidget(setting("Updates", "Updates download in the background and never install while you are tracking", box))
        auto = QCheckBox("Check automatically")
        auto.setChecked(self.cfg["update_auto"])
        auto.toggled.connect(lambda on: self.cfg.__setitem__("update_auto", on))
        v.addWidget(setting("Automatic checks", "A few seconds after launch and every 6 hours. Does nothing if the network is offline", auto))
        page = QPushButton("Open the releases page")
        page.setFixedWidth(180)
        page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(updater.RELEASES_PAGE)))
        v.addWidget(setting("Releases", "Every version, with notes", page))
        v.addStretch(1)
        return w

    def _auto_check(self):
        if self.cfg["update_auto"]:
            self.check_updates()

    def check_updates(self, manual=False):
        if self._checking or self._updating:
            return
        self._checking = True
        if manual:
            self.about_status.setText("Checking…")

        def go():
            try:
                rel, err = updater.fetch_latest(), ""
                err = "" if rel else "the update file wasn't in the expected format"
            except Exception as e:
                rel, err = None, str(e) or type(e).__name__
            self.update_checked.emit(rel, err, manual)

        threading.Thread(target=go, daemon=True).start()

    def _update_checked(self, rel, err, manual):
        self._checking = False
        if rel is None:
            log.info("update check failed: %s", err)
            if manual:
                self.about_status.setText(f"Couldn't check for updates: {err}")
            return
        if not updater.is_newer(rel.version, __version__):
            if manual:
                self.about_status.setText(f"You're up to date (version {__version__}).")
            return
        self.release = rel
        if manual:
            self.about_status.setText(f"Version {rel.version} is available.")
        if manual or rel.version != self.cfg["update_skip"]:
            self.update_label.setText(f"PTZ Tracker {rel.version} is available")
            self.update_bar.show()

    def show_update_notes(self):
        if self.release:
            QMessageBox.information(self, f"PTZ Tracker {self.release.version}", self.release.notes or "No release notes.")

    def update_later(self):
        if self.release:
            self.cfg["update_skip"] = self.release.version
            self._save()
        self.update_bar.hide()

    def update_now(self):
        rel = self.release
        if rel is None or self._updating:
            return
        if self.track_btn.isChecked():
            if QMessageBox.question(self, "Stop tracking?", "Tracking is on. Stop tracking to update now?") != QMessageBox.Yes:
                return
            self.set_tracking(False)
        name, sha = updater.pick_asset(rel)
        if not updater.can_self_update() or not name:
            self._update_fallback(rel.page, "This copy can't update itself here." if getattr(sys, "frozen", False) else "")
            return
        self._updating = True
        for b in self.update_btns:
            b.setEnabled(False)
        self.update_label.setText(f"Downloading PTZ Tracker {rel.version}…")
        self.update_progress_bar.setValue(0)
        self.update_progress_bar.show()
        dest = os.path.join(updater.work_dir(), name)

        def go():
            try:
                updater.download(rel.url(name), dest, lambda f: self.update_progress.emit(int(f * 100)))
                if not updater.verify_sha256(dest, sha):
                    raise RuntimeError("the download did not match its checksum")
                self.update_ready.emit(dest, "")
            except Exception as e:
                self.update_ready.emit(dest if os.path.exists(dest) else "", str(e) or type(e).__name__)

        threading.Thread(target=go, daemon=True).start()

    def _update_progress(self, pct):
        self.update_progress_bar.setValue(pct)

    def _update_ready(self, path, err):
        try:
            if err:
                raise RuntimeError(err)
            self.update_label.setText("Installing the update…")
            if platform_paths.IS_WIN:
                updater.apply_windows(path)
            else:
                updater.apply_mac(path)
            log.info("update handed over, quitting")
            QTimer.singleShot(300, self._quit_for_update)
        except Exception as e:
            log.exception("update failed")
            self._updating = False
            self.update_progress_bar.hide()
            for b in self.update_btns:
                b.setEnabled(True)
            self.update_label.setText(f"PTZ Tracker {self.release.version} is available")
            self._update_fallback(path or self.release.page, f"The update couldn't be installed automatically ({e}).")

    def _update_fallback(self, target, message):
        """Open the downloaded file (or the release page) so the update can be done by hand."""
        if message:
            QMessageBox.warning(self, "Update", message + "\n\nIt will open now so you can finish by hand.")
        if target and os.path.exists(target):
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))
        else:
            QDesktopServices.openUrl(QUrl(target or updater.RELEASES_PAGE))

    def _quit_for_update(self):
        self.close()
        QApplication.quit()
