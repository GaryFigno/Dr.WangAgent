; Inno Setup script for Dr.Wang Agent
; Build the PyInstaller bundle first, then compile this file:
;
;   python packaging/build.py --clean --installer
;   # or:  iscc packaging/drwang.iss
;
; Requires Inno Setup 6+: https://jrsoftware.org/isinfo.php

#define MyAppName "Dr.Wang Agent"
#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#define MyAppPublisher "Dr.Wang contributors"
#define MyAppURL "https://github.com/YOUR_GITHUB_USERNAME/drwang-agent"
#define MyAppExeName "Dr.Wang.exe"
#define DistDir "..\dist\Dr.Wang"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=Dr.Wang-{#MyAppVersion}-windows-setup
SetupIconFile=..\assets\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeSetup(): Boolean;
begin
  if not FileExists(ExpandConstant('{#DistDir}\{#MyAppExeName}')) then
  begin
    MsgBox('Bundle not found:'#13#10 + ExpandConstant('{#DistDir}\{#MyAppExeName}') +
      #13#10#13#10'Run: python packaging/build.py --clean', mbError, MB_OK);
    Result := False;
  end
  else
    Result := True;
end;
