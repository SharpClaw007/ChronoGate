; Inno Setup script — wraps the PyInstaller one-folder build into a Windows
; installer.  Build after PyInstaller has produced dist\ChronoGate\:
;
;   iscc /DAppVersion=0.16.1 packaging\chronogate.iss
;
; The version is passed in by CI (from chronogate/__init__.py); it defaults so
; the script also runs by hand.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "ChronoGate"
#define AppPublisher "Rice University"
#define AppExeName "ChronoGate.exe"

[Setup]
AppId={{7C0B4E2A-9D3F-4E5A-B1C6-CHRONOGATE01}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user install needs no admin; drop to lowest for a friction-free install.
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=ChronoGate-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; Icon shown in Add/Remove Programs.
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The entire PyInstaller one-folder output.
Source: "..\dist\ChronoGate\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
