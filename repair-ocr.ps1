[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$sourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$installDir = Join-Path $env:LOCALAPPDATA "SynthPay\wechat-watcher"
$modelCacheDir = Join-Path $sourceDir "models"
$tessdataDir = Join-Path $installDir "tessdata"
$tesseract = Join-Path $env:ProgramFiles "Tesseract-OCR\tesseract.exe"
$modelHashes = @{
    chi_sim = "A5FCB6F0DB1E1D6D8522F39DB4E848F05984669172E584E8D76B6B3141E1F730"
    eng = "7D4322BD2A7749724879683FC3912CB542F19906C83BCC1A52132556427170B2"
}

if (-not (Test-Path -LiteralPath $tesseract)) {
    throw "Tesseract was not found at $tesseract. Run setup-and-start.ps1 first."
}

New-Item -ItemType Directory -Force -Path $tessdataDir | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
foreach ($language in "chi_sim", "eng") {
    $target = Join-Path $tessdataDir "${language}.traineddata"
    $cached = Join-Path $modelCacheDir "${language}.traineddata"
    if (Test-Path -LiteralPath $cached) {
        Copy-Item -LiteralPath $cached -Destination $target -Force
    } else {
        $client = New-Object System.Net.WebClient
        try {
            $client.DownloadFile("https://github.com/tesseract-ocr/tessdata_fast/raw/main/${language}.traineddata", $target)
        } finally {
            $client.Dispose()
        }
    }
    $hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    if ($hash -ne $modelHashes[$language]) {
        Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        throw "Tesseract $language language data failed integrity verification."
    }
}

$languages = & $tesseract --tessdata-dir $tessdataDir --list-langs
if ($LASTEXITCODE -ne 0 -or $languages -notcontains "chi_sim" -or $languages -notcontains "eng") {
    throw "Tesseract cannot load chi_sim and eng from $tessdataDir."
}

Write-Host "OCR models repaired and verified: chi_sim, eng"
