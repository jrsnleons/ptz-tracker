## PTZ Tracker for Windows

**Install:** download `PTZ-Tracker-Setup.exe` and run it (no admin needed). It adds PTZ Tracker to the Start menu and, if you tick it, the desktop.
**Or run without installing:** download `PTZ-Tracker-windows.zip`, unzip it anywhere and double-click `PTZ Tracker.exe`.

On first run, when Windows Firewall asks, allow PTZ Tracker on **Private networks** so it can find the NDI camera.
Windows may show "Windows protected your PC" because the app isn't code-signed: click **More info → Run anyway**.

### What's new
- **Camera video fixed.** If GPU (DirectML) detection failed on a PC, the video thread stopped and the picture never appeared. Detection now checks the GPU on start-up and falls back to the CPU, and a bad frame can no longer stop the video.
- **New look**, the same redesigned window as the Mac app: a connect card over the video, a live control rail, a settings sheet, and a dark Windows 11 title bar.
- **Finds the camera more reliably.** Once you have connected with the *Camera IP* filled in, NDI also looks for the camera at that IP directly from the next launch (helps when the firewall or network blocks discovery), and a typed source name like `LWNRA-PTZ_CAM` matches the full NDI name.
- **Webcams and capture cards:** tries DirectShow, then Media Foundation, and says plainly if a camera sends no video.
- If there's still no picture after a few seconds, the window says what to check. Errors are logged to `%LOCALAPPDATA%\PTZ Tracker\Logs\app.log` (*Settings → Camera → Open log folder*); please send that file with any feedback.
- App icon on the exe, installer, taskbar and shortcuts.
