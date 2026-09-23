"""The pocket door: named people, each with a key that outlives a restart.

There is already a key in this house -- the angel session's, in `angel.py` --
and this one is deliberately not like it. That one is minted fresh every time
the room opens, because it is handed to a process that starts in the same
breath and a key that outlived a restart would be a key worth stealing. This
one is carried in a phone that is not here when the room restarts. If it were
reminted at boot, every restart would silently un-pair the owner's phone, and
the failure would look like the room being down rather than the key being
changed. So: **minted once and kept**. The trade is real and it is chosen -- a
longer-lived secret, in exchange for a pairing that survives a machine that
reboots.

The keys live one per file under `data/people/`, named for the person, because
the room has to know *which* person knocked and not merely that somebody did.
The owner's is made on the first boot that finds it missing. The mechanism
takes any name; nobody else's is created here.

What it is worth, honestly. The key alone is not the fence. Anything not on
loopback must *also* come from inside 100.64.0.0/10 -- the Tailscale range -- so
a key that leaked to the open internet still reaches nothing, because nothing
outside the tailnet can present a source address in that range to this machine.
Two walls, and the one made of network is the load-bearing one. The key is what
keeps a stranger already inside the tailnet out of the room.

Loopback also requires pairing authentication. Native command networking must
not turn localhost into owner privileges. Trusted desktop clients read the same
existing key; model commands are denied its storage location.
"""

import ipaddress
import re
import secrets
from pathlib import Path
from . import home

ROOT = Path(__file__).resolve().parent.parent
TOKEN_DIR = home.DATA / "people"

# Tailscale's slice of the carrier-grade NAT range. Every node in a tailnet gets
# a 100.x address out of this; nothing routable on the open internet is in it.
TAILNET = ipaddress.ip_network("100.64.0.0/10")

# A name is a file name, so it is held to being one. Not a validation nicety:
# `..` here would be a path out of the folder.
NAME_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# The household. The door above decides who may come IN -- a key, or being
# this machine. This bounds something smaller: what a line may be *signed*
# as. People who share one screen switch between themselves freely -- no
# key on it, because they are not hiding from each other. The label is
# attribution, not a lock. A name outside this list is refused in words, so
# a typo cannot quietly mint an extra person.
HOUSEHOLD = home.HOUSEHOLD

# How each of them is written when drawn or said. Ids stay short and
# lowercase; people are called by the names their home gives them.
CALLED = dict(home.CALLED)


class BadName(ValueError):
    pass


def _path(name: str) -> Path:
    name = (name or "").strip().lower()
    if not NAME_OK.match(name):
        raise BadName("a person's name here is lowercase letters, digits, "
                      "dash and underscore, 1-32 of them: " + repr(name))
    return TOKEN_DIR / (name + ".token")


def mint(name: str) -> str:
    """This person's key, made if it is not there and *kept* if it is.

    Idempotent on purpose -- calling it on every boot is how the owner's gets
    made the first time without un-pairing their phone on the second."""
    path = _path(name)
    have = read_token(name)
    if have:
        return have
    token = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written whole or not at all: a half-written key is a phone that stops
    # working for a reason nobody can see.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(token, encoding="utf-8")
    tmp.replace(path)
    return token


def remint(name: str) -> str:
    """Throw this person's key away and make another. Nothing calls it yet; it
    is here because the answer to *a phone was lost* has to exist somewhere."""
    try:
        _path(name).unlink()
    except OSError:
        pass
    return mint(name)


def read_token(name: str) -> str:
    try:
        return _path(name).read_text(encoding="utf-8").strip()
    except (OSError, BadName):
        return ""


def room_headers():
    """For trusted, local room clients only; never put this in command env/logs."""
    return {"Cookie": home.COOKIE + "=" + read_token(home.OWNER)}


def open_room(port=None):
    """Trusted desktop launch through the existing pairing hand-off. No output."""
    import webbrowser
    from urllib.parse import urlencode
    key = read_token(home.OWNER)
    webbrowser.open("http://localhost:" + str(port or home.PORT) + "/" + ("?" + urlencode({"k": key}) if key else ""))


def everyone() -> dict:
    """Every paired person, name to key. Read off the disk each time rather than
    cached, so pairing someone new does not need a restart to take."""
    out = {}
    try:
        for f in sorted(TOKEN_DIR.glob("*.token")):
            try:
                key = f.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if key:
                out[f.stem.lower()] = key
    except OSError:
        pass
    return out


def whose(key: str) -> str:
    """Whose key this is, or "" for nobody's.

    Every candidate is compared even after a match, and always with
    `compare_digest`, so neither the answer nor the number of paired people can
    be read off how long this took."""
    key = (key or "").strip()
    found = ""
    for name, mine in everyone().items():
        if len(key) == len(mine) and secrets.compare_digest(key, mine):
            found = found or name
    return found if key else ""


def in_tailnet(addr) -> bool:
    """Whether this source address is inside the Tailscale range. IPv6 is not in
    it and never can be, so a v6 caller that is not loopback is simply out --
    said plainly here rather than discovered at the door."""
    try:
        ip = addr if isinstance(addr, (ipaddress.IPv4Address,
                                       ipaddress.IPv6Address)) \
            else ipaddress.ip_address(str(addr))
    except ValueError:
        return False
    return ip.version == 4 and ip in TAILNET


def my_tailnet_address() -> str:
    """This machine's own address inside the tailnet, or "" if it has none right
    now. Asked at render time rather than at boot: Tailscale may come up after
    the room does, and an address read once at boot would be a blank line
    forever on the day it did."""
    import socket
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None,
                                   family=socket.AF_INET)
    except OSError:
        return ""
    for info in infos:
        addr = info[4][0]
        if in_tailnet(addr):
            return addr
    return ""
