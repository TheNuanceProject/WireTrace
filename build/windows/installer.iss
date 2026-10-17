; WireTrace: the Windows installer (Inno Setup 6).
;
; Compiled by build/build.py, which passes every value that the checks
; below ask for. They come from version.py, app/constants.py and
; updater/install.py, so the installer cannot state a version, a name or
; a wording that differs from the application's. This file is not
; compiled on its own.
;
; Installs for all users (as administrator) or for the current user
; only; the person installing chooses.

#ifndef AppName
  #error Not given: AppName. Compile through build/build.py.
#endif
#ifndef AppVersion
  #error Not given: AppVersion. Compile through build/build.py.
#endif
#ifndef AppPublisher
  #error Not given: AppPublisher. Compile through build/build.py.
#endif
#ifndef AppUrl
  #error Not given: AppUrl. Compile through build/build.py.
#endif
#ifndef AppDescription
  #error Not given: AppDescription. Compile through build/build.py.
#endif
#ifndef AppCopyright
  #error Not given: AppCopyright. Compile through build/build.py.
#endif
#ifndef AppExeName
  #error Not given: AppExeName. Compile through build/build.py.
#endif
#ifndef AppMutexName
  #error Not given: AppMutexName. Compile through build/build.py.
#endif
#ifndef SourceDir
  #error Not given: SourceDir. Compile through build/build.py.
#endif
#ifndef OutputDir
  #error Not given: OutputDir. Compile through build/build.py.
#endif
#ifndef OutputName
  #error Not given: OutputName. Compile through build/build.py.
#endif
#ifndef IconFile
  #error Not given: IconFile. Compile through build/build.py.
#endif
#ifndef PrivacyFile
  #error Not given: PrivacyFile. Compile through build/build.py.
#endif
#ifndef UpdateOption
  #error Not given: UpdateOption. Compile through build/build.py.
#endif
#ifndef ChoicesFile
  #error Not given: ChoicesFile. Compile through build/build.py.
#endif
#ifndef ChoicesSection
  #error Not given: ChoicesSection. Compile through build/build.py.
#endif
#ifndef ChoicesKey
  #error Not given: ChoicesKey. Compile through build/build.py.
#endif

[Setup]
; Identifies the application to Windows. Must stay the same in every version.
AppId={{COM.THENUANCEPROJECT.WIRETRACE}

AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} v{#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}
AppCopyright={#AppCopyright}
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppDescription}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}

; For the current user unless the person installing chooses all users.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; {autopf} is Program Files for all users, AppData\Local\Programs for one.
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes

OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}

Compression=lzma2/max
SolidCompression=yes

SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
UninstallFilesDir={app}\uninstall
WizardStyle=modern

; Shown before anything is installed: what WireTrace keeps on this
; computer and what its update check sends.
InfoBeforeFile={#PrivacyFile}

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; The application holds this mutex while it runs, so the installer can
; ask for it to be closed before replacing its files.
AppMutex={#AppMutexName}

; Windows 10 version 1809 or later
MinVersion=10.0.17763

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";   Description: "Create a desktop shortcut";    GroupDescription: "Additional shortcuts:"
Name: "startmenuicon"; Description: "Create a Start Menu shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce
; Ticked unless the person installing unticks it. Not offered again when
; an installation that already holds a choice is updated.
Name: "updatecheck";   Description: "{#UpdateOption}";          GroupDescription: "Updates:"; Check: not AlreadyChosen

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExeName}"; Comment: "{#AppDescription}"; Tasks: startmenuicon
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}";      Tasks: startmenuicon
Name: "{autodesktop}\{#AppName}";     Filename: "{app}\{#AppExeName}"; Comment: "{#AppDescription}"; Tasks: desktopicon

[INI]
; What was chosen about update checks. The application takes it over on
; its first start.
Filename: "{app}\{#ChoicesFile}"; Section: "{#ChoicesSection}"; Key: "{#ChoicesKey}"; String: "true";  Tasks: updatecheck;     Check: ShouldRecordChoice
Filename: "{app}\{#ChoicesFile}"; Section: "{#ChoicesSection}"; Key: "{#ChoicesKey}"; String: "false"; Tasks: not updatecheck; Check: ShouldRecordChoice

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent unchecked

[UninstallDelete]
Type: files; Name: "{app}\{#ChoicesFile}"
; Working files that can be made again: downloaded updates, the filled
; user guide. Settings and logs are the user's own and are left alone.
Type: filesandordirs; Name: "{localappdata}\{#AppName}\Cache"

[Code]
function ChoicesPath: String;
begin
  Result := AddBackslash(WizardDirValue) + '{#ChoicesFile}';
end;

// An installation that already holds a choice is being updated: the
// option is not offered and the choice is left as it is. A silent
// installation records nothing, so the setting keeps its default.
function AlreadyChosen: Boolean;
begin
  Result := FileExists(ChoicesPath);
end;

function ShouldRecordChoice: Boolean;
begin
  Result := (not WizardSilent) and (not AlreadyChosen);
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  if IsAdminInstallMode then
    Result := 'Installation mode:' + NewLine + Space + 'All users (administrator)' + NewLine + NewLine
  else
    Result := 'Installation mode:' + NewLine + Space + 'Current user only' + NewLine + NewLine;
  Result := Result + MemoDirInfo + NewLine;
  if MemoGroupInfo <> '' then
    Result := Result + NewLine + MemoGroupInfo + NewLine;
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + MemoTasksInfo + NewLine;
end;
