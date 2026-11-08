; Установщик Джарвиса. Сборка: iscc packaging\installer.iss  (версия берётся из переменной JARVIS_VERSION)
#define AppVersion GetEnv("JARVIS_VERSION")
#if AppVersion == ""
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{B7A1E2D4-6C3F-4E58-9A0B-5D2C7F1E9A34}
AppName=Джарвис
AppVersion={#AppVersion}
AppPublisher=kitlame7-lab
AppPublisherURL=https://github.com/kitlame7-lab/jarvis67
AppSupportURL=https://github.com/kitlame7-lab/jarvis67/issues
DefaultDirName={localappdata}\Programs\Jarvis
DefaultGroupName=Джарвис
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist-installer
OutputBaseFilename=Jarvis-Setup
SetupIconFile=jarvis.ico
UninstallDisplayIcon={app}\Jarvis.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "autostart"; Description: "Запускать Джарвиса вместе с Windows"; GroupDescription: "Дополнительно:"
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Дополнительно:"; Flags: unchecked

[Files]
Source: "..\dist\Jarvis\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\dist\pc\*"; DestDir: "{app}\pc"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; Старая версия запускалась ярлыком из папки «Автозагрузка»
Type: files; Name: "{userstartup}\Jarvis.lnk"

[Icons]
Name: "{group}\Джарвис"; Filename: "{app}\Jarvis.exe"
Name: "{group}\Удалить Джарвиса"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Джарвис"; Filename: "{app}\Jarvis.exe"; Tasks: desktopicon

[Registry]
; Автозапуск ставится только при обычной установке: тихое обновление не должно менять выбор пользователя
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Jarvis"; ValueData: """{app}\Jarvis.exe"" --minimized"; Flags: uninsdeletevalue; Tasks: autostart; Check: not WizardSilent
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "Jarvis"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\Jarvis.exe"; Description: "Запустить Джарвиса"; Flags: nowait postinstall skipifsilent
; После тихого обновления из самого приложения запускаем заново, чтобы окно вернулось
Filename: "{app}\Jarvis.exe"; Flags: nowait; Check: WizardSilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
procedure StopOldJarvis();
var
  ResultCode: Integer;
begin
  { Версия 2 жила как pythonw.exe jarvis.py и держала микрофон; закрываем её, чтобы не было двух Джарвисов }
  Exec('powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like ''*jarvis.py*'' -or $_.CommandLine -like ''*ui_server.py*'' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    StopOldJarvis();
end;
