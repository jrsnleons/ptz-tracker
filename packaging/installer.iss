; Inno Setup script for the Windows installer. build_win.ps1 runs it after PyInstaller:
;   iscc /DAppVersion=1.2.0 packaging\installer.iss  ->  dist\PTZ-Tracker-Setup.exe
; Without /DAppVersion the version is read from ptztracker\version.py (the one place it lives).
#ifndef AppVersion
  #define VerFile FileOpen(SourcePath + "..\ptztracker\version.py")
  #define VerLine FileRead(VerFile)
  #expr FileClose(VerFile)
  #define AppVersion Copy(VerLine, Pos('"', VerLine) + 1, RPos('"', VerLine) - Pos('"', VerLine) - 1)
#endif

[Setup]
AppId={{6B7F2C1E-2D4A-4C55-9E0B-5A1F0C3D7E21}
AppName=PTZ Tracker
AppVersion={#AppVersion}
AppPublisher=LWNRA
DefaultDirName={autopf}\PTZ Tracker
DefaultGroupName=PTZ Tracker
DisableProgramGroupPage=yes
; per-user install by default, so no admin prompt; admins can still pick all users
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=PTZ-Tracker-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\PTZ Tracker.exe
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\PTZ Tracker\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\PTZ Tracker"; Filename: "{app}\PTZ Tracker.exe"
Name: "{autodesktop}\PTZ Tracker"; Filename: "{app}\PTZ Tracker.exe"; Tasks: desktopicon

[Run]
; normal install: the "Start PTZ Tracker" tick box on the last page
Filename: "{app}\PTZ Tracker.exe"; Description: "Start PTZ Tracker"; Flags: nowait postinstall skipifsilent
; silent install (the in-app updater): no last page, so start the app again straight away
Filename: "{app}\PTZ Tracker.exe"; Flags: nowait; Check: WizardSilent
