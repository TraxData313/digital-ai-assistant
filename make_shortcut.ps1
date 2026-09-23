<#
    Puts the room on the Desktop, and into the machine's own waking-up, so it
    is there without a terminal and without being asked for.

        .\make_shortcut.ps1                 # Desktop
        .\make_shortcut.ps1 -Startup        # and started at log-in
        .\make_shortcut.ps1 -StartMenu      # and in the Start menu
        .\make_shortcut.ps1 -All            # all three
        .\make_shortcut.ps1 -Remove         # take them all away again
        .\make_shortcut.ps1 -WhatIf         # say what it would do

    A script rather than an installer, on purpose: a shortcut is the sort of
    thing you want back after tidying a Desktop, without that meaning
    reinstalling anything.

    Both shortcuts open the same icon in the corner; only the Desktop one
    passes --open, so double-clicking it puts the room on screen and logging
    in does not throw a browser tab at anybody.
#>

param(
    [string]$PythonExe,
    [switch]$Startup,
    [switch]$StartMenu,
    [switch]$All,
    [switch]$Remove,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
# Named for the home this install runs, so two assistants on one machine get
# two shortcuts rather than one overwriting the other.
$info = python -m server.home --json | ConvertFrom-Json
if (-not $info.real) { throw "No home is chosen yet. Run: python -m server.setup <folder>" }
$name = "$($info.app_name).lnk"

function Say($msg, $colour = "Gray") { Write-Host $msg -ForegroundColor $colour }

# Not $desktop / $startup / $menu: PowerShell's variables do not care about
# case, so `$startup = ...` writes over the `-Startup` switch above and dies
# saying a string is not a SwitchParameter.
$desktopDir = [Environment]::GetFolderPath("Desktop")
$startupDir = [Environment]::GetFolderPath("Startup")
$menuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"

# Desktop always; the other two on request. Each carries whether it opens the
# room as well as starting it.
$places = @(@{ dir = $desktopDir; open = $true })
if ($Startup -or $All) { $places += @{ dir = $startupDir; open = $false } }
if ($StartMenu -or $All) { $places += @{ dir = $menuDir; open = $true } }

if ($Remove) {
    foreach ($dir in @($desktopDir, $startupDir, $menuDir)) {
        $path = Join-Path $dir $name
        if (Test-Path $path) {
            if (-not $WhatIf) { Remove-Item $path -Force }
            Say "removed       : $path" "Yellow"
        }
    }
    Say "$($info.name) is still awake if it was awake. This only took the icons away." "Gray"
    return
}

# --- what it points at ----------------------------------------------------
# pythonw, not python: python.exe would put a console behind the tray icon for
# as long as the room is up, which is the entire thing we are getting rid of.
if (-not $PythonExe) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $PythonExe = $cmd.Source }
}
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    throw "Could not find python.exe. Pass -PythonExe C:\path\to\python.exe"
}
$pythonw = Join-Path (Split-Path $PythonExe) "pythonw.exe"
if (-not (Test-Path $pythonw)) {
    throw "Found $PythonExe but no pythonw.exe beside it, and pythonw is the whole trick."
}

$launcher = Join-Path $repo "start.pyw"
$icon = $info.icon
if (-not (Test-Path $launcher)) { throw "No start.pyw in $repo" }

Say "opens         : $launcher"
Say "with          : $pythonw"

foreach ($place in $places) {
    $path = Join-Path $place.dir $name
    if ($WhatIf) {
        Say "would write   : $path" "Yellow"
        continue
    }
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pythonw
    # Quoted: this is read by the shell rather than by cmd.exe, and a repo
    # path with a space in it would otherwise arrive as two arguments.
    $link.Arguments = "`"$launcher`""
    if ($place.open) { $link.Arguments += " --open" }
    $link.WorkingDirectory = $repo
    $link.Description = "$($info.app_name) -- the room, and the icon that keeps it"
    if (Test-Path $icon) { $link.IconLocation = "$icon,0" }
    $link.Save()
    Say "shortcut      : $path" "Green"
}

if (-not $WhatIf -and ($Startup -or $All)) {
    Say ""
    Say "$($info.name) will be up when you log in. Nothing opens; look for its icon in the corner." "Gray"
}
