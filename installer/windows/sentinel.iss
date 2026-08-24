; Argus NVR — Inno Setup 6 installer script
; Build with: iscc sentinel.iss  (from installer\windows\)
; Or run:     build-installer.ps1
;
; Layout on disk after install:
;   C:\Argus\
;     app\             — Python package + embedded Python + pre-built frontend
;     tools\           — go2rtc.exe, ffmpeg\bin\, nssm.exe
;     data\            — SQLite database  (preserved on upgrade/uninstall)
;     recordings\      — MP4 files        (preserved on upgrade/uninstall)
;     events\          — thumbnails       (preserved on upgrade/uninstall)
;     config\          — go2rtc.yaml (runtime-generated; preserved)
;     logs\            — stdout/stderr    (preserved)
;     .env             — secrets + config (preserved on upgrade/uninstall)

#define AppName    "Argus NVR"
#define AppVersion "1.0.0"
#define AppPublisher "Argus NVR"

[Setup]
AppId={{A3F2C1D4-7B8E-4F1A-9C2D-0E5F6A7B8C9D}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Argus
DefaultGroupName={#AppName}
OutputDir=..\..\dist
OutputBaseFilename=Argus-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\tools\go2rtc.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Dirs]
; These dirs are marked uninsneveruninstall so they survive uninstall by default.
Name: "{app}\data";       Flags: uninsneveruninstall
Name: "{app}\recordings"; Flags: uninsneveruninstall
Name: "{app}\events";     Flags: uninsneveruninstall
Name: "{app}\config";     Flags: uninsneveruninstall
Name: "{app}\logs";       Flags: uninsneveruninstall

[Files]
; App (sentinel package + embedded Python + pre-built frontend + installer scripts)
Source: "payload\app\*";    DestDir: "{app}\app";   Flags: ignoreversion recursesubdirs createallsubdirs
; Tool binaries (go2rtc, ffmpeg, nssm)
Source: "payload\tools\*";  DestDir: "{app}\tools"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
; Step 1: Generate .env with a fresh secret key (skip on upgrade if .env exists)
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -Command ""if (-not (Test-Path '{app}\.env')) {{ & '{app}\app\python\python.exe' '{app}\app\installer\common\generate_env.py' '{app}' '{app}\.env' }}"""; \
  StatusMsg: "Generating configuration..."; \
  Flags: runhidden

; Step 2: Create runtime directories
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -Command ""foreach ($d in 'data','recordings','events','config','logs') {{ New-Item -ItemType Directory -Force ('{app}\' + $d) | Out-Null }}"""; \
  StatusMsg: "Creating directories..."; \
  Flags: runhidden

; Step 3: Register Argus Windows Service via NSSM + add firewall rule
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -File ""{app}\installer\windows\service-install.ps1"" ""{app}"""; \
  StatusMsg: "Registering Argus service..."; \
  Flags: runhidden

; Step 4 (optional): open browser — user-selectable post-install
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -Command ""Start-Sleep 6; Start-Process 'http://localhost:8080'"""; \
  StatusMsg: "Opening Argus in browser..."; \
  Flags: postinstall runhidden unchecked; \
  Description: "Open Argus NVR in my browser now"

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -File ""{app}\installer\windows\service-uninstall.ps1"" ""{app}"""; \
  Flags: runhidden; \
  RunOnceId: "UninstallService"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Answer: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Answer := MsgBox(
      'Do you want to keep your recordings, events, and database?' + #13#10 +
      'Click Yes to keep your data. Click No to delete everything.',
      mbConfirmation,
      MB_YESNO or MB_DEFBUTTON1
    );
    if Answer = IDNO then
    begin
      DelTree(ExpandConstant('{app}\data'),       True, True, True);
      DelTree(ExpandConstant('{app}\recordings'), True, True, True);
      DelTree(ExpandConstant('{app}\events'),     True, True, True);
      DelTree(ExpandConstant('{app}\logs'),       True, True, True);
      DeleteFile(ExpandConstant('{app}\.env'));
    end;
  end;
end;
