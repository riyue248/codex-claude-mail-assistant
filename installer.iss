#define MyAppName "Codex 与 Claude Code 邮件助手"
#define MyAppVersion "1.6.0"
#define MyAppExeName "CodexClaudeMailAssistant-1.6.0.exe"

[Setup]
AppId={{B9C3E5DE-77B3-4A9F-8A35-A4D7AB3B4C61}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=CodexClaudeMailAssistant
DefaultDirName={localappdata}\Programs\CodexClaudeMailAssistant
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
OutputDir=..\CodexClaudeMailAssistant-v1.6.0
OutputBaseFilename=CodexClaudeMailAssistant-Setup-1.6.0
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: checkedonce

[Files]
Source: "..\CodexClaudeMailAssistant-v1.6.0\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CodexClaudeMailAssistant-v1.6.0\使用说明.md"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{app}\CodexClaudeMailAssistant-*.exe"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.md"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
var
  InstallModePage: TInputOptionWizardPage;

procedure InitializeWizard;
begin
  InstallModePage := CreateInputOptionPage(
    wpWelcome,
    '选择安装方式',
    '请选择一键安装或自定义安装路径',
    '一键安装会使用推荐目录并创建桌面快捷方式；自定义安装可修改目录和快捷方式选项。',
    True,
    False
  );
  InstallModePage.Add('一键安装（推荐）');
  InstallModePage.Add('自定义安装路径');
  InstallModePage.SelectedValueIndex := 0;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if InstallModePage.SelectedValueIndex = 0 then
    Result :=
      (PageID = wpSelectDir) or
      (PageID = wpSelectProgramGroup) or
      (PageID = wpSelectTasks) or
      (PageID = wpReady);
end;
