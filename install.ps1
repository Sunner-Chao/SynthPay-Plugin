param(
    [string]$PythonExecutable,
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $env:LOCALAPPDATA "SynthPay\wechat-watcher"
$ConfigDir = Join-Path $env:APPDATA "SynthPay"
$ConfigPath = Join-Path $ConfigDir "wechat-watcher.ini"
$TaskName = "SynthPay WeChat Watcher"
$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $PythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($PythonLauncher) {
        $PythonExecutable = & $PythonLauncher.Source -3.10 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1
        if ($PythonExecutable) {
            $PythonExecutable = $PythonExecutable.Trim()
        }
    }
}
if ([string]::IsNullOrWhiteSpace($PythonExecutable) -or -not (Test-Path -LiteralPath $PythonExecutable)) {
    throw "Python 3.10 was not found. Run setup-and-start.cmd, or pass -PythonExecutable with a valid Python 3.10 path."
}
$PythonVersion = (& $PythonExecutable -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null | Select-Object -First 1).Trim()
if ($PythonVersion -ne "3.10") {
    throw "SynthPay watcher requires Python 3.10; found Python $PythonVersion at $PythonExecutable."
}

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
    & $PythonExecutable -m venv $VenvDir
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
Write-Host "Configuration: $ConfigPath"
if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Watcher task started."
} else {
    Write-Host "Run setup-and-start.cmd for guided configuration and immediate startup."
}
