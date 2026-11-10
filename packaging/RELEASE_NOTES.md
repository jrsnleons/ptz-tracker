## PTZ Tracker 1.3.0

### What's new
- **Control the camera from a tablet or phone.** While PTZ Tracker is open on the PC, an iPad, Android tablet or phone on the same Wi-Fi can run it from the browser. There's nothing to install. Turn it on in *Settings → Remote*, then scan the QR code (or type the address and the 4-digit PIN). Use *Add to Home Screen* to open it like an app.
- The remote has a live preview (tap a head to follow it, drag to move the camera), start/stop tracking, Release, Auto-pick, Stage shot, presets 1–20, a joystick, zoom, focus, the five speed levels and Stop camera. On a phone, Move and Presets are two tabs.
- **Safe by design.** Remote control is off until you turn it on, works only on your own network, and each device pairs once with the PIN. *New PIN* changes the PIN; *Forget all paired devices* unpairs every device. If the tablet drops off Wi-Fi, locks or switches apps while you hold the joystick or zoom, the camera stops within a third of a second.
- The PC and any remotes can all drive at once, the last touch wins, and tracking, speed and preset names stay in sync on every screen. Storing presets from a remote is off by default (*Let remotes store presets*), and the stage shot preset can only be stored from the PC.
- On Windows, allow PTZ Tracker through the firewall on **Private networks** when asked, or the tablet can't connect.

### In 1.2.1
- Checks that updating from inside the app works. Nothing else changed since 1.2.0.

### In 1.2.0
- **Pick your own keys.** *Settings → Shortcuts* lists every action with its key. Click a key, press the new one, done. If the key was already used, the old action is cleared and shows a note, so a key never does two things. *Restore default keys* undoes it all.
- **Five speed levels from the keyboard.** Press **Z** for slower and **C** for faster (Crawl, Slow, Medium, Fast, Max). A short message on the video tells you the level, and the Manual panel has buttons for it too. Each level's speed can be changed in *Settings → Shortcuts*.
- **Smoother arrow keys / WASD.** The camera now eases in and out instead of jerking, diagonals are no faster than straight moves, and when you are zoomed in the camera pans more gently (it needs the Camera IP filled in).
- **20 presets**, in two banks. The number keys use presets 1–10; press **B** to switch them to 11–20. Right-click a preset to give it a name like "Pulpit". The stage shot preset is marked *Stage*, and the app asks before you save over it.
- **Picture settings.** *Settings → Picture* has exposure (auto or manual brightness, iris, gain, shutter) and white balance (auto, indoor, outdoor, once, manual). Changes happen live while you drag. *Save look…* keeps a named combination such as "Sunday morning"; *Reset camera to auto* undoes your changes. Contrast, saturation and sharpness only appear when the camera allows them (they can't be sent over NDI); the app logs what the camera offers so they can be added later.
- **Updates from inside the app.** When a newer version is out, a bar appears at the top: *What's new*, *Update now*, *Later*. Nothing installs by itself, and if tracking is on the app asks you to stop it first. *Settings → About* has *Check for updates* and a switch for automatic checks.
- One download page for Windows and Mac, one version number.

### Updating from an older version
**1.1.x on Windows and 1.0 on Mac can't update themselves.** Install 1.2.0 by hand once (below). After that, updates come from inside the app. Your settings and presets are kept.

### Install
**Windows (10/11):** download `PTZ-Tracker-Setup.exe` and run it (no admin needed). Or download `PTZ-Tracker-windows.zip`, unzip it anywhere and double-click `PTZ Tracker.exe`.
On first run, when Windows Firewall asks, allow PTZ Tracker on **Private networks** so it can find the NDI camera. Windows may show "Windows protected your PC" because the app isn't code-signed: click **More info → Run anyway**.

**Mac (Apple Silicon, macOS 13+):** download `PTZ-Tracker-mac.zip`, unzip it and drag *PTZ Tracker.app* to Applications. The first time, right-click it and choose **Open** (it isn't notarized). Allow **Local Network** access when macOS asks.

If the app ever closes by itself, send `app.log` and `crash.log` (*Settings → Camera → Open log folder*).
