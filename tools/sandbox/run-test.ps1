# Install the per-user build on a clean Windows, start it, and bring back
# everything that would explain a failure to start.
#
# PowerShell, not batch. The first version of this was a .cmd and its own
# report came back with the lines out of order, three copies of the closing
# banner, and "ECHO is off." where the blank lines should have been -- `echo`
# with an empty argument prints the echo state, and `echo text>>file` parses as
# a file-handle redirect when the text ends in a digit. A diagnostic that has to
# be diagnosed is worse than none, and this project already speaks pwsh
# everywhere else.
#
# Run automatically by git-assistant.wsb. The sandbox is destroyed when it
# closes, so anything not written into results\ is gone.

$ErrorActionPreference = 'Continue'

$share  = 'C:\Users\WDAGUtilityAccount\Desktop\share'
$out    = Join-Path $share 'results'
$appDir = Join-Path $env:LOCALAPPDATA 'Programs\GitAssistant'
$cfgDir = Join-Path $env:LOCALAPPDATA 'git-assistant'
$log    = Join-Path $cfgDir 'startup.log'

# Fresh every run. Appending to the last one is how three runs became one
# unreadable file that looked like a single confusing result.
if (Test-Path $out) { Remove-Item $out -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Path $out -Force | Out-Null
$report = Join-Path $out 'report.txt'

function Say([string]$text = '') {
    Write-Host $text
    Add-Content -LiteralPath $report -Value $text -Encoding utf8
}

# No Windows Error Reporting dialog: a modal box waiting for a click nobody
# will give it turns an unattended run into a hang instead of a result.
New-Item -Path 'HKCU:\Software\Microsoft\Windows\Windows Error Reporting' -Force | Out-Null
Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\Windows Error Reporting' `
    -Name DontShowUI -Value 1 -Type DWord

Say "=== Windows Sandbox run, $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
Say "Windows: $((Get-CimInstance Win32_OperatingSystem).Version)"
Say ''

$setup = Get-ChildItem -LiteralPath $share -Filter '*noupdate-user-windows-x64-setup.exe' |
    Select-Object -First 1
if (-not $setup) {
    Say 'NO INSTALLER FOUND. Put the -noupdate-user-windows-x64-setup.exe asset'
    Say 'beside git-assistant.wsb and run it again.'
    Read-Host 'Press Enter to close'
    exit 1
}
Say "Installer: $($setup.Name) ($([int]($setup.Length / 1MB)) MB)"

# An installer downloaded from GitHub carries a Mark of the Web, and
# `Start-Process` goes through ShellExecute, which honours it -- SmartScreen
# then blocks the launch and the whole run reports "not installed" as though
# the application were at fault. Stripping it is what makes this a test of the
# application rather than a test of SmartScreen.
$zone = Get-Item -LiteralPath $setup.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue
Say "Mark of the Web: $(if ($zone) { 'present - removing it' } else { 'none' })"
Unblock-File -LiteralPath $setup.FullName -ErrorAction SilentlyContinue

Say 'Installing silently...'
# NSIS silent mode. The per-user variant asks for no elevation.
#
# Both ways, deliberately. `Start-Process -Wait` waits for a GUI-subsystem
# process, which the call operator does not; but it goes through ShellExecute
# and can be refused outright, which is what happened here -- and the failure
# was invisible because a refused `Start-Process` leaves $proc null and
# "$($proc.ExitCode)" renders as an empty string rather than as an error.
$proc = $null
try {
    $proc = Start-Process -FilePath $setup.FullName -ArgumentList '/S' -PassThru -Wait -ErrorAction Stop
    Say "Installer exit code: $($proc.ExitCode)"
} catch {
    Say "Start-Process refused it: $($_.Exception.Message)"
    Say 'Falling back to direct invocation (CreateProcess, no shell)...'
    & $setup.FullName /S | Out-Null
    Say "Direct invocation returned: $LASTEXITCODE"
}

$exe = Join-Path $appDir 'GitAssistant.exe'
$waited = 0
while (-not (Test-Path $exe) -and $waited -lt 120) {
    Start-Sleep -Seconds 2; $waited += 2
}
if (-not (Test-Path $exe)) {
    # Everything below this point would then be reporting on an empty disk.
    # Said here so a reader does not take "MISSING: ...plugins\platforms" and
    # "IT IS NOT RUNNING" for findings about the application.
    Say "TIMED OUT: $exe never appeared."
    Say 'NOTHING WAS INSTALLED. Every line below is about an empty directory,'
    Say 'not about the application - read no further than this.'
} else {
    Say "Installed after ${waited}s."
}
Say ''

# What Qt loads first. Missing, QApplication aborts, and that is the whole
# answer without reading a dump.
Say '--- Qt platform plugins ---'
$plugins = Join-Path $appDir '_internal\PyQt6\Qt6\plugins\platforms'
if (Test-Path $plugins) {
    Get-ChildItem $plugins -Name | ForEach-Object { Say "  $_" }
} else {
    Say "  MISSING: $plugins"
}
Say ''

Say '--- starting the application ---'
Start-Process -FilePath $exe
Start-Sleep -Seconds 30

$running = Get-Process -Name 'GitAssistant' -ErrorAction SilentlyContinue
if ($running) {
    Say "Still running after 30s (pid $($running.Id -join ', '))."
    foreach ($p in $running) {
        Say "  responding: $($p.Responding)   main window: '$($p.MainWindowTitle)'"
    }
} else {
    Say 'IT IS NOT RUNNING after 30s - it exited or crashed.'
}
Say ''

# The whole directory, not just the one file. "No startup.log" and "the
# application never wrote anything at all" are different findings, and the
# first version of this could not tell them apart.
Say "--- $cfgDir ---"
if (Test-Path $cfgDir) {
    Get-ChildItem $cfgDir -Recurse -File |
        ForEach-Object { Say ("  {0,10}  {1}" -f $_.Length, $_.FullName.Replace($cfgDir, '')) }
} else {
    Say '  the application has written nothing here at all'
}
Say ''

Say '--- startup.log ---'
if (Test-Path $log) {
    Copy-Item $log (Join-Path $out 'startup.log') -Force
    Get-Content $log | ForEach-Object { Say "  $_" }
} else {
    Say '  NOT WRITTEN.'
    Say '  This build has faults.install() as its first statement, so if the'
    Say '  application is running and this is absent, the log path is wrong or'
    Say '  the write failed - not that start-up got no further.'
}

# Whatever Windows itself recorded, in case the process was killed outright.
foreach ($source in @("$env:LOCALAPPDATA\CrashDumps",
                      "$env:ProgramData\Microsoft\Windows\WER\ReportArchive")) {
    if (Test-Path $source) {
        $found = Get-ChildItem $source -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like '*GitAssistant*' }
        foreach ($item in $found) {
            Copy-Item $item.FullName (Join-Path $out $item.Name) -Force -ErrorAction SilentlyContinue
            Say "Collected: $($item.Name)"
        }
    }
}

Say ''
Say '=== done - results are in the results folder on the host ==='
Write-Host ''
Write-Host "Read $report, then close the sandbox window."
# Kept open on purpose: if a dialog is up, that screen is the finding.
Read-Host 'Press Enter to close'
