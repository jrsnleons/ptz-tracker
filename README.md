# PTZ Tracker (macOS)

A Mac port of the Windows `tracker.exe`, with manual PTZ controls added. It uses the same head-detection model (`models/head_640_v8s.onnx`) and the same pipeline: NDI video → YOLOv8 head detection → follow one head → PID → camera speed commands. On Apple Silicon it runs on CoreML (~5 ms per frame).

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
- Keys: arrows/WASD pan-tilt · Q/E zoom · H home · Space start/stop tracking · Esc release target · 1-9 recall preset · Shift+1-9 store preset.
- Settings are saved to `~/Library/Application Support/PTZ Tracker/config.json`.

### Stage boundary (return to the stage shot)
1. Connect with **Camera IP** filled in. The app reads the camera's pan/tilt/zoom from the CR-N300's web API.
2. Frame the wide stage shot, press **Draw stage…**, and drag a box around the stage. That shot is saved as the *stage shot preset* (default 10, clear of the 1-9 buttons).
3. With tracking on, if the speaker stays outside the box for *Left stage for* seconds, or nobody is tracked for *Lost for* seconds, the camera goes back to the stage shot. The camera also never pans past the stage edges.
4. With *Resume tracking* on, it picks up whoever steps back onto the stage.

On the way back the app recalls the preset and checks the position readout to confirm the camera arrived. It retries once, and if the camera still isn't there it drives to the saved angles itself. If the camera reports a VISCA error, it shows in the Connection status.

### Tests
- `.venv/bin/python -m tests.test_homing` runs a simulated camera, no hardware needed.
- `.venv/bin/python -m tests.test_worker` checks the worker with fake video: stall stop, homing without video, GUI frame dropping.
- Every second the app writes fps and timings to `~/Library/Logs/PTZ Tracker/perf.log` (also shown in the video HUD).
- `.venv/bin/python -m tests.camera_stage_test` uses the real camera on the church network. It moves the camera and stores preset 10, then puts the camera back where it started.

### Editing the stage and the return trip
- **Edit stage** shows handles on the stage box. Drag an edge or corner to adjust it, or drag the middle to move the whole box. No need to redraw.
- **Resume shot**: frame the position and zoom you want when someone comes back, press *Set resume shot from current view*, and tick *When they return, first move to a set position and zoom*. *Resume move speed* caps how fast the camera travels there. Leave the tick off to just keep following from the stage shot.
- *Resume tracking when someone is back on stage* turns the follow-again behaviour on or off.

### Tuning
- **Speed / Max speed**: how hard the camera chases, and the cap on that.
- **Deadzone**: the dashed box. The camera only moves once the head leaves it, which keeps it from hunting.
- **Curve**: above 1 means gentle near the target and faster when far away.
- **Frame X/Y**: where the head should sit in the shot (Y 0.33 = rule-of-thirds headroom).
- **Head size**: target head height for auto-zoom (only when zoom is unlocked).
