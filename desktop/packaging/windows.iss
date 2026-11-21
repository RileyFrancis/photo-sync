; Inno Setup script: builds PhotoSync-Setup.exe from the PyInstaller output (dist\PhotoSync).
; Run from desktop\:  iscc /DAppVersion=1.0.0 packaging\windows.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{CFDB5A94-85D8-4611-8EC0-09C1028B9D54}
AppName=Photo Sync
AppVersion={#AppVersion}
AppPublisher=Riley Francis
DefaultDirName={autopf}\Photo Sync
DefaultGroupName=Photo Sync
DisableProgramGroupPage=yes
; Per-user install by default: no admin prompt.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=PhotoSync-Setup
SetupIconFile=photosync.ico
UninstallDisplayIcon={app}\PhotoSync.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\PhotoSync\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Photo Sync"; Filename: "{app}\PhotoSync.exe"
Name: "{autodesktop}\Photo Sync"; Filename: "{app}\PhotoSync.exe"; Tasks: desktopicon

[Messages]
FinishedLabel=Photo Sync is installed.%n%nTo connect to an iPhone, Windows also needs Apple's free USB driver: install the "Apple Devices" app from the Microsoft Store (or iTunes) if you haven't already.

[Run]
Filename: "{app}\PhotoSync.exe"; Description: "Open Photo Sync now"; Flags: nowait postinstall skipifsilent
