<#
    OttDeck one-click setup.

    Finds a usable Python, builds the virtualenv, installs the package,
    collects a TMDB key, creates shortcuts, and offers a first sync.
    Safe to re-run: every step detects what is already done.
#>

[CmdletBinding()]
param(
    [switch]$NoPrompt,       # unattended: skip the key prompt and the sync offer
    [switch]$NoShortcuts
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$VenvDir = Join-Path $Root '.venv'
$VenvPy  = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPyW = Join-Path $VenvDir 'Scripts\pythonw.exe'
$EnvFile = Join-Path $Root '.env'
$IconFile = Join-Path $Root 'assets\ottdeck.ico'
$MinVersion = [Version]'3.10'

function Write-Step  ($m) { Write-Host "`n  $m" -ForegroundColor Cyan }
function Write-Ok    ($m) { Write-Host "    $m"  -ForegroundColor Green }
function Write-Info  ($m) { Write-Host "    $m"  -ForegroundColor DarkGray }
function Write-Warn2 ($m) { Write-Host "    $m"  -ForegroundColor Yellow }

Write-Host ""
Write-Host "  OttDeck setup" -ForegroundColor White
Write-Host "  =============" -ForegroundColor DarkGray

# ---------------------------------------------------------------- python

function Get-PythonCandidates {
    $found = @()
    # The py launcher knows about every installed version; prefer newest first.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($tag in '-3.14', '-3.13', '-3.12', '-3.11', '-3.10', '-3') {
            $found += ,@('py', $tag)
        }
    }
    foreach ($name in 'python', 'python3') {
        if (Get-Command $name -ErrorAction SilentlyContinue) { $found += ,@($name) }
    }
    return $found
}

function Resolve-Python {
    foreach ($cand in Get-PythonCandidates) {
        $exe = $cand[0]
        # NB: `1..0` counts DOWN in PowerShell, so slice only when there is a tail.
        # `$args` is an automatic variable here, hence $pyArgs.
        $pyArgs = if ($cand.Length -gt 1) { @($cand[1..($cand.Length - 1)]) } else { @() }
        try {
            $out = & $exe @pyArgs -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
        try { $v = [Version]($out | Select-Object -First 1).Trim() } catch { continue }
        if ($v -ge $MinVersion) {
            return [pscustomobject]@{ Exe = $exe; PyArgs = $pyArgs; Version = $v }
        }
    }
    return $null
}

Write-Step 'Looking for Python 3.10 or newer...'

if (Test-Path $VenvPy) {
    $existing = & $VenvPy -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
    Write-Ok "Existing environment found (Python $existing)"
} else {
    $py = Resolve-Python
    if (-not $py) {
        Write-Host ""
        Write-Warn2 'No suitable Python found.'
        Write-Info  'Install Python 3.12 from https://www.python.org/downloads/'
        Write-Info  'Tick "Add python.exe to PATH" during install, then run this again.'
        Write-Host ""
        if (-not $NoPrompt) { Read-Host '  Press Enter to close' }
        exit 1
    }
    Write-Ok "Found Python $($py.Version)"

    Write-Step 'Creating the virtual environment...'
    & $py.Exe @($py.PyArgs) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
    Write-Ok '.venv created'
}

# ------------------------------------------------------------- packages

Write-Step 'Installing dependencies (this can take a few minutes)...'
& $VenvPy -m pip install --upgrade --quiet pip
& $VenvPy -m pip install --quiet -e .
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

& $VenvPy -c "import PySide6, httpx, rapidfuzz" 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Packages installed but will not import.' }
Write-Ok 'All packages installed'

# pythonw launches without a console window; fall back if it is missing.
$Launcher = if (Test-Path $VenvPyW) { $VenvPyW } else { $VenvPy }

# ------------------------------------------------------------------ icon

if (-not (Test-Path $IconFile)) {
    & $VenvPy (Join-Path $Root 'scripts\make_icon.py') | Out-Null
}

# ------------------------------------------------------------- tmdb key

function Read-Region {
    # TMDB wants ISO 3166-1 alpha-2. A dialling code here is silently accepted
    # by the API and returns an empty catalogue, so reject it at the prompt.
    $dialling = @{ '+91' = 'IN'; '91' = 'IN'; '+1' = 'US'; '1' = 'US';
                   '+44' = 'GB'; '44' = 'GB'; '+61' = 'AU'; '+971' = 'AE' }
    while ($true) {
        Write-Host ""
        Write-Info 'Two-letter country code, e.g. IN (India), US, GB, AE.'
        $r = (Read-Host '    Country code [IN]').Trim()
        if (-not $r) { return 'IN' }
        if ($r -match '^[A-Za-z]{2}$') { return $r.ToUpper() }
        if ($dialling.ContainsKey($r)) {
            $m = $dialling[$r]
            Write-Warn2 "$r is a phone dialling code. You probably mean $m."
            $ok = Read-Host "    Use $m? [Y/n]"
            if ($ok -notmatch '^[Nn]') { return $m }
            continue
        }
        Write-Warn2 "'$r' is not a two-letter country code."
    }
}

function Test-TmdbKey {
    param([string]$Key)
    try {
        $r = Invoke-RestMethod -TimeoutSec 15 -Method Get `
            -Uri "https://api.themoviedb.org/3/configuration?api_key=$Key"
        return [bool]$r.images
    } catch { return $false }
}

$hasKey = $false
if (Test-Path $EnvFile) {
    $line = Select-String -Path $EnvFile -Pattern '^\s*TMDB_(API_KEY|BEARER)\s*=\s*\S' -Quiet
    if ($line) { $hasKey = $true; Write-Step 'TMDB key already configured'; Write-Ok 'Using the key in .env' }
}

if (-not $hasKey -and -not $NoPrompt) {
    Write-Step 'TMDB API key'
    Write-Info 'OttDeck reads its catalogue from TMDB. The key is free.'
    Write-Info 'Get one at: https://www.themoviedb.org/settings/api'
    Write-Info '(sign up, then copy the "API Key (v3 auth)" value)'
    Write-Host ""
    $open = Read-Host '    Open that page in your browser now? [Y/n]'
    if ($open -notmatch '^[Nn]') { Start-Process 'https://www.themoviedb.org/settings/api' }

    for ($i = 1; $i -le 3; $i++) {
        Write-Host ""
        $key = (Read-Host '    Paste your TMDB API key (or Enter to skip)').Trim()
        if (-not $key) { Write-Warn2 'Skipped - add it to .env later.'; break }

        Write-Info 'Checking the key...'
        if (Test-TmdbKey $key) {
            $region = Read-Region
            $lines = @(
                '# Created by setup. Get a key at https://www.themoviedb.org/settings/api'
                "TMDB_API_KEY=$key"
                'TMDB_BEARER='
                ''
                '# Two-letter ISO country code (IN, US, GB) - NOT a dialling code.'
                '# Streaming rights are per country; this decides what shows as available.'
                "OTTDECK_REGION=$region"
                'OTTDECK_LANGUAGE=en-US'
            )
            # Set-Content -Encoding utf8 writes a BOM on PowerShell 5.1, which
            # ends up inside the first key name when a reader is not BOM-aware.
            [System.IO.File]::WriteAllLines(
                $EnvFile, $lines, (New-Object System.Text.UTF8Encoding $false))
            Write-Ok "Key saved to .env (region $($region.ToUpper()))"
            $hasKey = $true
            break
        }
        Write-Warn2 "That key was rejected by TMDB. ($i of 3)"
    }
}

# -------------------------------------------------------------- database

Write-Step 'Preparing the database...'
& $VenvPy -c "from ottdeck import db; db.init_db(); print(db.DB_PATH if hasattr(db,'DB_PATH') else '')" | Out-Null
& $VenvPy -m ottdeck stats 2>$null | Select-Object -First 1 | ForEach-Object { Write-Ok $_.Trim() }

# ------------------------------------------------------------- shortcuts

if (-not $NoShortcuts) {
    Write-Step 'Creating shortcuts...'
    $shell = New-Object -ComObject WScript.Shell

    $places = @{
        'Desktop'    = [Environment]::GetFolderPath('Desktop')
        'Start menu' = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
    }
    foreach ($name in $places.Keys) {
        $dir = $places[$name]
        if (-not (Test-Path $dir)) { continue }
        try {
            $lnk = $shell.CreateShortcut((Join-Path $dir 'OttDeck.lnk'))
            $lnk.TargetPath       = $Launcher
            $lnk.Arguments        = '"' + (Join-Path $Root 'app.py') + '"'
            $lnk.WorkingDirectory = $Root
            $lnk.Description      = 'One catalogue across every streaming service'
            if (Test-Path $IconFile) { $lnk.IconLocation = $IconFile }
            $lnk.Save()
            Write-Ok "$name shortcut created"
        } catch {
            Write-Warn2 "Could not create the $name shortcut: $($_.Exception.Message)"
        }
    }
}

# ----------------------------------------------------------- first sync

if ($hasKey -and -not $NoPrompt) {
    Write-Step 'First catalogue sync'
    Write-Info 'Pulls a few hundred titles with cast, genres and where each one streams.'
    Write-Info 'Takes roughly a minute. You can always do it later with Ctrl+R in the app.'
    Write-Host ""
    $doSync = Read-Host '    Run it now? [Y/n]'
    if ($doSync -notmatch '^[Nn]') {
        & $VenvPy -m ottdeck sync --pages 5
    }
}

# ---------------------------------------------------------------- finish

Write-Host ""
Write-Host "  Setup complete." -ForegroundColor Green
if (-not $hasKey) {
    Write-Warn2 'No TMDB key yet - the app opens but the library stays empty.'
    Write-Info  'Add TMDB_API_KEY to the .env file, then press Ctrl+R in the app.'
}
Write-Info "Launch from the desktop shortcut, or run.bat in this folder."
Write-Host ""

if (-not $NoPrompt) {
    $launch = Read-Host '    Open OttDeck now? [Y/n]'
    if ($launch -notmatch '^[Nn]') {
        Start-Process -FilePath $Launcher `
                      -ArgumentList ('"' + (Join-Path $Root 'app.py') + '"') `
                      -WorkingDirectory $Root
    }
}
