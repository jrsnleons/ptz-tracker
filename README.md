# PTZ Tracker

PTZ Tracker 1.2: head tracking and manual PTZ control for the Canon CR-N300, on Mac and Windows. It began as a Mac port of the Windows `tracker.exe`. It uses the same head-detection model (`models/head_640_v8s.onnx`) and the same pipeline: NDI video → YOLOv8 head detection → follow one head → PID → camera speed commands. On Apple Silicon it runs on CoreML (~5 ms per frame).

## Download

One release per version on the [Releases](https://github.com/jrsnleons/ptz-tracker/releases) page, with both builds:

- **Windows** (10/11 x64): `PTZ-Tracker-Setup.exe` installs it with a Start menu and desktop shortcut. Or download `PTZ-Tracker-windows.zip`, unzip anywhere and run `PTZ Tracker.exe` (no install needed). Allow it through Windows Firewall (Private networks) on first run so NDI discovery works.
- **Mac** (Apple Silicon, macOS 13+): `PTZ-Tracker-mac.zip`. Unzip, drag *PTZ Tracker.app* to Applications, then right-click → Open the first time (it is ad-hoc signed, not notarized).
- `latest.json` is what the app reads to find updates.

### Updates
From 1.2.0 the app checks for a newer release a few seconds after launch and every 6 hours (*Settings → About* has *Check for updates* and a switch for the automatic check). A bar under the toolbar offers *What's new*, *Update now* and *Later*. *Update now* downloads the release, checks its SHA-256 from `latest.json`, then:

- **Windows** runs the installer silently (`/VERYSILENT`, same per-user location, no admin prompt); it relaunches the app.
- **Mac** unzips to a temp folder, and a small helper replaces `PTZ Tracker.app` once the app has quit, clears the quarantine flag and reopens it. If the app's folder isn't writable, it opens the download instead.
- From source (not a packaged app) it only tells you an update exists and opens the release page.

If tracking is on it asks to stop tracking first, and nothing ever installs on its own. If anything fails it shows the error and opens the downloaded file (or the release page). **Builds older than 1.2** (Windows 1.1.x, Mac 1.0) can't update themselves: install 1.2.0 by hand once.

## Branches

`windows-port` is the cross-platform source of truth: the Mac and Windows apps, packaging for both, and the release workflow. `master` is the older Mac-only line and is fast-forwarded to `windows-port` once 1.2 ships. The version lives in `ptztracker/version.py`; the specs, `build_win.ps1`, the installer and the release workflow all read it.

## Releasing

Bump `ptztracker/version.py`, update `packaging/RELEASE_NOTES.md` (the *What's new* section also becomes the in-app notes), commit, then `git tag v1.2.1 && git push --tags`. `.github/workflows/release.yml` builds Windows (`windows-latest`) and Mac (`macos-14`), self-tests each build (`--selftest`), and a publish job writes `latest.json` and creates the GitHub release. *Run workflow* on its own builds and tests both without publishing.

## Windows

Settings are in `%APPDATA%\PTZ Tracker\config.json`, logs in `%LOCALAPPDATA%\PTZ Tracker\Logs\` (`perf.log`, and `app.log` for errors).
If the NDI camera isn't listed, allow PTZ Tracker through Windows Firewall, or connect once with *Camera IP* filled in: from the next launch the app also asks the camera directly instead of relying only on network discovery.
Build on a Windows PC with Python 3.12: `powershell -ExecutionPolicy Bypass -File packaging\build_win.ps1` → `dist\PTZ Tracker\PTZ Tracker.exe`, the zip, and the installer if Inno Setup is installed. PyInstaller can't cross-compile, so on a Mac push a `v*` tag (or run the *Release* workflow): GitHub Actions builds, smoke-tests and publishes it.

## Run

Double-click **`PTZ Tracker.command`** in Finder, or run `./run.sh`. The first run creates `.venv`, which needs `uv` (`brew install uv`).
The first time it connects, macOS will ask to allow **Local Network** access. Click Allow, or the app will find NDI sources but get no video.

## Connecting to the CR-N300

| Setting | Value |
|---|---|
| Video | NDI → `LWNRA-PTZ_CAM (Channel 1)` (press ↻ to refresh) |
| Control | **NDI PTZ** (uses the same NDI link, as the Windows app did), or **VISCA over IP** → camera IP `192.168.254.122`, port `52381` |

VISCA over IP must be turned on in the camera's web settings. If it doesn't respond, try ticking "Raw VISCA". If the camera's DHCP address changes, update the IP. RTSP and webcams also work as video inputs, but RTSP adds latency, so NDI is better for tracking.

## Using it

- **Drag on the video** to move the camera (drag toward where it should look; further = faster). **Scroll wheel** zooms. A plain click without dragging still picks a head.
- **Click a head** in the video to follow that person, or turn on *Auto-pick* to follow the biggest head.
- The **shaded bottom zone** (Ignore below) is for the audience. Heads in it are never picked.
- Locks: *pan* / *tilt* / *zoom* (tilt and zoom are locked by default, like the Windows config).
- Using any manual control pauses tracking output until you let go.
- Settings are saved to `~/Library/Application Support/PTZ Tracker/config.json` (`%APPDATA%\PTZ Tracker\config.json` on Windows).

### Keys
Every key can be changed in *Settings → Shortcuts* (click a key, press the new one; Esc cancels, Backspace clears; a key already in use moves to the new action). Each action holds two keys. Defaults:

| Held while pressed | Pressed once |
|---|---|
| Pan left `A` `←` · right `D` `→` | Start/stop tracking `Space` |
| Tilt up `W` `↑` · down `S` `↓` | Release target `Esc` · Stop camera `X` |
| Zoom in `E` `+` · out `Q` `-` | Home `H` · Go to stage shot `G` |
| Focus near `[` · far `]` | Focus once `F` |
| | Slower `Z` · Faster `C` · Next preset bank `B` |
| | Recall preset `1`–`9`, `0` · store with `Shift` + the key |

`⌘,` / `Ctrl+,` opens Settings and isn't rebindable. Keys are saved as `"keys"` in the config; missing entries fall back to the defaults, so old configs keep working.

### Speed levels
Five levels replace the old Manual speed slider: Crawl, Slow, Medium, Fast, Max. `Z` / `C` step down / up (no wraparound), a toast on the video confirms it, and the Manual panel shows them as 1–5 buttons. Pan/tilt and zoom speed per level are editable in *Settings → Shortcuts*. Old configs with a `manual_speed` get the nearest level. Manual pan/tilt eases in (~0.25 s) and out (~0.15 s), diagonals are normalised, and with the Camera IP set the speed scales with the zoom (*Slow down pan/tilt when zoomed in*). Tracking is not affected; Stop camera is instant.

### Presets
20 slots in a 5 × 4 grid. The number keys recall 1–10; `B` switches them to 11–20 (the grid dims the other bank). `Shift` + a number stores. Right-click a slot to rename it (names are saved as `"preset_names"`). The stage-shot preset is marked *Stage* and asks before it is overwritten.

### Picture settings
*Settings → Picture*: exposure (auto with brightness, or manual iris / gain / shutter) and white balance (auto, indoor, outdoor, once, manual with red/blue), sent live while you drag (throttled to one update per 100 ms). *Save look…* stores a named set of values; *Reset camera to auto* returns exposure and white balance to auto. The church setup uses **NDI PTZ**, which carries exposure and white balance but has no contrast, sharpness or saturation; VISCA adds sharpness (Aperture). With the Camera IP set, the app also reads the camera's `info.cgi` list on connect, writes it to `app.log`, and shows Contrast / Sharpness / Saturation sliders only if the camera has matching items. That part is untested on the real CR-N300. NDI and VISCA can't read the picture back, so sliders start from the last values sent (saved per camera in `"picture"`; looks in `"looks"`).

### Stage boundary (return to the stage shot)
1. Connect with **Camera IP** filled in. The app reads the camera's pan/tilt/zoom from the CR-N300's web API.
2. Frame the wide stage shot, press **Draw stage…**, and drag a box around the stage. That shot is saved as the *stage shot preset* (default 10, shown as *Stage* in the preset grid).
3. With tracking on, if the speaker stays outside the box for *Left stage for* seconds, or nobody is tracked for *Lost for* seconds, the camera goes back to the stage shot. The camera also never pans past the stage edges.
4. With *Resume tracking* on, it picks up whoever steps back onto the stage.

On the way back the app recalls the preset and checks the position readout to confirm the camera arrived. It retries once, and if the camera still isn't there it drives to the saved angles itself. If the camera reports a VISCA error, it shows in the Connection status.

### Tests
Run from the repo root (use the venv's Python; add `QT_QPA_PLATFORM=offscreen` on a machine without a display):
- `python -m tests.test_keys` key binds: defaults, round trip, conflicts, old configs, Shift+digit store, preset banks.
- `python -m tests.test_speed` speed levels and migration, ramp, diagonals, zoom scaling, and the window's manual move loop.
- `python -m tests.test_presets` key handling in the window: banks, names, the stage slot, rebinding.
- `python -m tests.test_picture` VISCA picture bytes through a fake socket, NDI calls on a fake receiver, throttling, looks.
- `python -m tests.test_updater` version compare, `latest.json`, asset pick, SHA-256 (no network).
- `python -m tests.test_homing` runs a simulated camera, no hardware needed.
- `python -m tests.test_worker` checks the worker with fake video: stall stop, homing without video, GUI frame dropping.
- Every second the app writes fps and timings to `~/Library/Logs/PTZ Tracker/perf.log` (also shown in the video HUD).
- `python -m tests.camera_stage_test` uses the real camera on the church network. It moves the camera and stores preset 10, then puts the camera back where it started.

### Editing the stage and the return trip
- **Edit stage** shows handles on the stage box. Drag an edge or corner to adjust it, or drag the middle to move the whole box. No need to redraw.
- **Return to stage speed** (1.00 = the camera's own preset speed). Lower it and the app drives the camera back itself at that capped speed, using the saved stage shot angles.
- **Resume zoom**: zoom to the level you want when someone comes back, press *Set resume zoom from current view*, and tick *When they return, ease to a set zoom while tracking*. Tracking restarts straight away and the position follows the person; only the zoom eases to that level. *Resume zoom speed* caps the zoom and pan/tilt speed while it does.
- *Resume tracking when someone is back on stage* turns the follow-again behaviour on or off.

### Tuning
- **Speed / Max speed**: how hard the camera chases, and the cap on that.
- **Deadzone**: the dashed box. The camera only moves once the head leaves it, which keeps it from hunting.
- **Curve**: above 1 means gentle near the target and faster when far away.
- **Frame X/Y**: where the head should sit in the shot (Y 0.33 = rule-of-thirds headroom).
- **Head size**: target head height for auto-zoom (only when zoom is unlocked).

## Building the Mac app

`packaging/build_app.sh` builds `dist/PTZ Tracker.app` and `dist/PTZ-Tracker-mac.zip` with PyInstaller (installs into `.venv`: `uv pip install --python .venv/bin/python pyinstaller`; set `PYTHON` to use another interpreter). It is ad-hoc signed, so the first launch needs right-click → Open. The app bundles the NDI library and the detection model; settings and the perf log stay in `~/Library`.
