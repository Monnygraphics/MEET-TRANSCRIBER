; Inno Setup script — builds MeetTranscriber_Setup.exe from the PyInstaller output.
; Compile from the project root:
;     iscc build_tools\installer.iss

#define MyAppName "Local Meet Transcriber"
#define MyAppVersion "1.0.1"
#define MyAppPublisher "MonnyCreaX"
#define MyAppExe "MeetTranscriber.exe"

[Setup]
AppId={{8F3A2C10-7B5E-4D2A-9C1F-MEETTRANSCR01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\MeetTranscriber
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\Output
OutputBaseFilename=MeetTranscriber_Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; Pull in the entire PyInstaller one-folder build.
Source: "..\dist\MeetTranscriber\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
