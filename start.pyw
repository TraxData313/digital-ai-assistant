"""What the shortcut opens: the Digital Assistant Manager.

`.pyw` and `pythonw.exe` are the whole reason there is no window: that pair
has no console and cannot be given one, so nothing the rooms do -- restarting
themselves several times in an evening included -- can leave a black
rectangle behind on the desktop. Everything they say goes to each home's
`logs/`, and the manager's own story to `logs/` beside the code.

`--open` puts the manager's page on screen once it is up. The Desktop
shortcut passes it, the Startup one does not.

The console ways still work and are still worth having when something is wrong:

    python -m server.manager   # the manager, in front of you
    python -m server.app       # one room on its own, saying everything
    .\run.ps1                  # the same thing, one keystroke shorter
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import manager

raise SystemExit(manager.main(sys.argv[1:]))
