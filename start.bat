@echo off
rem The old way in, kept pointing at the new one.
rem
rem This used to BE the room: a console that ran `python -m server.app` and
rem sat there. That console is gone -- the room restarts itself now, and
rem restart.ps1 opened a fresh window every time without closing the dead one,
rem so they piled up on the desktop.
rem
rem It is still here because it may be pinned somewhere, and a shortcut that
rem does nothing is worse than one that does the right thing. It hands off to
rem the icon in the corner, which is what the Desktop shortcut opens.
rem
rem To watch a boot in front of you, which is the one thing the icon cannot do:
rem     .\run.ps1

cd /d "%~dp0"
start "" pythonw.exe "%~dp0start.pyw" --open
