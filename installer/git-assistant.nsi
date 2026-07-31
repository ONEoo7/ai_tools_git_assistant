; NSIS installer for Git Assistant.
;
; Build (VERSION is injected by tools/build.py so it cannot drift from
; src/git_assistant/__init__.py):
;     makensis /DVERSION=0.2.0 installer\git-assistant.nsi
;
; Deliberately a PER-USER install:
;   * the app self-updates by replacing the files it runs from, so the install
;     directory must be writable without elevation - Program Files would mean a
;     UAC prompt for every update;
;   * no admin rights are needed to install at all.

Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "WordFunc.nsh"
; FileFunc/WordFunc macros must be inserted before ${GetSize}/${VersionCompare}.
!insertmacro GetSize
!insertmacro VersionCompare

!ifndef VERSION
  !define VERSION "0.0.0"
!endif

; Invoke with an ABSOLUTE path to this script. Given a relative one, NSIS
; resolves ${__FILEDIR__} to <cwd>\installer\installer -- this directory
; appended twice -- and every path below breaks with an unhelpful macro error.
!if ! /FileExists "${__FILEDIR__}\..\LICENSE"
  !error "Run makensis with an absolute path to this script \
(${__FILEDIR__} did not resolve). See tools/build.py."
!endif

; Overridable so one script serves both build paths: the local spec produces
; dist\GitAssistant\GitAssistant.exe, while CI names it git-assistant. Paths are
; anchored to this script's directory (__FILEDIR__) so the working directory of
; makensis does not change what gets packaged.
!ifndef SRC_DIR
  !define SRC_DIR "${__FILEDIR__}\..\dist\GitAssistant"
!endif
!ifndef EXE_NAME
  !define EXE_NAME "GitAssistant.exe"
!endif
!ifndef OUT_FILE
  !define OUT_FILE "${__FILEDIR__}\..\dist\GitAssistant-${VERSION}-setup.exe"
!endif

!define APP_NAME     "Git Assistant"
!define PUBLISHER    "Stefan Ghitescu"
!define UNINST_KEY   "Software\Microsoft\Windows\CurrentVersion\Uninstall\GitAssistant"
; Only ever deleted now - see the "Start with Windows" section for why.
!define RUN_KEY      "Software\Microsoft\Windows\CurrentVersion\Run"
!define STARTUP_LNK  "$SMSTARTUP\${APP_NAME}.lnk"

; PERMACHINE switches this from a per-user install under %LOCALAPPDATA% to a
; per-machine one under Program Files.
;
; The per-user location exists so the self-updater can replace the files it
; runs from without a UAC prompt. A build without the updater has no such need,
; and a user-writable install directory is one of the ingredients Defender's
; heuristics score: an unsigned executable somewhere any process can rewrite,
; registered to appear at sign-in, is the shape of malware persistence. Program
; Files is not writable without elevation, which removes that ingredient.
;
; Whether it is enough is unverified -- five earlier attempts at reducing the
; behavioural profile changed nothing. Code signing remains the real answer;
; this is the last ingredient that can be removed without one. See the README.
!ifdef PERMACHINE
  !define REG_ROOT     HKLM
  !define SHELL_CTX    all
  !define DEFAULT_DIR  "$PROGRAMFILES64\GitAssistant"
  !define EXEC_LEVEL   admin
!else
  !define REG_ROOT     HKCU
  !define SHELL_CTX    current
  !define DEFAULT_DIR  "$LOCALAPPDATA\Programs\GitAssistant"
  !define EXEC_LEVEL   user
!endif

; Settings belong to the person, not the machine, and the application resolves
; them with platformdirs regardless of how it was installed. Always read under
; the *current user's* context, even in a per-machine install -- see the
; uninstaller, which switches context before touching this.
!define CONFIG_DIR   "$LOCALAPPDATA\git-assistant"

Name "${APP_NAME}"
OutFile "${OUT_FILE}"
InstallDir "${DEFAULT_DIR}"
InstallDirRegKey ${REG_ROOT} "Software\GitAssistant" "InstallDir"
RequestExecutionLevel ${EXEC_LEVEL}
SetCompressor /SOLID lzma

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName"     "${APP_NAME}"
VIAddVersionKey "FileDescription" "${APP_NAME} installer"
VIAddVersionKey "FileVersion"     "${VERSION}"
VIAddVersionKey "ProductVersion"  "${VERSION}"
VIAddVersionKey "CompanyName"     "${PUBLISHER}"
VIAddVersionKey "LegalCopyright"  "${PUBLISHER}"

!define MUI_ICON   "${__FILEDIR__}\..\src\git_assistant\resources\icon.ico"
!define MUI_UNICON "${__FILEDIR__}\..\src\git_assistant\resources\icon.ico"
!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_LICENSE "${__FILEDIR__}\..\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipDirIfInstalled
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

; No MUI_FINISHPAGE_RUN in any variant -- see the note above .onInstSuccess for
; why nothing here launches the application. Since the checkbox is gone, the
; page has to say how to start it, or "installed" is the last thing a first-time
; user is told about a program that puts no window on screen.
!define MUI_FINISHPAGE_TEXT "${APP_NAME} has been installed.$\r$\n$\r$\n\
Start it from the Start menu. It runs in the system tray -- look for its icon \
by the clock, and click it to open the window."
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Var PrevVersion   ; version already installed ("" when this is a fresh install)
Var PrevDir       ; where it is installed, so an upgrade lands in the same place
Var ModeVerb      ; "Installing" / "Repairing" / "Upgrading" / "Downgrading"

; Decide up front what this run is: a first install, a repair of the same
; version, an upgrade, or a downgrade. Asking here (rather than silently
; overwriting) means running the installer twice cannot surprise anyone.
Function .onInit
  ; Everything below -- $SMPROGRAMS, $DESKTOP, $SMSTARTUP, $LOCALAPPDATA --
  ; resolves differently per context, so it is set once, up front.
  SetShellVarContext ${SHELL_CTX}
  StrCpy $ModeVerb "Installing"
  ReadRegStr $PrevVersion ${REG_ROOT} "${UNINST_KEY}" "DisplayVersion"
  ReadRegStr $PrevDir     ${REG_ROOT} "${UNINST_KEY}" "InstallLocation"

  ${If} $PrevVersion == ""
    Return  ; nothing installed - ordinary first install
  ${EndIf}

  ; Stay where the existing copy lives; the directory page is skipped below.
  ${If} $PrevDir != ""
    StrCpy $INSTDIR $PrevDir
  ${EndIf}

  ; $0: 0 = same, 1 = this installer is newer, 2 = installed copy is newer
  ${VersionCompare} "${VERSION}" "$PrevVersion" $0

  ${If} $0 == 0
    StrCpy $ModeVerb "Repairing"
    MessageBox MB_YESNO|MB_ICONQUESTION \
      "${APP_NAME} ${VERSION} is already installed in:$\n$INSTDIR$\n$\n\
Reinstall it? Program files are replaced with a clean copy; your settings \
are kept." \
      /SD IDYES IDYES continue
    Abort
  ${ElseIf} $0 == 1
    StrCpy $ModeVerb "Upgrading"
    MessageBox MB_YESNO|MB_ICONQUESTION \
      "${APP_NAME} $PrevVersion is installed in:$\n$INSTDIR$\n$\n\
Upgrade to ${VERSION}? Your settings are kept." \
      /SD IDYES IDYES continue
    Abort
  ${Else}
    StrCpy $ModeVerb "Downgrading"
    MessageBox MB_YESNO|MB_ICONEXCLAMATION \
      "A NEWER version of ${APP_NAME} ($PrevVersion) is already installed in:$\n\
$INSTDIR$\n$\nDowngrade to ${VERSION}?" \
      /SD IDNO IDYES continue
    Abort
  ${EndIf}

  continue:
FunctionEnd

; There is deliberately no .onInstSuccess, and no finish-page "run now" option.
; No installer variant starts the application.
;
; It used to relaunch after a silent (self-update) run, so the tray icon did not
; simply vanish mid-update. That is friendlier, but an installer that executes a
; program when it finishes is a defining behaviour of a dropper, and this one is
; already being quarantined -- see the README for what has and has not been
; ruled out. Not launching is the one thing an installer can do about that which
; costs nothing but a click.
;
; It also removes a real hazard in the per-machine variants: the installer runs
; elevated, so anything it started would inherit that token. A tray app running
; as administrator writes its settings into the wrong profile and hands every
; git subprocess it spawns rights it should not have.
;
; The user starts it from the Start menu. The finish page says so.

; Nothing to choose when a copy already exists - it is replaced in place, and
; offering a different directory would strand the old install.
Function SkipDirIfInstalled
  ${If} $PrevVersion != ""
    Abort
  ${EndIf}
FunctionEnd

; File replacement fails while the app holds its own exe open, so stop it first.
;
; By name, and both names: the two builds disagree about it -- a local build
; produces GitAssistant.exe and CI produces git-assistant.exe -- and killing
; only the name this installer was built with misses precisely the case an
; upgrade runs into, a CI installer replacing a copy a local build installed.
; The old process would keep _internal open and the upgrade would fail
; part-way through.
;
; This was a single PowerShell call that matched on the install *path*, which
; was more precise: it stopped what was running from $INSTDIR and nothing else.
; It was replaced because an installer that launches PowerShell to enumerate
; and terminate processes is a large part of what a malicious installer looks
; like, and this one was detected as Trojan:Script/Wacatac.F!ml -- a script
; detection, on an installer whose only script-like act was that call.
;
; The cost is precision. Killing by name will also stop a *portable* build the
; user happens to be running from somewhere else, which the path match left
; alone. That is a rare situation and its worst outcome is an application
; closing during an install it was about to be replaced by; the alternative was
; an installer that antivirus removes, which has no working outcome at all.
;
; Whether this actually changes the detection is unverified -- see the README.
!macro StopRunningApp
  DetailPrint "Closing ${APP_NAME} if it is running..."
  nsExec::Exec 'taskkill /IM "GitAssistant.exe" /F'
  Pop $0  ; ignored: not running is the ordinary case
  nsExec::Exec 'taskkill /IM "git-assistant.exe" /F'
  Pop $0
  Sleep 500
!macroend

Section "${APP_NAME} (required)" SEC_APP
  SectionIn RO
  DetailPrint "$ModeVerb ${APP_NAME} ${VERSION}..."
  !insertmacro StopRunningApp

  ; Clear the previous payload first. Overwriting in place would leave files
  ; that a newer build no longer ships, so "repair" would not restore a clean
  ; copy and an upgrade could keep loading a stale module. Only _internal is
  ; removed - never the whole $INSTDIR, which the user may have pointed
  ; somewhere shared.
  RMDir /r "$INSTDIR\_internal"

  SetOutPath "$INSTDIR"
  ; The onedir PyInstaller build, including its _internal support directory.
  ; "\*" rather than "\*.*": the latter can skip extensionless files.
  File /r "${SRC_DIR}\*"

  ; Drop the other build's executable if a previous install left one. Without
  ; this an upgrade across the two naming conventions leaves two executables
  ; in the directory -- one of them stale -- and a Start menu shortcut that may
  ; point at either.
  !if "${EXE_NAME}" != "GitAssistant.exe"
    Delete "$INSTDIR\GitAssistant.exe"
  !endif
  !if "${EXE_NAME}" != "git-assistant.exe"
    Delete "$INSTDIR\git-assistant.exe"
  !endif

  WriteRegStr ${REG_ROOT} "Software\GitAssistant" "InstallDir" "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; Add/Remove Programs entry, in the hive that matches the install.
  WriteRegStr   ${REG_ROOT} "${UNINST_KEY}" "DisplayName"     "${APP_NAME}"
  WriteRegStr   ${REG_ROOT} "${UNINST_KEY}" "DisplayVersion"  "${VERSION}"
  WriteRegStr   ${REG_ROOT} "${UNINST_KEY}" "Publisher"       "${PUBLISHER}"
  WriteRegStr   ${REG_ROOT} "${UNINST_KEY}" "DisplayIcon"     "$INSTDIR\${EXE_NAME}"
  WriteRegStr   ${REG_ROOT} "${UNINST_KEY}" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
  WriteRegStr   ${REG_ROOT} "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD ${REG_ROOT} "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD ${REG_ROOT} "${UNINST_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD ${REG_ROOT} "${UNINST_KEY}" "EstimatedSize" "$0"

  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${EXE_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" "$INSTDIR\Uninstall.exe"

  ; Remove autostart left by an earlier release, in whichever form it took:
  ; the Run key value up to 0.3.7, the Startup shortcut in 0.3.8. Nothing
  ; recreates either -- see the note further down for why autostart is no
  ; longer offered. Unconditional and in the required section, so upgrading is
  ; what clears an install that Defender is already unhappy about.
  ;
  ; Always the *per-user* copies, whatever context this install runs in: every
  ; release that wrote them was a per-user install, so HKLM and the all-users
  ; Startup folder never held them and looking there would clean nothing.
  SetShellVarContext current
  DeleteRegValue HKCU "${RUN_KEY}" "GitAssistant"
  Delete "${STARTUP_LNK}"
  SetShellVarContext ${SHELL_CTX}
SectionEnd

Section "Desktop shortcut" SEC_DESKTOP
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${EXE_NAME}"
SectionEnd

; There is deliberately NO "start with Windows" option.
;
; Two mechanisms were tried and both were flagged by Defender's cloud
; heuristics, which quarantined the executable and deleted the install:
;
;   HKCU ...\CurrentVersion\Run   -> Behavior:Win32/SuspiciousFileInRunKey.A!cl
;   Startup folder shortcut       -> Trojan:Win32/SuspStartupFolderFileTarget.A!cl
;
; They are two names for one judgement. What is being scored is not the
; mechanism but the target: an unsigned, low-prevalence executable in a
; user-writable directory (%LOCALAPPDATA%) that registers itself to run at
; sign-in. That is the shape of malware persistence, and this application
; matches every part of it except the intent.
;
; Worse, the verdict is not confined to the installed copy. Once the behaviour
; was observed, the same binary was blocked in the build tree as well, so the
; project would not even compile without an antivirus exclusion.
;
; Autostart is therefore not shipped at all. A user who wants it can create the
; shortcut themselves -- the --startup flag still works, and is documented in
; the README -- but the installer will not do it on their behalf, because the
; cost of being wrong is a quarantined install rather than a warning.
;
; Revisit once the executable is code-signed: a trusted publisher is the signal
; whose absence all of this is really about.

LangString DESC_APP     ${LANG_ENGLISH} "The ${APP_NAME} application (required)."
LangString DESC_DESKTOP ${LANG_ENGLISH} "Create a shortcut on the desktop."

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_APP}     $(DESC_APP)
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_DESKTOP} $(DESC_DESKTOP)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  SetShellVarContext ${SHELL_CTX}
  !insertmacro StopRunningApp

  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk"
  RMDir  "$SMPROGRAMS\${APP_NAME}"
  Delete "$DESKTOP\${APP_NAME}.lnk"

  DeleteRegKey ${REG_ROOT} "${UNINST_KEY}"
  DeleteRegKey ${REG_ROOT} "Software\GitAssistant"

  ; Per-user leftovers from 0.3.8 or earlier, which were always per-user
  ; installs. Same reasoning as the installer's cleanup.
  SetShellVarContext current
  Delete "${STARTUP_LNK}"
  DeleteRegValue HKCU "${RUN_KEY}" "GitAssistant"

  ; Only the install directory - never a blind RMDir /r of a user-chosen path.
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\${EXE_NAME}"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  ; Settings are the user's data: ask rather than delete or orphan silently.
  IfFileExists "${CONFIG_DIR}\*.*" 0 done
    MessageBox MB_YESNO|MB_ICONQUESTION \
      "Also remove your ${APP_NAME} settings (repository list, model and \
connection settings)?$\n$\n${CONFIG_DIR}" \
      /SD IDNO IDNO done
    RMDir /r "${CONFIG_DIR}"
  done:
SectionEnd
