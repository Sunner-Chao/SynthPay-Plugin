$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $env:LOCALAPPDATA "SynthPay\wechat-watcher"
$ConfigDir = Join-Path $env:APPDATA "SynthPay"
$ConfigPath = Join-Path $ConfigDir "wechat-watcher.ini"
$TaskName = "SynthPay WeChat Watcher"
$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
Copy-Item (Join-Path $SourceDir "synthpay_wechat_watcher.py") $InstallDir -Force
Copy-Item (Join-Path $SourceDir "requirements.txt") $InstallDir -Force
if (-not (Test-Path $ConfigPath)) {
    Copy-Item (Join-Path $SourceDir "wechat-watcher.example.ini") $ConfigPath
}

& icacls.exe $ConfigDir "/inheritance:r" "/grant:r" "${CurrentUser}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Failed to restrict watcher configuration permissions"
}

$VenvDir = Join-Path $InstallDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    py -3 -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create watcher Python environment"
    }
}
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $InstallDir "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install watcher Python dependencies"
}

$Python = Join-Path $VenvDir "Scripts\pythonw.exe"
$Script = Join-Path $InstallDir "synthpay_wechat_watcher.py"
$Action = New-ScheduledTaskAction -Execute $Python -Argument ('"{0}" "{1}"' -f $Script, $ConfigPath)
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Settings = New-ScheduledTaskSettingsSet -RestartCount 20 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null

Write-Host "Installed to $InstallDir"
Write-Host "Edit $ConfigPath, keep dry_run=1 for the first real receipt test, then run:"
Write-Host "Start-ScheduledTask -TaskName '$TaskName'"
