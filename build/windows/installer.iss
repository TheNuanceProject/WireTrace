; ══════════════════════════════════════════════════════════════════════════════
; WireTrace — Windows Installer (Inno Setup 6)
; ══════════════════════════════════════════════════════════════════════════════
;
; Supports BOTH admin and non-admin installation:
;   Admin:     C:\Program Files\WireTrace          (all users)
;   Non-Admin: C:\Users\{user}\AppData\Local\WireTrace (current user)
;
; The user is prompted to choose at install time via the privilege dialog.
;
; Build prerequisites:
;   1. Nuitka standalone output in build\dist\WireTrace\
;   2. Inno Setup 6.x (iscc.exe on PATH)
;
; Usage:
;   iscc build\windows\installer.iss
; ══════════════════════════════════════════════════════════════════════════════

#define MyAppName        "WireTrace"
#define MyAppVersion     "1.0.0"
#define MyAppPublisher   "The Nuance Project"
#define MyAppURL         "https://thenuanceproject.com"
#define MyAppExeName     "WireTrace.exe"
#define MyAppDescription "Professional Serial Data Monitor"
#define MyAppCopyright   "© 2026 The Nuance Project"

; ── Source paths (relative to this .iss file) ─────────────────────────────────
; Nuitka standalone output directory
#define NuitkaDistDir    "..\..\build\dist\WireTrace"

[Setup]
; Unique application identifier (do NOT change between versions)
AppId={{COM.THENUANCEPROJECT.WIRETRACE}

; Application metadata
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppCopyright={#MyAppCopyright}
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppDescription}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

; ── Installation mode: Admin OR Non-Admin ─────────────────────────────────────
; PrivilegesRequired=lowest makes non-admin the default.
; PrivilegesRequiredOverridesAllowed=dialog shows a choice on launch:
;   "Install for all users" (admin) vs "Install for me only" (non-admin)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; ── Install directories ───────────────────────────────────────────────────────
; {autopf} = Program Files (admin) or AppData\Local\Programs (non-admin)
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}

; ── Output ────────────────────────────────────────────────────────────────────
OutputDir=..\..\deployment\windows
OutputBaseFilename=WireTrace-Setup-v{#MyAppVersion}

; ── Compression ───────────────────────────────────────────────────────────────
Compression=lzma2/max
SolidCompression=yes

; ── Appearance ────────────────────────────────────────────────────────────────
SetupIconFile=..\..\resources\app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
WizardStyle=modern
WizardSizePercent=100

; ── Architecture ──────────────────────────────────────────────────────────────
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

; ── Uninstaller ───────────────────────────────────────────────────────────────
CreateUninstallRegKey=yes
UninstallFilesDir={app}\uninstall

; ── Misc ──────────────────────────────────────────────────────────────────────
; Prevent installing over a running instance
AppMutex=WireTrace_SingleInstance_Mutex
; Show setup size on disk
DiskSpanning=no
; Allow user to select directory
AllowNoIcons=yes
; Minimum Windows version (Windows 10 1809+)
MinVersion=10.0.17763

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

; ── Installation Tasks ────────────────────────────────────────────────────────
[Tasks]
Name: "desktopicon";   Description: "Create a desktop shortcut";       GroupDescription: "Additional shortcuts:"
Name: "startmenuicon"; Description: "Create a Start Menu shortcut";    GroupDescription: "Additional shortcuts:"; Flags: checkedonce

; ── Files to Install ──────────────────────────────────────────────────────────
; Bundle the entire Nuitka standalone directory
[Files]
; Main executable (renamed from main.exe)
Source: "{#NuitkaDistDir}\WireTrace.exe"; DestDir: "{app}"; Flags: ignoreversion

; All Nuitka runtime dependencies (DLLs, Python stdlib, PySide6, etc.)
Source: "{#NuitkaDistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "WireTrace.exe"

; ── Shortcuts ─────────────────────────────────────────────────────────────────
[Icons]
; Start Menu (program group)
Name: "{group}\{#MyAppName}";           Filename: "{app}\{#MyAppExeName}"; Comment: "{#MyAppDescription}"; Tasks: startmenuicon
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}";        Tasks: startmenuicon

; Desktop shortcut
Name: "{autodesktop}\{#MyAppName}";     Filename: "{app}\{#MyAppExeName}"; Comment: "{#MyAppDescription}"; Tasks: desktopicon

; ── Registry ──────────────────────────────────────────────────────────────────
[Registry]
; Store install path for the auto-updater to find
Root: HKCU; Subkey: "Software\{#MyAppPublisher}\{#MyAppName}"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\{#MyAppPublisher}\{#MyAppName}"; ValueType: string; ValueName: "Version";     ValueData: "{#MyAppVersion}"; Flags: uninsdeletekey

; ── Post-Install ──────────────────────────────────────────────────────────────
[Run]
; Offer to launch after install
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent unchecked

; ── Uninstall Cleanup ─────────────────────────────────────────────────────────
[UninstallDelete]
; Clean up config directory (only if user confirms uninstall)
Type: filesandordirs; Name: "{localappdata}\{#MyAppName}"

[Code]
// ── Custom Code: Display install mode info on the Ready page ─────────────────
function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := '';
  if IsAdminInstallMode then
    Result := Result + 'Installation mode:' + NewLine + Space + 'All users (administrator)' + NewLine + NewLine
  else
    Result := Result + 'Installation mode:' + NewLine + Space + 'Current user only' + NewLine + NewLine;
  Result := Result + MemoDirInfo + NewLine;
  if MemoGroupInfo <> '' then
    Result := Result + NewLine + MemoGroupInfo + NewLine;
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + MemoTasksInfo + NewLine;
end;
