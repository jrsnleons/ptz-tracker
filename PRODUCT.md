# Product

<!-- impeccable:product-schema 1 -->

## Platform

desktop

Native desktop app built with Qt (PySide6), shipped for macOS (Apple Silicon, 13+) and Windows 10/11 x64. Not one of Impeccable's web/ios/android/adaptive values: treat it as a Qt desktop tool, not a web page.

## Users

- The main operator is the developer. They are fluent in Canon's Remote Camera Control Application (RCCA) and expect the same controls: separate pan/tilt (1–16) and zoom (0–100 %) speeds, the camera's own presets with per-preset speed, and picture settings on the main screen.
- Rotating church volunteers of mixed technical skill also run it. Some are non-technical and run it live with little training. The app must stay safe and readable for the least experienced person on the rota.

## Product Purpose

PTZ Tracker keeps a Canon CR-N300 PTZ camera on the speaker during a church service. It follows one person's head automatically, and gives the operator fast manual control (keys, drag on the video, presets) when they need to take over. Success: a steady, well-framed shot of the speaker without the operator fighting the camera, and a camera that never runs off somewhere embarrassing while live.

## Positioning

Built for one church, one camera, one room. It should feel like Canon's RCCA plus head tracking: the camera, not the app, is the source of truth for presets and picture settings. It is not a general product for other venues. Decisions can assume the CR-N300, NDI video, and this church's network and stage.

## Operating Context

- Runs during live services, with streaming software (OBS, vMix or similar) open at the same time on the same machine.
- The booth is dim. The UI is dark, and status must read at a glance.
- Operators drive mainly from the keyboard. Every action has rebindable keys; the mouse is secondary.
- The camera's own video comes in over NDI; control goes over NDI PTZ or VISCA over IP.
- Releases update themselves in-app, but never while tracking without asking first.

## Capabilities and Constraints

- Head detection (YOLOv8, ONNX) with click-to-pick or auto-pick, PID-driven pan/tilt/zoom, pan/tilt/zoom locks.
- Stage boundary: return to a stage-shot preset when the speaker leaves or is lost, then resume tracking.
- Five manual speed levels, ease in/out, zoom-scaled pan speed.
- 20 presets in two banks, renamable; the stage-shot slot is protected.
- Picture settings (exposure, white balance; more only if the camera reports them).
- Ignore zone for the audience: heads there are never picked.
- Using any manual control pauses tracking output until released.
- Old config files must always keep loading.

## Evidence on Hand

- `README.md`: full user manual.
- `docs/DESIGN-1.2.md`: 1.2 feature spec.
- `assets/icon.*`: app icon.
- No user research, testimonials or usage data exist. Do not invent them.

## Product Principles

1. The live shot comes first. Nothing the app does should jerk the camera or move it somewhere unexpected while on air.
2. A nervous volunteer must be able to recover in one keypress (stop camera, release target, go to stage shot).
3. Glanceable over detailed: state (tracking on/off, target, connection) must read across a dim booth.
4. Keyboard first, mouse always available.
5. Never surprise the operator: no auto-install, no silent overwrite of the stage shot.

## Accessibility & Inclusion

Dim-room use: keep contrast high and state readable at a distance. Mixed-skill operators: plain labels, tooltips that name the key.
