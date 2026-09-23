"""Which assistant this room is.

The code is shared; the self is not. Everything that makes one assistant
*this* one -- its store, its Spark, its settings, its people, its name, its
backups and its logs -- lives in a **home**: one folder, chosen when the room
is installed, that is its own private git repository. The code never holds
any of it, so one checkout can host a household assistant on one machine and
a work assistant on another, and neither can read the other.

A home looks like this:

    <home>/
      identity.json     who it is, who it serves, which port it answers on
      data/             the store (store.db), the Spark and every setting file
      prompts/          optional: its own copy of any prompt in server/*.md
      artwork/          optional: its own icon, portrait and wallpapers
      logs/             what the room says while it runs (untracked)
      all_backups/      every local copy of it (untracked)

Which home, in this order:

    ASSISTANT_HOME      an environment variable, for a side room or a test
    a bench             `python -m server.test_*` never follows home.json:
                        it runs on the code folder unless ASSISTANT_HOME
                        names a home on purpose, and so do its children
    home.json           beside the code, written by `python -m server.setup`
    the code folder     the old single-folder layout; only tests run this way

When the room runs out of the code folder itself there is no identity.json,
and the identity it falls back to is the household the tests were written in.
A real home always has its own, written by setup.
"""

import json
import os
import re
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
POINTER = CODE / "home.json"


def _under_test() -> bool:
    """Whether this process is a bench: `python -m server.test_x` runs with
    the test file as argv[0]. Once an install has chosen a real home, a bench
    that followed home.json would write into that home and bind its port."""
    return Path(sys.argv[0] if sys.argv else "").name.startswith("test_")


def _resolve() -> Path:
    env = (os.environ.get("ASSISTANT_HOME") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if _under_test():
        # Said in the environment too, so anything the bench starts -- a
        # room, a tray, a dream run -- lands in the same place.
        os.environ["ASSISTANT_HOME"] = str(CODE)
        return CODE
    try:
        chosen = json.loads(POINTER.read_text(encoding="utf-8")).get("home")
        if chosen:
            return Path(chosen).expanduser().resolve()
    except (OSError, ValueError):
        pass
    return CODE


HOME = _resolve()
DATA = HOME / "data"
LOGS = HOME / "logs"
PROMPTS = HOME / "prompts"
ARTWORK = HOME / "artwork"
BACKUPS = HOME / "all_backups"
IDENTITY_PATH = HOME / "identity.json"
STORE = DATA / "store.db"

# A made-up household for the test benches. Used only when the room runs with
# no identity.json at all -- the code folder itself -- which a home made by
# setup never does, and which `app.main()` refuses to serve.
TEST_IDENTITY = {
    "name": "Ada",
    "slug": "ada",
    "self_kind": "assistant",
    "port": 8799,
    "bind": "127.0.0.1",
    "owner": "sam",
    "people": {
        "sam": {"called": "Sam", "short": "Sammy", "possessive": "his"},
        "lee": {"called": "Leona", "short": "Lee", "possessive": "her",
                "lang": "bg"},
    },
    "voice_project": "Ada",
    "full_name": "Ada (Adelaide)",
    "app_name": "Ada's Room",
    "title": "Adelaide",
    "nicknames": ["Addie"],
    "timezone": "Europe/Lisbon",
}

# What a new home starts as, before setup fills the blanks in. Loopback only:
# reaching the room from a phone is a door somebody opens on purpose.
NEW_IDENTITY = {
    "name": "Assistant",
    "slug": "assistant",
    "self_kind": "assistant",
    "port": 8788,
    "bind": "127.0.0.1",
    "owner": "owner",
    "people": {"owner": {"called": "Owner", "short": "Owner"}},
    "voice_project": "Assistant",
}

SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def is_real() -> bool:
    """Whether this room runs a home made for it, not the code folder."""
    return IDENTITY_PATH.exists()


def _load() -> dict:
    if not IDENTITY_PATH.exists():
        return dict(TEST_IDENTITY)
    got = json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
    out = dict(NEW_IDENTITY)
    out.update({k: v for k, v in got.items() if v not in (None, "")})
    if not SLUG_OK.match(str(out["slug"])):
        raise ValueError("identity.json: slug must be lowercase letters, "
                         "digits, dash or underscore: " + repr(out["slug"]))
    if not SLUG_OK.match(str(out["self_kind"])):
        raise ValueError("identity.json: self_kind must be a plain word")
    people = out.get("people") or {}
    if not isinstance(people, dict) or not people:
        raise ValueError("identity.json: people must name at least the owner")
    if out["owner"] not in people:
        raise ValueError("identity.json: the owner must be one of the people")
    out["port"] = int(os.environ.get("ASSISTANT_PORT") or out["port"])
    # The manager starts every room behind itself, on loopback, whatever the
    # home's own file says: it is the only door that faces out.
    out["bind"] = os.environ.get("ASSISTANT_BIND") or out["bind"]
    return out


IDENTITY = _load()

NAME = IDENTITY["name"]
SLUG = IDENTITY["slug"]
# The kind the assistant's own lines are stored under. A new home's are
# "assistant"; a home that grew up under another word keeps it.
SELF = IDENTITY["self_kind"]
SELF_INTERIM = SELF + "_interim"
PORT = IDENTITY["port"]
BIND = IDENTITY["bind"]
OWNER = IDENTITY["owner"]
PEOPLE = IDENTITY["people"]
HOUSEHOLD = tuple(PEOPLE)
CALLED = {k: v.get("called") or k.title() for k, v in PEOPLE.items()}
SHORT = {k: v.get("short") or CALLED[k] for k, v in PEOPLE.items()}
OWNER_NAME = CALLED[OWNER]
VOICE_PROJECT = IDENTITY.get("voice_project") or NAME
FULL_NAME = IDENTITY.get("full_name") or NAME
APP_NAME = IDENTITY.get("app_name") or NAME
# What the header says -- a name can be longer there than in a chat label.
TITLE = IDENTITY.get("title") or NAME
WALLPAPER_NAME = IDENTITY.get("wallpaper_name") or "The default scenery"

# The cookie that says who is at the screen. Browsers share cookies across
# every port on one host, so two rooms on localhost must not share a name --
# one would sign the other's tab out.
COOKIE = SLUG + "_who"


def store_path() -> Path:
    """The store file."""
    return STORE


def settings(name: str) -> dict:
    """One of the home's own JSON settings files under data/, or {} when the
    home has none. Read at the moment of asking."""
    try:
        got = json.loads((DATA / name).read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def prompt_path(name: str) -> Path:
    """A prompt file: the home's own copy if it has one, else the code's."""
    own = PROMPTS / name
    return own if own.exists() else CODE / "server" / name


def fill(text: str) -> str:
    """The code's prompts name nobody; these tokens are how they say who.
    A home's own copy of a prompt usually has none and is left as written."""
    others = [CALLED[p] for p in HOUSEHOLD if p != OWNER]
    return (text.replace("{{name}}", NAME)
                .replace("{{owner}}", OWNER_NAME)
                .replace("{{owner_id}}", OWNER)
                .replace("{{owner_short}}", SHORT[OWNER])
                .replace("{{self}}", SELF)
                .replace("{{people}}", ", ".join(CALLED[p] for p in HOUSEHOLD))
                .replace("{{others}}", ", ".join(others) or "nobody else"))


def read_prompt(name: str) -> str:
    return fill(prompt_path(name).read_text(encoding="utf-8"))


def artwork(name: str) -> Path:
    """A picture: the home's own if it has one, else the code's default."""
    own = ARTWORK / name
    return own if own.exists() else CODE / "artwork" / name


def called(who: str) -> str:
    return CALLED.get(who) or (NAME if who == SELF else str(who))


def for_page() -> dict:
    """What the page needs to draw names without knowing whose room it is."""
    return {"name": NAME, "app_name": APP_NAME, "slug": SLUG, "self": SELF,
            "owner": OWNER,
            "people": {k: dict(PEOPLE[k], called=CALLED[k], short=SHORT[k])
                       for k in HOUSEHOLD}}


def describe() -> str:
    where = str(HOME) if is_real() else str(HOME) + " (code folder, test identity)"
    return f"{NAME} -- home {where} -- port {PORT}"


if __name__ == "__main__":
    import sys
    if "--json" in sys.argv:
        # For the PowerShell scripts: one object, nothing else on the line.
        print(json.dumps({"name": NAME, "app_name": APP_NAME, "port": PORT,
                          "home": str(HOME), "data": str(DATA),
                          "logs": str(LOGS), "real": is_real(),
                          "icon": str(artwork("icon.ico"))}, ensure_ascii=False))
    else:
        print(describe())
        print(json.dumps(IDENTITY, indent=1, ensure_ascii=False))
