"""Lean on the room-background picker over a scratch artwork folder:

    python -m server.test_wallpapers C:\\somewhere\\scratch

It exercises the catalogue and its HTTP door without touching the live
artwork, the running room's data, or a real conversation store.
"""

import base64
import json
import shutil
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from . import app, people, wallpapers
from .test_pictures import a_png


BASE = ""
FAILED = []


def check(name, ok, detail=""):
    print("  " + ("ok  " if ok else "FAIL") + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def request(path, body=None, token=""):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Cookie": "ada_who=" + token} if token else {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    # The room's picture addresses are relative to the page, at its root here.
    req = urllib.request.Request(BASE + "/" + path.lstrip("/"), data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return res.status, res.read(), res.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def body_of(code, raw):
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"error": "not JSON: " + str(code)}


def main():
    global BASE
    if len(sys.argv) < 2:
        print("give me a scratch folder; I will not write into a home's artwork")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    repo = Path(__file__).resolve().parent.parent
    if scratch == repo or repo in scratch.parents:
        print("that scratch is inside the repo. Give me one outside it.")
        sys.exit(2)
    shutil.rmtree(scratch, ignore_errors=True)
    artwork = scratch / "artwork"
    artwork.mkdir(parents=True)
    default = artwork / "wallpaper.png"
    default.write_bytes(a_png(240, 160, (60, 130, 90)))

    wallpapers.ARTWORK = artwork
    wallpapers.COLLECTION = artwork / "wallpapers"
    # The room serves pictures from the home first; the scratch is the home.
    app.home.ARTWORK = artwork
    wallpapers.DEFAULT_FILE = default
    wallpapers.SETTINGS_PATH = scratch / "data" / "wallpaper.json"
    app.ROOT = scratch
    people.TOKEN_DIR = scratch / "people"
    token = people.mint("sam")

    # An ephemeral port keeps the bench separate from Ada's running room and
    # from any other focused test someone may be running beside it.
    room = app.OneRoom(("127.0.0.1", 0), app.Handler)
    BASE = "http://127.0.0.1:" + str(room.server_port)
    threading.Thread(target=room.serve_forever, daemon=True).start()
    try:
        code, raw, _ = request("/api/wallpapers", token=token)
        initial = body_of(code, raw)
        check("the original garden remains the default", code == 200
              and initial.get("selected") == "default"
              and initial.get("current", {}).get("url") == "artwork/wallpaper.png", initial)

        png = a_png(320, 180, (100, 90, 180))
        code, raw, _ = request("/api/wallpapers/upload", {
            "name": "..\\My sunny / room?.png",
            "data": base64.b64encode(png).decode("ascii")}, token)
        uploaded = body_of(code, raw)
        selected = uploaded.get("selected")
        current = uploaded.get("current") or {}
        check("an upload is saved and selected", code == 200 and selected
              and selected != "default" and current.get("id") == selected, uploaded)
        check("the filename is made safe inside only the collection",
              wallpapers.COLLECTION.is_dir()
              and len(list(wallpapers.COLLECTION.iterdir())) == 1
              and not (scratch / "room.png").exists(), list(wallpapers.COLLECTION.iterdir()))
        check("the selected image is served through its generated URL",
              request(current.get("url", ""), token=token)[1] == png, current)

        # Same friendly name is not a request to replace an older picture.
        code, raw, _ = request("/api/wallpapers/upload", {
            "name": "room.png", "data": base64.b64encode(a_png(80, 60)).decode("ascii")}, token)
        second = body_of(code, raw)
        names = [p.name for p in wallpapers.COLLECTION.iterdir()]
        check("a duplicate name receives a new file rather than overwriting",
              code == 200 and len(names) == 2 and len(set(names)) == 2, names)

        code, raw, _ = request("/api/wallpapers/choose", {"id": "default"}, token)
        chosen = body_of(code, raw)
        check("the default can be chosen again", code == 200
              and chosen.get("selected") == "default", chosen)
        saved = json.loads(wallpapers.SETTINGS_PATH.read_text(encoding="utf-8"))
        check("the selection is persisted for the next server", saved.get("selected") == "default"
              and wallpapers.status().get("selected") == "default", saved)

        code, raw, _ = request("/api/wallpapers/choose", {"id": "../../data/store.db"}, token)
        check("a path is never a selectable wallpaper", code == 400
              and b"shown here" in raw, raw)
        code, raw, _ = request("/api/wallpapers/upload", {
            "name": "not-a-picture.png",
            "data": base64.b64encode(b"not a picture").decode("ascii")}, token)
        check("bytes that only claim to be a picture are refused", code == 400
              and b"not a supported image" in raw, raw)
        check("an oversized image is refused before it can be stored",
              _refused_big(), "limit " + str(wallpapers.MAX_BYTES))
        code, _, _ = request("/artwork/wallpapers/..%2F..%2Fdata%2Fwallpaper.json", token=token)
        check("static artwork cannot traverse out of the repository root", code == 404, code)
    finally:
        room.shutdown()

    print()
    if FAILED:
        print("  " + str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print("  every wallpaper rule held.")


def _refused_big():
    try:
        wallpapers._validate(b"x" * (wallpapers.MAX_BYTES + 1), "huge.png")
    except wallpapers.Refused:
        return True
    return False


if __name__ == "__main__":
    main()
