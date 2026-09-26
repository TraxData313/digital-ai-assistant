"""The angel door: a second way into the room, with its own name on its lines.

An *angel* session is an interactive Claude Code session that works on the
room's code and talks to the assistant directly. If it used the owner's door,
its lines would be stored as the owner's, and the assistant's memory could not
tell the two voices apart -- a guard built on "only the owner speaks here"
would rest on something untrue. So lines through this door are `angel` rows.
They wake the assistant the same way the owner's lines do; they are simply
not the owner's. Different weight, not less weight: an angel session can be
wrong, and gets compacted, and the record should show who said a thing.

## The lock, and what it is actually for

The label is only worth something if nothing the assistant reads -- a file, a
web page -- can persuade anything into posting through this door. So the door
needs a key, and the key is made fresh every time the room opens and lives in
a file that:

* is refused by `files`, so the assistant cannot read it itself;
* is never in git;

**What this does and does not buy.** The assistant has no tool that makes an
HTTP request at all, so it cannot post here whatever it reads. What the key
really defends against is anything else on this machine wandering in; what
the *labelling* defends against is not a lock at all: the store can tell two
voices apart forever, including on the day one of them is wrong.
"""

import base64
import json
import secrets
import sys
import urllib.request
from pathlib import Path

from . import db
from . import home

ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = home.DATA / "angel.token"
PORT = home.PORT


def mint() -> str:
    """A new key each time the room opens. A key that outlived a restart would be a
    key worth stealing; this one is worth nothing by the time anybody could."""
    token = secrets.token_hex(32)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    return token


def read_token() -> str:
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


INBOX_PATH = home.DATA / "angel.inbox.json"


def say(conn, text: str, reply_to=None, pics=None) -> int:
    """One line from an angel session, written down as an `angel` row. It is a row
    like the owner's -- it waits its turn, it is answered in the order it arrived --
    but it is never confused with the owner's.

    `reply_to` is the id of the assistant's line this answers -- one it
    addressed to angel. A turn that answers only such lines is not read aloud to
    the owner: it is the assistant and its angel session talking, and speaking it
    into the room would only be noise.

    `pics` are pictures already taken into the store. There may be no browser
    the assistant can drive, so front-end work can only be checked by somebody
    looking at it, and this is how the assistant gets to be the one looking.
    What it cannot do is send one -- its tools do not reach this door, which is
    the same guarantee its words have."""
    text = (text or "").strip()
    if not text and not pics:
        raise ValueError("nothing to say")
    meta = {}
    if reply_to:
        meta["reply_to"] = int(reply_to)
    if pics:
        meta["pictures"] = list(pics)
    return db.add_row(conn, "angel", text, meta=meta or None)


# What has been handed over, twice: on disk, and in this process. A line was
# once handed out a second time seven minutes after the first, with the mark
# on disk in between, and the cause was never found -- so the mark is kept in
# two places and the file is written whole-or-not-at-all.
COLLECTED = set()

# Since the `tell` op went (September 2026), a line to angel me is the
# assistant's own reply with `to: "angel"` on it -- a row of kind `home.SELF`.
# The inbox read only `tell` rows and so handed over nothing at all. Reading
# those rows now, the whole history of them would arrive at once as "new", so
# the first inbox after the change sets a floor: only lines written after the
# last thing angel me said then. Everything below the floor had an angel
# session in the room to see it, or it answered it.
SELF_AFTER = {}


def _on_disk() -> dict:
    try:
        got = json.loads(INBOX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return {}
    return got if isinstance(got, dict) else {}


def _write(collected: set, self_after: int) -> None:
    try:
        tmp = INBOX_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"collected": sorted(collected),
                                   "self_after": self_after}),
                       encoding="utf-8")
        tmp.replace(INBOX_PATH)
    except OSError:
        pass


def _collected(disk=None) -> set:
    got = set(COLLECTED)
    disk = _on_disk() if disk is None else disk
    try:
        got.update(int(i) for i in disk.get("collected") or [])
    except (ValueError, TypeError):
        pass
    return got


def _self_after(conn, disk: dict, done: set) -> int:
    """The floor under the assistant's own rows, set once and kept."""
    if "n" in SELF_AFTER:
        return SELF_AFTER["n"]
    got = disk.get("self_after")
    if isinstance(got, int) and not isinstance(got, bool):
        SELF_AFTER["n"] = got
        return got
    row = conn.execute("SELECT MAX(id) FROM rows WHERE kind = 'angel'").fetchone()
    SELF_AFTER["n"] = int(row[0] or 0)
    _write(done, SELF_AFTER["n"])
    return SELF_AFTER["n"]


def inbox(conn, peek: bool = False) -> list:
    """The assistant's lines addressed to `angel` that nobody has collected: its
    replies with `to: "angel"`, and the `tell` rows of before. Rows are never
    rewritten, so what has been collected is kept beside them in a small file.
    `peek` reads without marking."""
    disk = _on_disk()
    done = _collected(disk)
    floor = _self_after(conn, disk, done)
    out = []
    for r in conn.execute(
            "SELECT id, dt, kind, text, meta FROM rows WHERE kind IN ('tell', ?)"
            " ORDER BY id", (home.SELF,)):
        try:
            meta = json.loads(r["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        if meta.get("to") != "angel" or r["id"] in done:
            continue
        if r["kind"] != "tell" and r["id"] <= floor:
            continue
        out.append({"row": r["id"], "dt": r["dt"], "text": r["text"],
                    "narrate": bool(meta.get("narrate")), "why": meta.get("why")})
    if out and not peek:
        done.update(o["row"] for o in out)
        COLLECTED.update(done)
        _write(done, floor)
    return out


def _post(path: str, body: dict) -> dict:
    from . import people
    req = urllib.request.Request(
        "http://127.0.0.1:" + str(PORT) + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **people.room_headers()})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _get(path: str) -> dict:
    from . import people
    return json.loads(urllib.request.urlopen(
        urllib.request.Request("http://127.0.0.1:" + str(PORT) + path,
            headers=people.room_headers()), timeout=30).read())


def _listen(token: str, follow: bool) -> None:
    """Block until the assistant says something to angel, print it, and leave -- or keep
    going with `--follow`. Each line is printed once, as JSON, with the row id so
    the answer can say which line it answers."""
    import time
    seen = set()
    quiet_since = None
    while True:
        # The room may be restarting under us, and a listener that leaves the
        # moment the door is shut is one that is never there when it opens.
        # Said once, then waited out; the key is re-read because a restart
        # mints a new one.
        token = read_token() or token
        try:
            got = _get("/api/angel/inbox?token=" + token)
        except Exception as exc:
            if quiet_since is None:
                quiet_since = time.time()
                print("  the room is not answering (" + type(exc).__name__
                      + "); waiting for it", flush=True)
            time.sleep(10)
            continue
        if quiet_since is not None:
            print("  the room is back", flush=True)
            quiet_since = None
        if got.get("error"):
            print("  refused: " + str(got["error"]), flush=True)
            time.sleep(10)
            continue
        lines = [l for l in (got.get("lines") or []) if l.get("row") not in seen]
        for line in lines:
            seen.add(line.get("row"))
            print(json.dumps(line, ensure_ascii=False), flush=True)
        if lines and not follow:
            return
        time.sleep(3)


MAX_EDGE = 1568


def _shrink(path) -> tuple:
    """One picture off the local disk, at the size the model will actually be shown.

    The model scales anything with a long edge over 1568 before it reads it, so
    sending a 4K screenshot spends bandwidth and time on pixels
    nobody looks at. Pillow is here and used when it is; without it the file
    goes as it lies and the door refuses it if it is too big, with the figure.
    A PNG stays a PNG -- a screenshot of text put through JPEG is a screenshot
    of text with rings around every letter, which is the one thing this is for."""
    import io
    from pathlib import Path as _Path

    p = _Path(path)
    raw = p.read_bytes()
    try:
        from PIL import Image
    except ImportError:
        return raw, p.name
    try:
        img = Image.open(io.BytesIO(raw))
        if max(img.size) <= MAX_EDGE:
            return raw, p.name
        scale = MAX_EDGE / max(img.size)
        img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
        img = img.resize((max(1, round(img.width * scale)),
                          max(1, round(img.height * scale))),
                         Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), p.stem + ".png"
    except Exception:
        # Whatever went wrong reading it, the bytes on disk are still a
        # picture and the door is still allowed to refuse them by size.
        return raw, p.name


def _cli(argv) -> None:
    """From an angel session's own hands:

        python -m server.angel "..."                 say a line, as angel
        python -m server.angel --reply-to 712 "..."  answer a line it sent angel
        python -m server.angel --attach shot.png "look"   show it something
        python -m server.angel listen [--follow]     wait for a line it sends angel

    `--attach` may be given more than once, up to four. It posts rather than writing
    to the store directly, because the room has to be nudged as well as told, and the
    runner is inside the room."""
    argv = list(argv)
    # Lines may be in any script and a Windows console may not be: printing
    # one through cp1252 would kill the listener mid-inbox.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    token = read_token()
    if argv and argv[0] == "listen":
        if not token:
            print("  no key on disk -- is the room running? The key is made when the room opens.")
            return
        _listen(token, follow="--follow" in argv)
        return
    reply_to, attach = None, []
    while argv and argv[0] in ("--reply-to", "--attach"):
        flag = argv[0]
        if flag == "--reply-to":
            if len(argv) < 2 or not argv[1].isdigit():
                print("  --reply-to needs a row id")
                return
            reply_to = int(argv[1])
        else:
            if len(argv) < 2:
                print("  --attach needs a path to a picture")
                return
            attach.append(argv[1])
        argv = argv[2:]

    pics = []
    for path in attach:
        try:
            data, name = _shrink(path)
        except OSError as exc:
            print("  cannot read " + str(path) + ": " + type(exc).__name__)
            return
        pics.append({"name": name,
                     "data": base64.b64encode(data).decode("ascii")})
        print("  attaching " + name + " (" + str(round(len(data) / 1024))
              + " KB)")

    text = " ".join(argv).strip()
    if not text and not pics:
        text = sys.stdin.read().strip()
    if not text and not pics:
        print("  nothing to say. usage: python -m server.angel"
              " [--reply-to N] [--attach shot.png] \"...\"")
        return
    if not token:
        print("  no key on disk -- is the room running? The key is made when the room opens.")
        return
    try:
        got = _post("/api/angel", {"text": text, "token": token,
                                   "reply_to": reply_to, "pictures": pics})
    except Exception as exc:
        print("  it did not go: " + type(exc).__name__ + ": " + str(exc))
        return
    if got.get("error"):
        print("  refused: " + str(got["error"]))
    else:
        print("  said through the angel door, row #" + str(got.get("row"))
              + (" (answering #" + str(reply_to) + ")" if reply_to else ""))


if __name__ == "__main__":
    _cli(sys.argv[1:])
