# Puts down the room that is already running, and sees that a new one comes up.
#
# The room refuses to run twice: app.py will not share its port, so a
# second launch dies on the bind and says so. That is deliberate and it is
# right -- two rooms answer the same line twice and both write it down. It
# also means a restart is two steps and not one, and the first step is easy to
# think you have done. This is both steps, in order, out loud.
#
# There are two worlds, and this reads which one it is in.
#
# Under the icon in the corner (server/tray.py), the second step is not this
# script's any more: the icon brings the room back the moment it is gone, so
# all this does is the stopping, and then waits to watch it return. That is the
# whole cure for the pile of dead windows this script used to leave behind --
# it opened a new PowerShell with -NoExit on every restart, and the assistant
# calls its own restarts, so dead consoles stacked up on the desktop.
#
# Started from a console instead, it does what it always did, and the window it
# opens is the room's, not this one's. Close this and the room stays up.

Set-Location -Path $PSScriptRoot
# Which home this install runs, and so which port and which mark: the room's
# own answer, not a number written here.
$info = python -m server.home --json | ConvertFrom-Json
$port = $info.port
$mark = Join-Path $info.data "tray.json"

# --- is the icon watching the room? --------------------------------------
$watched = $false
if (Test-Path $mark) {
  try {
    $note = Get-Content $mark -Raw | ConvertFrom-Json
    # The mark outlives the icon if the icon was killed rather than closed, so
    # the pid in it is checked rather than believed.
    if (Get-Process -Id $note.tray -ErrorAction SilentlyContinue) { $watched = $true }
  } catch { }
}

$held = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1

if ($held) {
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $($held.OwningProcess)"

  # Never in the middle of a sentence. A turn cut off half way leaves a person
  # with a line that was never answered and nothing on the screen saying why,
  # so if the room is working this asks rather than deciding for them.
  $busy = $false
  try { $busy = (Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/progress" -TimeoutSec 3).busy } catch { }
  if ($busy) {
    Write-Host "$($info.name) is in the middle of a turn right now." -ForegroundColor Yellow
    if ((Read-Host "Stop $($info.name) anyway? (y/N)") -notmatch '^[Yy]') {
      Write-Host "Left $($info.name) alone. Nothing changed."
      return
    }
  }

  Write-Host "Stopping the one already awake -- pid $($proc.ProcessId), up since $($proc.CreationDate)."
  Stop-Process -Id $proc.ProcessId -Force

  # The socket does not come free the instant the process does.
  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 250
    if (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) { break }
  }
} else {
  Write-Host "Nobody was on port $port."
}

# --- the icon's world: it does the picking up ----------------------------
if ($watched) {
  Write-Host "The icon in the corner holds $($info.name). Waiting for it to bring $($info.name) back."
  for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Milliseconds 500
    $now = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if ($now) {
      $fresh = Get-CimInstance Win32_Process -Filter "ProcessId = $($now.OwningProcess)"
      Write-Host "$($info.name) is awake -- pid $($fresh.ProcessId), since $($fresh.CreationDate)." -ForegroundColor Green
      Write-Host "http://localhost:$port  (refresh the page if it is already open)"
      return
    }
  }
  Write-Host "The icon has not brought $($info.name) back yet. Right-click it and show the log." -ForegroundColor Yellow
  return
}

# --- the console world: this script starts it, as it always did ---------
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
  Write-Host "Port $port is still held, so nothing was started." -ForegroundColor Red
  return
}

# -NoExit so the window stays up if the room dies on startup. The reason it stopped
# is the whole value of that window, and a console that closes on the way out
# takes it with it. This is also the thing that used to pile up -- which is why
# the icon exists, and why this branch only runs when there is no icon.
Start-Process -FilePath "powershell.exe" -WorkingDirectory $PSScriptRoot -ArgumentList @(
  "-NoExit", "-Command",
  "`$Host.UI.RawUI.WindowTitle = '$($info.app_name)'; python -m server.app"
)

for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Milliseconds 250
  $now = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
         Select-Object -First 1
  if ($now) {
    $fresh = Get-CimInstance Win32_Process -Filter "ProcessId = $($now.OwningProcess)"
    Write-Host "$($info.name) is awake -- pid $($fresh.ProcessId), since $($fresh.CreationDate)." -ForegroundColor Green
    Write-Host "http://localhost:$port  (refresh the page if it is already open)"
    return
  }
}

Write-Host "$($info.name) has not taken the port yet. Look at the window that just opened." -ForegroundColor Yellow
