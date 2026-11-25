# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

PTZ Tracker: a PySide6 desktop app (Mac + Windows) that follows a speaker's head with a Canon CR-N300 PTZ camera at church. Pipeline: NDI video → YOLOv8 head detection (ONNX, `models/head_640_v8s.onnx`) → pick one head → PID → camera speed commands over NDI PTZ or VISCA over IP. README.md is the user manual and is detailed; read it before changing behaviour.

## Commands

Python 3.12. Mac venv is `.venv` (created by `./run.sh` with `uv`); Windows uses `.venv-win` (see `packaging/build_win.ps1`). Windows deps: `requirements-windows.txt` (`onnxruntime-directml` instead of `onnxruntime`, plus PyInstaller).

- Run from source: `./run.sh` (Mac) or `python -m ptztracker`
- Tests are plain scripts, not pytest. Run one from the repo root: `python -m tests.test_keys` (likewise `test_speed`, `test_presets`, `test_picture`, `test_updater`, `test_homing`, `test_worker`, `test_remote`). Each prints PASS/FAIL lines and ends with `all passed` or `SOME FAILED`. Set `QT_QPA_PLATFORM=offscreen` on a headless machine. Known: `test_updater`'s "mac bundle from the executable path" check fails on Windows.
- `python -m tests.camera_stage_test` drives the **real camera** on the church network (moves it, overwrites preset 10). Do not run it casually.
- Smoke test a built app: `"PTZ Tracker.exe" --selftest result.txt` → last line `OK` (`ptztracker/selftest.py`; CI runs it).
- Build Windows: `powershell -ExecutionPolicy Bypass -File packaging\build_win.ps1` → `dist\PTZ Tracker\`, zip, and installer if Inno Setup is installed.
- Build Mac: `packaging/build_app.sh` → `dist/PTZ Tracker.app` + zip. PyInstaller can't cross-compile.
- No linter or formatter config in the repo.

## Architecture

- `ptztracker/app.py` (~1700 lines) is `MainWindow`; the rest of the old monolith is split into `config.py` (defaults, load/save), `worker.py` (`TrackerWorker`), `video_view.py`, `widgets.py` (look + helpers, imported by `remote_ui` too), `update_ui.py` (`UpdateMixin`), `picture_panel.py`. In short:
  - `TrackerWorker(QThread)` (`worker.py`): owns the source, detector, `TargetTracker`, `CamController` and stage `Homing`. Its loop reads the latest frame, detects, tracks, sends PTZ commands, and emits `frame_ready` dicts to the GUI. GUI → worker communication is via attributes guarded by `self._lock` (click, release, manual_active, home requests). `gui_busy` makes the worker drop frames while the GUI is still painting.
  - `VideoView` (`video_view.py`): paints the frame and overlays; emits clicks, joystick drags, wheel zoom and stage-box edits in frame coordinates.
  - `MainWindow`: all panels, Settings dialog (Shortcuts, Picture, About), key handling via `eventFilter`, config load/save, update bar.
- Pure logic lives in small modules so it can be tested without Qt or hardware:
  - `tracking.py`: `TargetTracker` (IoU-based target persistence), `PID`, `TrackSettings`, `CamController` (error → pan/tilt/zoom speed).
  - `stage.py`: stage boundary, `CanonPositionPoller` (reads pan/tilt/zoom from the camera's web API when Camera IP is set), pixel↔angle maths, `Homing` state machine for the return-to-stage-shot trip.
  - `motion.py`: manual speed levels, legacy `manual_speed` migration, ease `Ramp`, diagonal normalising, zoom-scaled speed.
  - `keys.py`: `Binds` action→keys map (two keys per action, `QKeySequence` strings), preset banks.
  - `picture.py`: exposure/white-balance values, looks and send groups for NDI PTZ / VISCA (which cannot read the picture back).
  - `updater.py`: `latest.json` parsing, version compare, SHA-256 check, platform-specific apply (Windows silent installer, Mac helper swaps the `.app`).
- `remote.py` + `remote_ui.py` + `web/remote.html` (1.3): phone/tablet remote. `remote.py` is an aiohttp websocket server on its own thread (PIN pairing, `HoldWatchdog` stops the camera if a held control goes quiet for 300 ms). `remote_ui.py` turns device commands into `MainWindow` actions and owns the Settings → Remote tab.
- `canon_http.py`: the Canon WV-HTTP API, as Canon's Remote Camera Control Application uses it (reverse-engineered from RCCA 1.7.1; unconfirmed on the CR-N300 until `tests/canon_probe.py` is run there). `CanonClient` owns one poll thread (position every 80 ms + the full info list every 2 s, emitted to `MainWindow.canon_state`) and one writer thread where the newest command per group wins; the session opens lazily on the first command. It is also the position `poller` the worker and stage code use. `CanonAPI(PTZBase)` sends RCCA's speeds (pan/tilt step L → `floor(5000*(L-1)/15)`, zoom % → `floor(round(max*pct,1))`), preset recall/store with speed, and any picture key via `set`.
- `picture_panel.py`: `CanonPicturePanel` builds controls only for the `c.1.*` keys the camera reports (`.list` → combo, `.min/.max` → slider).
- `ptz.py`: camera backends. Every backend uses normalised speeds in `[-1, 1]` (pan + = right, tilt + = up, zoom + = tele, focus + = far). `PTZBase` is the no-op default; `VISCAoverIP` and `NDIPTZ` implement it. Backends are wrapped in `Guarded`, which logs failed commands (rate-limited) instead of raising: an exception in a Qt handler can kill the app on Windows. `COMMANDS` is derived from `PTZBase`'s public methods, so a new command must be declared on `PTZBase` to get that protection.
- `sources.py`: `NDIFinder` (discovery, plus direct-IP probing from saved Camera IP), `NDISource`, `CVSource` (RTSP/webcam). Sources push into `LatestFrame`, a single-slot buffer that always keeps only the newest frame.
- `platform_paths.py`: all OS-specific paths (config, logs, bundled resources under PyInstaller) and ONNX providers / webcam backends. Put new OS branching here.
- `detector.py`: `HeadDetector` wraps an onnxruntime session (CoreML on Apple Silicon, DirectML on Windows).

## Config and compatibility

- Speeds: pan/tilt is a step 1–16 (`cfg["pt_step"]`, `motion.pt_speed`), zoom a 0.01–1 float shown as %. Backends take 0..1 floats; only `CanonAPI` maps them to Canon wire values.
- Config is one JSON file (`~/Library/Application Support/PTZ Tracker/config.json`, `%APPDATA%\PTZ Tracker\config.json`). Old configs must keep loading: missing keys fall back to defaults, and old fields are migrated (e.g. `manual_speed` → speed level). Keep that rule for any new field.
- Logs: `perf.log` (fps/timings each second) and `app.log` (errors) under `~/Library/Logs/PTZ Tracker/` or `%LOCALAPPDATA%\PTZ Tracker\Logs\`.

## Releases and branches

- `ptztracker/version.py` is the single version source; specs, `build_win.ps1`, the installer and `.github/workflows/release.yml` read it.
- Release: bump `version.py`, update `packaging/RELEASE_NOTES.md` (its *What's new* section becomes the in-app notes), commit, tag `vX.Y.Z`, push tags. The workflow builds both platforms, self-tests, and publishes with `latest.json` (`packaging/make_latest.py`). The tag must match `version.py` or publish fails.
- `windows-port` is the cross-platform source of truth; `master` gets fast-forwarded to it after 1.2 ships. Check which branch you are on before release work.
- `docs/DESIGN-1.2.md` is the design spec for the 1.2 features.
