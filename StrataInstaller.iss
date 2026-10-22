; Strata 控制台 安装包脚本（Inno Setup 6）
; 产出：installer/StrataConsole-Setup-<ver>.exe
; 特性：全中文向导、免管理员权限安装到用户目录、桌面快捷方式自动创建、
;       无需任何手动勾选——用户点「安装」即可、支持卸载

[Setup]
AppId={{7C1E2A94-6B5D-4A08-9F3E-2D51C0A7B661}}
AppName=Strata 控制台
AppVersion=0.2.19
AppVerName=Strata 控制台 0.2.19
DefaultDirName={autopf}\StrataControl
DefaultGroupName=Strata 控制台
UninstallDisplayName=Strata 控制台
UninstallDisplayIcon={app}\StrataControl.exe
OutputDir=installer
OutputBaseFilename=StrataConsole-Setup-0.2.19
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
SetupIconFile=strata.ico
WizardStyle=modern
DisableProgramGroupPage=yes
DisableDirPage=no
DisableReadyPage=yes
DisableFinishedPage=no

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"

[Files]
Source: "dist\StrataControl.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; 桌面快捷方式：自动创建，不让用户打钩
Name: "{userdesktop}\Strata 控制台"; Filename: "{app}\StrataControl.exe"; \
    Comment: "启动 Strata 控制台"
Name: "{group}\Strata 控制台"; Filename: "{app}\StrataControl.exe"; \
    Comment: "启动 Strata 控制台"
Name: "{group}\卸载 Strata 控制台"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\StrataControl.exe"; \
    Description: "立即运行 Strata 控制台"; \
    Flags: nowait postinstall skipifsilent; WorkingDir: {app}

[UninstallDelete]
; 卸载时清掉日志等运行残留（不动 %APPDATA%\StrataControl 里的用户配置和 Key）
Type: files; Name: "{app}\*.log"
