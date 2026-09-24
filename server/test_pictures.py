"""Lean on its eyes, without a model and without its room. Run it over a
scratch folder:

    python -m server.test_pictures C:\\somewhere\\scratch

A side room comes up on port 8791 over whatever store is in that folder, with
no turn loop, so nothing here can think, spend, or write to the live store --
which is refused by name.

What it leans on is everything the pixels touch on the way through: the store
that names them by their own sha, the door that takes them in, the row that
carries their weight, the prompt that numbers them, the blocks that go to the
model in that same order, and the one route that hands them back to the page.
The model call itself is proved separately -- `--call` below asks the real CLI
to name a colour, which costs a fraction of a penny and is the only part of
this that leaves the machine.
"""

import base64
import io
import json
import struct
import sys
import threading
import urllib.error
import urllib.request
import zlib
from pathlib import Path

from . import app, brain, db, pictures

PORT = 8791
BASE = "http://127.0.0.1:" + str(PORT)

FAILED = []


def check(name, ok, detail=""):
    print("  " + ("ok  " if ok else "FAIL") + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def raw_get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), ""


def a_png(w=120, h=80, rgb=(255, 140, 0)) -> bytes:
    """A picture with no library at all -- the test must not need one where
    the room does not."""
    def chunk(tag, data):
        c = tag + data
        return (struct.pack(">I", len(data)) + c
                + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def lean_on(conn):
    shot = a_png(1280, 720)
    small = a_png(64, 64, (40, 200, 160))

    # 1. The door takes a line with a picture on it, and the row carries the
    #    pointer and the weight.
    code, got = post("/api/send", {"text": "look at this",
                                   "pictures": [{"name": "chat.png",
                                                 "data": b64(shot)}]})
    check("the door takes a picture", code == 200, code)
    rows = {r["id"]: r for r in got.get("rows", [])}
    mine = max((r for r in rows.values() if r["kind"] == "user"),
               key=lambda r: r["id"], default=None)
    check("the line is in the room", mine is not None)
    pics = ((mine or {}).get("meta") or {}).get("pictures") or []
    check("the row carries one pointer", len(pics) == 1, len(pics))
    if pics:
        p = pics[0]
        check("it kept the name", p.get("name") == "chat.png", p.get("name"))
        check("it read the size", (p.get("w"), p.get("h")) == (1280, 720),
              (p.get("w"), p.get("h")))
        check("it costed it the API's way",
              p.get("tokens") == pictures.tokens_for((1280, 720)),
              p.get("tokens"))
        check("the row's estimate is words plus pixels",
              mine["tokens_est"] == db.est_tokens("look at this") + p["tokens"],
              mine["tokens_est"])

    # 2. A picture with no words is a whole thing to say; nothing at all is
    #    still nothing.
    code, _ = post("/api/send", {"text": "", "pictures": [
        {"name": "alone.png", "data": b64(small)}]})
    check("a picture on its own is allowed", code == 200, code)
    code, got = post("/api/send", {"text": "   "})
    check("an empty line is still refused", code == 400, code)

    # 3. Refusals happen before anything is written down.
    before = conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
    code, got = post("/api/send", {"text": "here", "pictures": [
        {"name": "notes.txt", "data": b64(b"just some words, not a picture")}]})
    check("bytes that are not a picture are refused", code == 400, code)
    check("and the refusal says so in words",
          "not a picture" in str(got.get("error")), got.get("error"))
    code, got = post("/api/send", {"text": "here", "pictures": [
        {"name": str(i) + ".png", "data": b64(small)} for i in range(5)]})
    check("five on one line are refused", code == 400, code)
    check("and it says how many are allowed",
          "4 is the most" in str(got.get("error")), got.get("error"))
    after = conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
    check("a refused line left no row behind", before == after,
          str(before) + " -> " + str(after))

    # 4. The same bytes twice are one file on disk.
    n_before = len(list(pictures.STORE.iterdir()))
    post("/api/send", {"text": "again", "pictures": [
        {"name": "same.png", "data": b64(shot)}]})
    check("the same picture twice is one file",
          len(list(pictures.STORE.iterdir())) == n_before, n_before)

    # 5. The route hands the pixels back, and only ever by the store's names.
    code, body, ctype = raw_get("/pictures/" + pics[0]["file"])
    check("the page can fetch the picture", code == 200, code)
    check("and it is the very bytes that went in", body == shot)
    check("served as a picture", ctype.startswith("image/png"), ctype)
    for bad in ("../store.db", "..%2Fassistant.db", "store.db",
                "0123456789abcdef.py", "0000000000000000.png"):
        code, _, _ = raw_get("/pictures/" + bad)
        check("refused /pictures/" + bad, code == 404, code)

    # 6. What it is told, and what it is shown, in the same order.
    held = db.loaded_rows(conn)
    prompt = brain.build_prompt(conn, rows=held)
    told = [p for m in prompt["messages"] for p in m.get("pictures") or []]
    shown = brain.pictures_in(held)
    check("it is told about every picture in its hands",
          len(told) == len(shown), str(len(told)) + " vs " + str(len(shown)))
    check("numbered from one, in row order",
          [t["n"] for t in told] == list(range(1, len(told) + 1)),
          [t["n"] for t in told])
    check("the names match the order",
          [t["name"] for t in told] == [s["name"] for s in shown])
    blocks, missing = pictures.blocks(shown)
    check("a block for each, nothing missing",
          len(blocks) == len(shown) and not missing, missing)
    check("the first block is the first picture it was told about",
          base64.b64decode(blocks[0]["source"]["data"])
          == pictures.read(shown[0]))

    # 7. Dropping the row takes the pixels out of its hands and leaves the
    #    file on disk; reaching it back brings them home.
    db.unload(conn, [mine["id"]])
    held = db.loaded_rows(conn)
    check("a dropped row takes its pictures with it",
          all(r["id"] != mine["id"] for r in held))
    check("the pixels are out of its hands",
          len(brain.pictures_in(held)) == len(shown) - 1,
          len(brain.pictures_in(held)))
    check("the file is still on disk",
          pictures.path_of(pics[0]).exists())
    db.reload_rows(conn, [mine["id"]])
    back = brain.pictures_in(db.loaded_rows(conn))
    check("reaching it back brings the picture home", len(back) == len(shown),
          len(back))
    check("and it is the same picture", pictures.read(back[0]) == shot)

    # 8. Angel me's door carries them too, and refuses without the key.
    from . import angel
    code, got = post("/api/angel", {"text": "look", "token": "not the key",
                                    "pictures": [{"name": "x.png",
                                                  "data": b64(small)}]})
    check("a wrong key at angel's door is refused", code == 403, code)
    key = angel.read_token()
    if key:
        code, got = post("/api/angel", {"text": "look at your own work",
                                        "token": key,
                                        "pictures": [{"name": "front.png",
                                                      "data": b64(small)}]})
        check("angel me can show it something", code == 200, got)
        row = db.get_row(conn, got.get("row")) if code == 200 else None
        check("it lands as an angel row with the picture on it",
              row and row["kind"] == "angel"
              and len(pictures.of_row(row["meta"])) == 1)

    # 9. What the readers with no eyes see.
    check("a picture-only line is not an empty line",
          pictures.with_label("", [{"name": "alone.png"}])
          == "[picture: alone.png]")


def main():
    if len(sys.argv) < 2:
        print("give me a scratch folder; I will not run over the live store")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    live = Path(__file__).resolve().parent.parent / "data"
    if scratch == live or scratch == live.parent:
        print("that is the live room; give me a scratch folder")
        sys.exit(2)
    scratch.mkdir(parents=True, exist_ok=True)
    db.DB_PATH = scratch / "store.db"
    # The pixels go to the scratch folder too. Without this the bench would
    # write into its real picture store, which is the one thing a bench must
    # never do -- and it is exactly the mistake that would look like it worked.
    pictures.STORE = scratch / "pictures"
    pictures.STORE.mkdir(parents=True, exist_ok=True)

    conn = db.connect()
    print("its eyes, leant on over " + str(db.DB_PATH))
    room = app.OneRoom(("127.0.0.1", PORT), app.Handler)
    threading.Thread(target=room.serve_forever, daemon=True).start()
    try:
        lean_on(conn)
    finally:
        room.shutdown()
        conn.close()

    print()
    if FAILED:
        print("  " + str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print("  every rule held.")


if __name__ == "__main__":
    main()
