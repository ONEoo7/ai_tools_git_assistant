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
!define RUN_KEY      "Software\Microsoft\Windows\CurrentVersion\Run"
!define CONFIG_DIR   "$LOCALAPPDATA\git-assistant"

Name "${APP_NAME}"
OutFile "${OUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\GitAssistant"
InstallDirRegKey HKCU "Software\GitAssistant" "InstallDir"
RequestExecutionLevel user
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

; Offer to launch straight from the finish page.
!define MUI_FINISHPAGE_RUN "$INSTDIR\${EXE_NAME}"
!define MUI_FINISHPAGE_RUN_TEXT "Start ${APP_NAME} (runs in the system tray)"
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
  StrCpy $ModeVerb "Installing"
  ReadRegStr $PrevVersion HKCU "${UNINST_KEY}" "DisplayVersion"
  ReadRegStr $PrevDir     HKCU "${UNINST_KEY}" "InstallLocation"

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

; A silent run is a self-update: the application downloaded this installer,
; verified it against signed metadata, and launched it. Restart it afterwards.
;
; The finish page is where an interactive install offers that, and silent mode
; has no pages - so without this the app would vanish mid-update and the user
; would have to start it again from the Start menu, which reads as a crash.
;
; .onInstSuccess rather than the end of the section: it runs once, after every
; section has succeeded, so a failed install never relaunches into a
; half-replaced directory.
Function .onInstSuccess
  ${If} ${Silent}
    Exec '"$INSTDIR\${EXE_NAME}"'
  ${EndIf}
FunctionEnd

; Nothing to choose when a copy already exists - it is replaced in place, and
; offering a different directory would strand the old install.
Function SkipDirIfInstalled
  ${If} $PrevVersion != ""
    Abort
  ${EndIf}
FunctionEnd

; File replacement fails while the app holds its own exe open, so stop it first.
;
; By install PATH, not by executable name. The two builds disagree about the
; name -- a local build produces GitAssistant.exe and CI produces
; git-assistant.exe -- so killing by name misses precisely the case self-update
; runs into: a CI installer replacing a copy that a local build installed. The
; taskkill would match nothing, the old process would keep _internal open, and
; the upgrade would fail part-way through.
;
; Anything running out of $INSTDIR is ours, whatever it is called.
!macro StopRunningApp
  DetailPrint "Closing ${APP_NAME} if it is running..."
  nsExec::Exec `powershell -NoProfile -NonInteractive -Command "Get-Process | Where-Object { $$_.Path -like '$INSTDIR\*' } | Stop-Process -Force"`
  Pop $0  ; ignored: nothing running from there is the ordinary case
  ; Fallback for a machine where PowerShell will not run, and belt-and-braces
  ; for the name this particular installer was built with.
  nsExec::Exec 'taskkill /IM "${EXE_NAME}" /F'
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

  WriteRegStr HKCU "Software\GitAssistant" "InstallDir" "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; Add/Remove Programs entry (per-user hive, matching the install).
  WriteRegStr   HKCU "${UNINST_KEY}" "DisplayName"     "${APP_NAME}"
  WriteRegStr   HKCU "${UNINST_KEY}" "DisplayVersion"  "${VERSION}"
  WriteRegStr   HKCU "${UNINST_KEY}" "Publisher"       "${PUBLISHER}"
  WriteRegStr   HKCU "${UNINST_KEY}" "DisplayIcon"     "$INSTDIR\${EXE_NAME}"
  WriteRegStr   HKCU "${UNINST_KEY}" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
  WriteRegStr   HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"

  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${EXE_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Desktop shortcut" SEC_DESKTOP
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${EXE_NAME}"
SectionEnd

; On by default: this is a tray app, so starting with Windows is the normal way
; to use it. HKCU (not HKLM) to match the per-user install.
;
; --startup keeps sign-in quiet: it comes up in the tray only, whereas the
; shortcuts open the window (an icon that appears to do nothing reads as broken).
Section "Start with Windows" SEC_STARTUP
  WriteRegStr HKCU "${RUN_KEY}" "GitAssistant" "$\"$INSTDIR\${EXE_NAME}$\" --startup"
SectionEnd

LangString DESC_APP     ${LANG_ENGLISH} "The ${APP_NAME} application (required)."
LangString DESC_DESKTOP ${LANG_ENGLISH} "Create a shortcut on the desktop."
LangString DESC_STARTUP ${LANG_ENGLISH} "Launch ${APP_NAME} automatically when you sign in."

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_APP}     $(DESC_APP)
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_DESKTOP} $(DESC_DESKTOP)
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_STARTUP} $(DESC_STARTUP)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  !insertmacro StopRunningApp

  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk"
  RMDir  "$SMPROGRAMS\${APP_NAME}"
  Delete "$DESKTOP\${APP_NAME}.lnk"

  DeleteRegValue HKCU "${RUN_KEY}" "GitAssistant"
  DeleteRegKey   HKCU "${UNINST_KEY}"
  DeleteRegKey   HKCU "Software\GitAssistant"

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
