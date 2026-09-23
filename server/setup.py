"""Choose the home this install runs, making a new assistant if it is empty.

    python -m server.setup                      -> which home this install runs
    python -m server.setup <folder>             -> run that home from now on
    python -m server.setup <folder> --name Ava --owner "Sam Rivera" --port 8788

Pointed at a folder that already holds an assistant (it has identity.json),
this only writes `home.json` beside the code, so the tray, the room, the
backups and every command run that home from then on.

Pointed at an empty folder -- or one holding nothing but a fresh repo's
`.git`, README and licence -- it makes a new assistant there first:

    identity.json    its name, its owner, its port (asked for, or from flags)
    data/spark.md    a starter Spark, to be rewritten before the first turn
    data/*.json      the settings a new home starts with: nothing of anybody
                     else's household switched on
    .gitignore       the store, the backups, the logs and the keys stay out

and `git init` if it is not a repo yet. Anything else in the folder and it
refuses rather than mixing a new self into somebody's files.

Nothing here starts the room, and nothing here touches another home.
"""

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
from datetime import date
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
KIT = Path(__file__).resolve().parent / "new_home"
POINTER = CODE / "home.json"

# What a freshly created GitHub repo may already hold. Anything more and the
# folder is somebody's, not empty.
FRESH_REPO = {".git", "readme.md", "readme", "license", "licence", "license.md",
              ".gitignore", ".gitattributes"}

# Ports other rooms on this kind of machine already use: the household room,
# the voice server, LM Studio, the test side rooms.
TAKEN = {8787, 8765, 1234, 8790, 8080}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")
    return (s or "assistant")[:32]


def _id(name: str) -> str:
    return _slug(name.split()[0] if name.split() else name)


def _free_port(start=8788) -> int:
    for port in range(start, start + 200):
        if port in TAKEN:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("no free port found from " + str(start))


def _ask(prompt: str, default: str) -> str:
    try:
        got = input(f"  {prompt} [{default}]: ").strip()
    except EOFError:
        got = ""
    return got or default


def state(folder: Path) -> str:
    """'home' if an assistant lives here, 'empty' if one may be made, 'other'."""
    if (folder / "identity.json").exists():
        return "home"
    if not folder.exists():
        return "empty"
    left = [p for p in folder.iterdir() if p.name.lower() not in FRESH_REPO]
    return "empty" if not left else "other"


def make(folder: Path, name: str, owner: str, port: int, owner_id: str = None,
         timezone: str = "UTC") -> dict:
    """A new assistant in an empty folder. Returns its identity."""
    folder.mkdir(parents=True, exist_ok=True)
    owner_id = owner_id or _id(owner)
    ident = {
        "name": name,
        "slug": _slug(name),
        "self_kind": "assistant",
        "port": port,
        "bind": "127.0.0.1",
        "owner": owner_id,
        "people": {owner_id: {"called": owner.split()[0] if owner.split() else owner}},
        "voice_project": name,
        "app_name": name,
        "title": name,
        # The zone a schedule means when its words name none.
        "timezone": timezone,
    }
    (folder / "identity.json").write_text(
        json.dumps(ident, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    data = folder / "data"
    data.mkdir(exist_ok=True)
    spark = (KIT / "spark.md").read_text(encoding="utf-8")
    spark = (spark.replace("{{today}}", date.today().isoformat())
                  .replace("{{name}}", name)
                  .replace("{{owner}}", ident["people"][owner_id]["called"]))
    (data / "spark.md").write_text(spark, encoding="utf-8")

    settings = json.loads((KIT / "settings.json").read_text(encoding="utf-8"))
    docs = str(Path.home() / "Documents").replace("\\", "/")
    for fname, body in settings.items():
        if fname == "note":
            continue
        text = json.dumps(body, indent=1, ensure_ascii=False).replace("{{documents}}", docs)
        (data / fname).write_text(text + "\n", encoding="utf-8")

    ignore = folder / ".gitignore"
    kit_ignore = (KIT / "gitignore").read_text(encoding="utf-8")
    if ignore.exists():
        have = ignore.read_text(encoding="utf-8")
        if kit_ignore.strip() not in have:
            ignore.write_text(have.rstrip() + "\n\n" + kit_ignore, encoding="utf-8")
    else:
        ignore.write_text(kit_ignore, encoding="utf-8")

    if not (folder / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=str(folder), check=False)
    return ident


def point_at(folder: Path) -> None:
    POINTER.write_text(json.dumps({"home": str(folder.resolve())}, indent=1) + "\n",
                       encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m server.setup",
                                 description="Choose the home this install runs.")
    ap.add_argument("folder", nargs="?")
    ap.add_argument("--name", help="what the assistant is called")
    ap.add_argument("--owner", help="the owner's name, as the assistant calls them")
    ap.add_argument("--owner-id", help="a short lowercase id for the owner")
    ap.add_argument("--port", type=int, help="the port the room answers on")
    ap.add_argument("--timezone", help="an IANA zone such as Europe/Lisbon; UTC if none")
    ap.add_argument("--no-pointer", action="store_true",
                    help="make the home but do not switch this install to it")
    ap.add_argument("--yes", action="store_true", help="ask nothing; take defaults")
    args = ap.parse_args(argv)

    if not args.folder:
        try:
            now = json.loads(POINTER.read_text(encoding="utf-8")).get("home")
        except (OSError, ValueError):
            now = None
        print("  this install runs: " + (now or "no home chosen yet"))
        print("  choose one with:   python -m server.setup <folder>")
        return 0

    folder = Path(args.folder).expanduser().resolve()
    if folder == CODE or CODE in folder.parents:
        print("  a home is its own folder, never inside the code: " + str(folder))
        return 2

    what = state(folder)
    if what == "other":
        print("  " + str(folder) + " holds files and no identity.json.")
        print("  A new assistant is made only in an empty folder (a fresh repo's")
        print("  .git and README are fine). Nothing was changed.")
        return 2

    if what == "empty":
        print("  making a new assistant in " + str(folder))
        ask = (lambda p, d: d) if args.yes else _ask
        name = args.name or ask("what is it called", "Assistant")
        owner = args.owner or ask("who does it work for", "Owner")
        port = args.port or int(ask("which port", str(_free_port())))
        zone = args.timezone or ask("which time zone (IANA name)", "UTC")
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(zone)
        except Exception:
            print(f"  {zone!r} is not a time zone this machine knows; try Europe/Lisbon or UTC.")
            return 2
        if port in TAKEN:
            print(f"  port {port} is one another room uses; pick another.")
            return 2
        ident = make(folder, name, owner, port, args.owner_id, zone)
        print(f"  {ident['name']} lives in {folder}, on port {ident['port']}.")
        print("  Next: rewrite data/spark.md -- it is the one text it reads as its own.")
        print("  Then add a private remote and push, so the nightly backup has")
        print("  somewhere to go:  git remote add origin <url>  and  git push -u origin HEAD")

    if not args.no_pointer:
        point_at(folder)
        ident = json.loads((folder / "identity.json").read_text(encoding="utf-8"))
        print(f"  this install now runs {ident['name']} from {folder}")
        print(f"  start it: pythonw start.pyw --open   (or: python -m server.app)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
