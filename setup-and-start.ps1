[CmdletBinding()]
param(
    [string]$CallbackUrl,
    [string]$CallbackSecret,
    [switch]$DryRun,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"

$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigDir = Join-Path $env:APPDATA "SynthPay"
$ConfigPath = Join-Path $ConfigDir "wechat-watcher.ini"
$ModelCacheDir = Join-Path $SourceDir "models"
$TessdataModelHashes = @{
    chi_sim = "A5FCB6F0DB1E1D6D8522F39DB4E848F05984669172E584E8D76B6B3141E1F730"
    eng = "7D4322BD2A7749724879683FC3912CB542F19906C83BCC1A52132556427170B2"
}
$ReceiptWindowTitle = [string]::Concat([char]0x5FAE, [char]0x4FE1, [char]0x6536, [char]0x6B3E, [char]0x52A9, [char]0x624B)

function Get-Python310 {
    $pythonPath = ""
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $pythonPath = & $launcher.Source -3.10 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1
            if ($pythonPath) {
                $pythonPath = $pythonPath.Trim()
            }
        } catch {
            $pythonPath = ""
        }
    }
    if ($pythonPath -and (Test-Path -LiteralPath $pythonPath)) {
        return $pythonPath
    }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"),
        (Join-Path $env:ProgramFiles "Python310\python.exe")
    )
    foreach ($registryPath in @(
        "HKCU:\Software\Python\PythonCore\3.10\InstallPath",
        "HKLM:\Software\Python\PythonCore\3.10\InstallPath",
        "HKLM:\Software\WOW6432Node\Python\PythonCore\3.10\InstallPath"
    )) {
        $registryKey = Get-Item -LiteralPath $registryPath -ErrorAction SilentlyContinue
        if ($registryKey) {
            $installPath = $registryKey.GetValue("")
            if ($installPath) {
                $candidates += Join-Path $installPath "python.exe"
            }
        }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    return $null
}

function Install-WingetPackage([string]$Id, [string]$Name) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "Windows Package Manager (winget) is required to install $Name automatically. Install App Installer from Microsoft Store, then run this script again."
    }
    Write-Host "Installing $Name..."
    & $winget.Source install --id $Id --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install $Name (winget exit code $LASTEXITCODE)."
    }
}

function Find-TesseractInstall {
    $candidates = @(
        (Join-Path $env:ProgramFiles "Tesseract-OCR\tesseract.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Tesseract-OCR\tesseract.exe")
    )
    $command = Get-Command tesseract.exe -ErrorAction SilentlyContinue
    if ($command) {
        $candidates += $command.Source
    }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (Test-Path -LiteralPath $candidate) {
            return [PSCustomObject]@{
                Executable = $candidate
                Tessdata = Join-Path (Split-Path -Parent $candidate) "tessdata"
            }
        }
    }
    return $null
}

function Test-TesseractLanguages([object]$Tesseract) {
    $required = @("chi_sim", "eng")
    foreach ($language in $required) {
        if (-not (Test-Path -LiteralPath (Join-Path $Tesseract.Tessdata "${language}.traineddata"))) {
            return $false
        }
    }
    $languages = & $Tesseract.Executable --tessdata-dir $Tesseract.Tessdata --list-langs 2>$null
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    foreach ($language in $required) {
        if ($languages -notcontains $language) {
            return $false
        }
    }
    return $true
}

function Get-ModelCandidate([string]$Language) {
    $candidates = @(
        (Join-Path $ModelCacheDir "${Language}.traineddata"),
        (Join-Path $env:USERPROFILE "Downloads\${Language}.traineddata")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    return $null
}

function Test-TessdataFile([string]$Path, [string]$Language) {
    if (-not (Test-Path -LiteralPath $Path) -or (Get-Item -LiteralPath $Path).Length -lt 1MB) {
        return $false
    }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -eq $TessdataModelHashes[$Language]
}

function Install-ChineseTessdata([object]$Tesseract) {
    New-Item -ItemType Directory -Force -Path $Tesseract.Tessdata | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    foreach ($language in "chi_sim", "eng") {
        $target = Join-Path $Tesseract.Tessdata "${language}.traineddata"
        if (Test-TessdataFile $target $language) {
            continue
        }
        Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        $candidate = Get-ModelCandidate $language
        if ($candidate) {
            Write-Host "Using local $language OCR language data: $candidate"
            Copy-Item -LiteralPath $candidate -Destination $target -Force
        } else {
            Write-Host "Installing $language OCR language data..."
            $client = New-Object System.Net.WebClient
            try {
                $client.DownloadFile("https://github.com/tesseract-ocr/tessdata_fast/raw/main/${language}.traineddata", $target)
            } finally {
                $client.Dispose()
            }
        }
        if (-not (Test-TessdataFile $target $language)) {
            Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
            throw "Tesseract $language language data failed integrity verification."
        }
    }
}

function Escape-IniValue([string]$Value) {
    return $Value.Trim()
}

function Write-Configuration([string]$Url, [string]$Secret, [bool]$UseDryRun, [object]$Tesseract) {
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    $content = @"
[watcher]
dry_run = $(if ($UseDryRun) { '1' } else { '0' })
callback_url = $(Escape-IniValue $Url)
callback_secret = $(Escape-IniValue $Secret)
state_dir = %LOCALAPPDATA%\SynthPay\wechat-watcher\state
poll_interval_seconds = 2
http_timeout_seconds = 8
max_attempts = 30
use_system_proxy = 0
observer_mode = auto
tesseract_path = $(Escape-IniValue $Tesseract.Executable)
tessdata_dir = $(Escape-IniValue $Tesseract.Tessdata)
background_window = 1
background_x = -10000
background_y = -10000
window_titles = $ReceiptWindowTitle
"@
    $encoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllText($ConfigPath, $content, $encoding)
}

function Get-ExistingConfigValue([string]$Key) {
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        return ""
    }
    $match = Get-Content -LiteralPath $ConfigPath | Where-Object { $_ -match ("^\s*" + [regex]::Escape($Key) + "\s*=") } | Select-Object -First 1
    if (-not $match) {
        return ""
    }
    return (($match -split "=", 2)[1]).Trim()
}

function Test-ExistingConfiguration {
    $dryRunValue = Get-ExistingConfigValue "dry_run"
    if ($dryRunValue -match "^(1|true|yes|on)$") {
        return $true
    }
    $url = Get-ExistingConfigValue "callback_url"
    $secret = Get-ExistingConfigValue "callback_secret"
    return $url -match "^https://" -and $url -notmatch "REPLACE_" -and $secret -and $secret -notmatch "REPLACE_"
}

function Update-MachineSpecificConfiguration([object]$Tesseract) {
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        return
    }
    $lines = @(Get-Content -LiteralPath $ConfigPath)
    $updated = @()
    $seenTesseract = $false
    $seenTessdata = $false
    foreach ($line in $lines) {
        if ($line -match '^\s*tesseract_path\s*=') {
            $updated += "tesseract_path = $($Tesseract.Executable)"
            $seenTesseract = $true
        } elseif ($line -match '^\s*tessdata_dir\s*=') {
            $updated += "tessdata_dir = $($Tesseract.Tessdata)"
            $seenTessdata = $true
        } else {
            $updated += $line
        }
    }
    if (-not $seenTesseract) { $updated += "tesseract_path = $($Tesseract.Executable)" }
    if (-not $seenTessdata) { $updated += "tessdata_dir = $($Tesseract.Tessdata)" }
    $encoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllLines($ConfigPath, $updated, $encoding)
}

$python = Get-Python310
if (-not $python) {
    Install-WingetPackage -Id "Python.Python.3.10" -Name "Python 3.10"
    $python = Get-Python310
}
if (-not $python) {
    throw "Python 3.10 installation completed but the Python launcher cannot find it. Restart Windows, then run this script again."
}

$tesseractInstall = Find-TesseractInstall
if (-not $tesseractInstall) {
    Install-WingetPackage -Id "UB-Mannheim.TesseractOCR" -Name "Tesseract OCR with Chinese language data"
    $tesseractInstall = Find-TesseractInstall
}
if (-not $tesseractInstall) {
    throw "Tesseract OCR was not found after installation. Restart Windows, then run this script again."
}
$userTessdata = Join-Path $env:LOCALAPPDATA "SynthPay\wechat-watcher\tessdata"
$tesseract = [PSCustomObject]@{
    Executable = $tesseractInstall.Executable
    Tessdata = $userTessdata
}
Install-ChineseTessdata $tesseract
if (-not (Test-TesseractLanguages $tesseract)) {
    throw "Tesseract OCR cannot load the required chi_sim and eng language data."
}

$configure = -not (Test-Path -LiteralPath $ConfigPath)
if ($PSBoundParameters.ContainsKey("CallbackUrl") -or $PSBoundParameters.ContainsKey("CallbackSecret") -or $DryRun) {
    $configure = $true
}
if (-not $configure -and -not (Test-ExistingConfiguration)) {
    Write-Host "Existing watcher configuration is incomplete or uses placeholder values."
    $configure = $true
}

if ($configure) {
    if (-not $DryRun -and ([string]::IsNullOrWhiteSpace($CallbackUrl) -or [string]::IsNullOrWhiteSpace($CallbackSecret))) {
        if ($NonInteractive) {
            throw "Production setup requires -CallbackUrl and -CallbackSecret. Use -DryRun for an offline installation check."
        }
        Write-Host "No local SynthPay configuration exists."
        $CallbackUrl = Read-Host "SynthPay HTTPS callback URL (press Enter for dry-run mode)"
        if ([string]::IsNullOrWhiteSpace($CallbackUrl)) {
            $DryRun = $true
        } else {
            $CallbackSecret = Read-Host "SynthPay callback secret"
            if ([string]::IsNullOrWhiteSpace($CallbackSecret)) {
                throw "A callback secret is required for production mode."
            }
        }
    }
    if ($DryRun) {
        $CallbackUrl = ""
        $CallbackSecret = ""
    } elseif ($CallbackUrl -notmatch "^https://") {
        throw "Production callback URL must use HTTPS."
    }
    Write-Configuration -Url $CallbackUrl -Secret $CallbackSecret -UseDryRun ([bool]$DryRun) -Tesseract $tesseract
} else {
    Write-Host "Keeping existing local configuration: $ConfigPath"
}
Update-MachineSpecificConfiguration $tesseract

& (Join-Path $SourceDir "install.ps1") -PythonExecutable $python -Start
if ($LASTEXITCODE -ne 0) {
    throw "Watcher installation failed (exit code $LASTEXITCODE)."
}

Write-Host ""
Write-Host "SynthPay watcher is running for the current Windows user."
Write-Host "Open and log in to WeChat, then open 微信收款助手. The watcher starts automatically after future user logons."
Write-Host "Log: $env:LOCALAPPDATA\SynthPay\wechat-watcher\state\watcher.log"
