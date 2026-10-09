<#
Install what a new Windows machine needs before `python -m server.install`.

    powershell -ExecutionPolicy Bypass -File tools\install-prereqs.ps1
    ... -SkipModel      do not download the small model (about 6 GB)
    ... -DryRun         only say what would be installed

Uses winget, installs only what is missing, and is safe to run again. It
never handles a credential: every sign-in is left to you and listed at the
end. Windows PowerShell 5.1 is enough.
#>
param([switch]$SkipModel, [switch]$DryRun)

$ErrorActionPreference = 'Continue'
$code = Split-Path -Parent $PSScriptRoot
$todo = New-Object System.Collections.Generic.List[string]

function Say($m) { Write-Host "[prereqs] $m" }

function Update-SessionPath {
    $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $u = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($m, $u) | Where-Object { $_ }) -join ';'
}

function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# A real Python, not the Microsoft Store stub that opens the Store.
function Have-Python {
    $p = Get-Command python -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    if ($p.Source -like '*\WindowsApps\*') { return $false }
    & $p.Source -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>$null
    return ($LASTEXITCODE -eq 0)
}

if (-not (Have winget)) {
    Say 'winget is missing. Update "App Installer" from the Microsoft Store, then run this again.'
    exit 1
}

function Ensure($label, $test, $id) {
    if (& $test) { Say "$label is here"; return }
    if ($DryRun) { Say "would install $label ($id)"; return }
    Say "installing $label ($id)"
    winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements
    Update-SessionPath
    if (-not (& $test)) { Say "$label may need a new terminal before it is found" }
}

Update-SessionPath
Ensure 'Git'          { Have git }    'Git.Git'
Ensure 'Python 3.12'  { Have-Python } 'Python.Python.3.12'
Ensure 'GitHub CLI'   { Have gh }     'GitHub.cli'
Ensure 'Claude Code'  { Have claude }  'Anthropic.ClaudeCode'
Ensure 'Tailscale'    { (Have tailscale) -or (Test-Path "$env:ProgramFiles\Tailscale\tailscale.exe") } 'Tailscale.Tailscale'
$lmHome = Join-Path $env:USERPROFILE '.lmstudio'
$lmExe  = Join-Path $env:LOCALAPPDATA 'Programs\LM Studio\LM Studio.exe'
Ensure 'LM Studio'    { (Test-Path $lmExe) -or (Test-Path $lmHome) } 'ElementLabs.LMStudio'
Say 'Codex (OpenAI.Codex, or the ChatGPT app) is optional; install it if you want the assistant to work through it.'

if ($DryRun) { Say 'dry run: nothing changed'; exit 0 }

if (Have git) { git config --global core.longpaths true; Say 'git core.longpaths is true' }

# LM Studio makes ~/.lmstudio (and the `lms` command) the first time it runs.
if (-not (Test-Path $lmHome) -and (Test-Path $lmExe)) {
    Say 'starting LM Studio once so it sets itself up'
    Start-Process -FilePath $lmExe | Out-Null
    for ($i = 0; $i -lt 60 -and -not (Test-Path (Join-Path $lmHome 'bin')); $i++) { Start-Sleep -Seconds 2 }
}
$lms = Join-Path $lmHome 'bin\lms.exe'
if (-not (Have lms) -and (Test-Path $lms)) { $env:Path += ';' + (Split-Path $lms) }

# The model the code asks for: the first one in server/recall.py's MODELS.
$model = $null
$recall = Join-Path $code 'server\recall.py'
if (Test-Path $recall) {
    $hit = Select-String -Path $recall -Pattern '^\s*\{"key":\s*"([^"]+)"' | Select-Object -First 1
    if ($hit) { $model = $hit.Matches[0].Groups[1].Value }
}

if ($SkipModel) {
    Say 'skipping the model download (-SkipModel)'
} elseif (-not $model) {
    Say 'could not read the model name from server\recall.py; download one in LM Studio'
} elseif (-not (Have lms)) {
    Say 'the lms command is not there yet; open LM Studio once, then run this again'
} else {
    $have = (lms ls 2>$null | Out-String)
    if ($have -match [regex]::Escape($model)) {
        Say "model $model is already downloaded"
    } else {
        # `lms get` aborts inside a captured shell, so it runs detached, its
        # output to files, and we wait for it.
        Say "downloading $model (several GB; this can take a while)"
        $log = Join-Path $env:TEMP 'lms-get.out.txt'
        $err = Join-Path $env:TEMP 'lms-get.err.txt'
        $exe = (Get-Command lms).Source
        $p = Start-Process -FilePath $exe -ArgumentList @('get', $model, '--yes') `
            -RedirectStandardOutput $log -RedirectStandardError $err -WindowStyle Hidden -PassThru
        $p.WaitForExit()
        Say "lms get finished (exit $($p.ExitCode)); log: $log"
        if ($p.ExitCode -ne 0) { Say "the download did not finish; see $err, or get the model in LM Studio's own search" }
    }
    $s = Start-Process -FilePath (Get-Command lms).Source -ArgumentList @('server', 'start') `
        -RedirectStandardOutput (Join-Path $env:TEMP 'lms-server.out.txt') `
        -RedirectStandardError (Join-Path $env:TEMP 'lms-server.err.txt') -WindowStyle Hidden -PassThru
    $s.WaitForExit(60000) | Out-Null
    Say 'LM Studio server asked to start (port 1234)'
}

Write-Host ''
Write-Host 'Still for you to do by hand (nothing here touches a sign-in):'
@(
    'gh auth login                      (GitHub: the backups come through it)',
    'claude                             (run once and sign in to Claude Code)',
    'Codex / ChatGPT app                (optional: install and sign in once)',
    'Tailscale                          (open it and sign in; same account as your phone)',
    'LM Studio > Developer > settings   (turn on "start server on login")'
) | ForEach-Object { Write-Host "  - $_" }
Write-Host ''
Write-Host 'Then, from the code folder:  python -m server.install ..\<home>'
