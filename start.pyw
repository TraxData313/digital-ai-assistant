"""What the shortcut opens.

`.pyw` and `pythonw.exe` are the whole reason there is no window: that pair
has no console and cannot be given one, so nothing the room does -- restarting
itself several times in an evening included -- can leave a black rectangle
behind on the desktop. Everything it says goes to `logs/` instead.

`--open` puts the room on screen a moment after it starts. The Desktop
shortcut passes it, the Startup one does not.

The console way still works and is still worth having when something is wrong:

    python -m server.app       # the room, in front of you, saying everything
    .\run.ps1                  # the same thing, one keystroke shorter
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import tray

raise SystemExit(tray.main())
