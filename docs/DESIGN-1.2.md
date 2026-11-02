# PTZ Tracker 1.2 — design

Scope: custom key binds, keyboard speed levels, more presets, smoother WASD, camera picture settings, and one release for Mac + Windows that updates itself from inside the app.

All work lands on `windows-port` (it already runs on both OSes). After 1.2 ships, `master` is fast-forwarded to it so there is one branch again.

---

## 1. Custom key binds

**Settings → new "Shortcuts" tab.** One row per action: name, current key, *Change* button, *Reset*. *Change* listens for the next key press (Esc cancels). If the key is already used, the row that had it is cleared and shown in amber ("was Zoom in"), so there are never two actions on one key. *Restore defaults* at the bottom.

Actions (default in brackets):

| Held while pressed | Pressed once |
|---|---|
| Pan left [A / ←] · Pan right [D / →] | Start/stop tracking [Space] |
| Tilt up [W / ↑] · Tilt down [S / ↓] | Release target [Esc] |
| Zoom in [E / +] · Zoom out [Q / −] | Stop camera [X] |
| Focus near [ [ ] · Focus far [ ] ] | Home [H] · Go to stage shot [G] |
| | Focus once [F] |
| | Slower [Z] · Faster [C] (new, §2) |
| | Next preset bank [B] (new, §3) |
| | Recall preset 1–10 [1…9, 0] · Store = Shift + that key |

- Each action holds up to two keys (primary + alternate), which is how WASD and arrows coexist today.
- Stored in `config.json` as `"keys": {"pan_left": ["A", "Left"], ...}` using `QKeySequence` strings, so they read the same on both OSes. Missing entries fall back to defaults, so old configs keep working.
- The button labels and tooltips that show keys ("+ E", "Once F", "1–9 recall") are built from the binds, so they stay correct after a change.
- `eventFilter` swaps the hard-coded `if k == …` chain for a `key → action` lookup built from the config. Behaviour is otherwise unchanged (auto-repeat ignored, text fields keep their keys, ⌘, / Ctrl+, still opens Settings and is not rebindable).

## 2. Speed levels from the keyboard

**Five speed levels** replace the single Manual speed slider: `1 Crawl · 2 Slow · 3 Medium · 4 Fast · 5 Max`.

- **Z** = one level slower, **C** = one level faster (rebindable). No wraparound, so mashing Z always lands on Crawl.
- The Manual panel shows the levels as a 5-segment selector you can also click; the active level is lit. A short toast on the video ("Speed 2 · Slow") confirms a key press, so you don't need to look at the panel.
- Each level's pan/tilt and zoom speed is editable under Settings → Shortcuts → *Speed levels* (two sliders per level). Defaults: pan/tilt 0.03 / 0.08 / 0.18 / 0.35 / 0.7, zoom 0.15 / 0.25 / 0.4 / 0.6 / 1.0.
- Changing level while a key is held takes effect immediately.
- Saved level is restored on launch.

## 3. More preset slots

- **20 slots**, shown as a 5 × 4 grid (was 9).
- Keyboard: 1–9, 0 recall slots 1–10. **B** switches the number keys to **bank 2** (11–20); the grid highlights the active bank and the bank header reads "Keys → 11–20". B again goes back. Shift+number stores into the active bank.
- **Names:** right-click a slot → *Rename…* (e.g. "Pulpit", "Choir"). The name shows under the number. Stored in config as `"preset_names": {"3": "Pulpit"}`.
- **Stage shot slot:** whichever slot equals the stage-shot preset number (default 10) is labelled *Stage* and tinted, and storing into it asks to confirm, because it would overwrite the stage shot. No renumbering, so existing setups keep working.
- The camera supports far more than 20 (VISCA up to 127, NDI 0–99), so this is just UI.

## 4. Smoother WASD / arrows

What's wrong today:
1. **Speed ignores zoom.** Zoom already has a floor and its own curve; pan/tilt sends `manual_speed` straight through. When zoomed in, the same angular speed sweeps across the picture many times faster, so it feels twitchy.
2. **Steps are coarse at the low end.** Over VISCA, 0.05 × 24 rounds to speed 1, and 0.1 to 2, so the bottom of the slider is only two real speeds. Over NDI, low values go straight to the camera with the same problem.
3. **Instant on/off.** Pressing a key jumps straight to full speed; releasing stops dead. On camera that reads as a jerk.

Fix (manual moves only; tracking is untouched):
- **Zoom-scaled speed.** When the camera position is readable (Camera IP filled in), scale pan/tilt by `current field of view / widest field of view`, clamped to at least 15 %. At full tele the same key moves the picture at about the same on-screen rate as at wide. Without a position readout, nothing changes. Toggle: *Slow down pan/tilt when zoomed in* (on by default).
- **Finer low end.** Levels are defined in the camera's own units, so Crawl really is the camera's slowest step.
- **Ease in / ease out.** Manual pan/tilt ramps to target speed over ~0.25 s and back down over ~0.15 s on release, stepped at 30 Hz on a timer. Commands only go out when the rounded value changes (same as now), so traffic stays low. X / Stop camera still stops instantly with no ramp.
- Diagonals (W+D) are normalised so they aren't faster than straight moves.

## 5. Camera picture settings (brightness, contrast and the rest)

**Settings → new "Picture" tab.** Changes are sent live as you drag, so you can watch the program feed.

```
┌ Picture ──────────────────────────────────────────────┐
│ Exposure    ( Auto ) ( Manual )                        │
│   Brightness  ───────●────  +2      (Auto mode)        │
│   Iris        ────●───────  F4.0    (Manual mode)      │
│   Gain        ──●─────────  6 dB                       │
│   Shutter     ─────●──────  1/60                       │
│ White balance ( Auto ) ( Indoor ) ( Outdoor ) ( Once ) ( Manual ) │
│   Red         ─────●──────                             │
│   Blue        ────●───────                             │
│ Contrast      ─────●──────        (only if camera has it) │
│ Sharpness     ────●───────        (only if camera has it) │
│ Saturation    ─────●──────        (only if camera has it) │
│                                                        │
│ Looks: [Sunday morning ▾] [Save look…] [Delete]        │
│ [Reset camera to auto]                                 │
└────────────────────────────────────────────────────────┘
```

**How each control reaches the camera.** The church PC drives the camera over NDI and VISCA didn't work there, so NDI is the primary path:

| Control | NDI PTZ (church setup) | VISCA over IP | Canon HTTP (when Camera IP is set) |
|---|---|---|---|
| Exposure auto / manual | yes | yes | — |
| Brightness (exposure level) | yes (`exposure level`) | yes (Bright / exposure comp) | — |
| Iris / gain / shutter | yes (fine exposure) | yes | — |
| White balance modes, red/blue | yes | yes | — |
| Contrast, sharpness, saturation | **no** (NDI has no command) | sharpness only (Aperture) | **probed**, see below |

- **Contrast and the other picture-profile settings** aren't part of NDI PTZ, and Canon's HTTP parameter names for them on the CR-N300 aren't publicly documented. Plan: on connect, read the camera's full `info.cgi` list, write it to the log, and show Contrast / Sharpness / Saturation only if matching items exist. If they don't show up the first time at church, the log tells us the exact names to wire up in a quick follow-up. The tab says "Not available on this camera over NDI" rather than showing dead sliders.
- **Readback:** NDI and VISCA can't reliably report the current picture values, so sliders start from the last values PTZ Tracker sent (saved per camera). *Reset camera to auto* puts exposure and white balance back to auto.
- **Looks (saved picture presets):** *Save look…* stores the current picture values under a name ("Sunday morning", "Evening, lights down"). Picking one sends all of them. Looks are app-side, separate from the camera's PTZ presets, so recalling a PTZ preset doesn't change the picture.

## 6. One release, updates from inside the app

**Release:** one tag `v1.2.0` → one GitHub release with both builds:
- `PTZ-Tracker-Setup.exe`, `PTZ-Tracker-windows.zip`
- `PTZ-Tracker-mac.zip`
- `latest.json`: `{"version": "1.2.0", "notes": "...", "assets": {"windows": "...Setup.exe", "mac": "...mac.zip"}, "sha256": {...}}`

A single workflow (`release.yml`, replaces `build-windows.yml`) runs a Windows job (`windows-latest`) and a Mac job (`macos-14`, Apple Silicon), each self-testing its build, then a publish job that writes `latest.json` and creates the release. The version lives in one place, `ptztracker/version.py`, read by both specs and the installer.

**In the app:**
- On launch (after 10 s, so it never slows startup) and every 6 hours, fetch `latest.json` from the latest release. Failure is silent (church network may be offline).
- Newer version found → a small bar under the toolbar: "PTZ Tracker 1.2.1 is available — *What's new* · *Update now* · *Later*". *Later* hides that version until the next one.
- Also **Settings → About**: current version, *Check for updates* button, *Check automatically* toggle (on by default).
- **Update now** downloads in the background with a progress bar, checks the SHA-256 against `latest.json`, then:
  - **Windows:** runs the downloaded installer with `/VERYSILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS`, then quits; the installer relaunches the app. Same per-user install location, so no admin prompt. Settings in `%APPDATA%` are untouched.
  - **Mac:** unzips to a temp folder, starts a small helper script that waits for the app to quit, replaces the `.app` in place, and relaunches it. Files the app downloads itself aren't quarantined, so there's no right-click → Open again. If the app's folder isn't writable, it falls back to opening the download in Finder.
- **Never during a service:** if tracking is on, *Update now* asks to stop tracking first. Updates never install on their own.

**Risks:**
- Avast on the church PC already blocked the unsigned exe once. The silent installer may be blocked the same way; the fallback is "Download installer" opening the file in Explorer. Code-signing would fix this but costs money, so it's out of scope.
- Builds older than 1.2 (1.1.3 on the church PC, 1.0.0 on Mac) can't update themselves. Each needs one manual install of 1.2.0, then updates work from the app.
- The Mac build stays ad-hoc signed (first install still needs right-click → Open).

## 7. Order of work

1. `version.py`, key-bind layer (`keys.py`) and the `eventFilter` rewrite.
2. Speed levels + smoother manual motion (ramp, zoom scaling, diagonal normalisation).
3. 20 preset slots, banks, names, stage slot label.
4. Picture tab: backend methods on `NDIPTZ` / `VISCAoverIP`, Canon `info.cgi` probe, looks.
5. Updater (`updater.py`) + About section + update bar.
6. `release.yml` (Mac + Windows + `latest.json`), README and release notes.
7. Tests: key-map round trip and conflicts, speed ramp, preset bank mapping, update version compare and `latest.json` parsing, fake-camera picture commands. Then build both, run the self-tests in CI, tag `v1.2.0`.

Not verifiable here: the real camera (contrast probe, smoothness feel) and the update from 1.2.0 → 1.2.1 on the church PC. I'll ship a `v1.2.1` test bump right after so the in-app update can be tried end to end.
