"""The face: pictures in, the home's own artwork out, the original kept whole.

    python -m server.test_portraits C:\\somewhere\\scratch

Points the portrait module at artwork folders inside the scratch; no home's
own face is touched. Then walks the two routes through a bench room on a port
the operating system picks.
"""

import base64
import io
import json
import shutil
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from . import app, home, people, portraits

FAILED = []


def check(name, ok, detail=""):
    mark = "ok  " if ok else "FAIL"
    print("  " + mark + "  " + name + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def picture(w, h, color, kind="PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, kind)
    return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def point_at(art: Path, scratch: Path):
    portraits.ARTWORK = art
    portraits.COLLECTION = art / "portraits"
    portraits.ORIGINAL = art / "portraits" / "original"
    portraits.SETTINGS_PATH = scratch / "data" / "portrait.json"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    if scratch == home.CODE or home.CODE in scratch.parents:
        print("that scratch is inside the repo. Give me one outside it.")
        sys.exit(2)
    shutil.rmtree(scratch, ignore_errors=True)
    art = scratch / "artwork"
    art.mkdir(parents=True)

    # -- a home with a hand-made face of its own ---------------------------
    hand = picture(300, 300, (200, 40, 40))
    (art / "portrait.png").write_bytes(hand)
    Image.new("RGBA", (64, 64), (200, 40, 40, 255)).save(art / "icon.ico", sizes=[(32, 32)])
    hand_ico = (art / "icon.ico").read_bytes()
    point_at(art, scratch)

    st = portraits.status()
    check("before any change the original is what is worn",
          st["selected"] == "original" and st["items"][0]["id"] == "original", st)

    got = portraits.upload("My Face.png", b64(picture(800, 600, (20, 90, 200))))
    chosen = got["selected"]
    check("an upload is worn at once", chosen != "original" and chosen.endswith(".png"), got)
    check("and kept in the collection", (art / "portraits" / chosen).is_file())
    check("the hand-made face is kept whole, byte for byte",
          (art / "portraits" / "original" / "portrait.png").read_bytes() == hand
          and (art / "portraits" / "original" / "icon.ico").read_bytes() == hand_ico)
    check("and only its own files are kept as the original",
          json.loads((art / "portraits" / "original" / "kept.json").read_text(encoding="utf-8"))["own"]
          == ["portrait.png", "icon.ico"])

    with Image.open(art / "portrait.png") as im:
        check("the portrait is 512 square, with room for the circle", im.size == (512, 512) and im.mode == "RGBA")
        check("its corners are clear", im.getpixel((2, 2))[3] == 0)
        r, g, b, a = im.getpixel((256, 256))
        check("its middle is the picture", a == 255 and b > 150 and r < 80, (r, g, b, a))
        r, g, b, a = im.getpixel((256, 4))
        check("and the ring is round it", a > 0 and g > 150, (r, g, b, a))
    with Image.open(art / "icon.ico") as ico:
        check("the corner icon carries every size up to 256",
              (256, 256) in ico.info.get("sizes", set()) and (16, 16) in ico.info.get("sizes", set()),
              ico.info.get("sizes"))
    with Image.open(art / "icon-down.ico") as down:
        down.size = (256, 256)
        px = down.convert("RGBA").getpixel((128, 128))
        check("the put-down icon is the same face in grey", px[0] == px[1] == px[2] and px[3] == 255, px)
    with Image.open(art / "icon-maskable-512.png") as mask:
        check("the maskable icon fills its square", mask.size == (512, 512) and mask.mode == "RGB")
    for name in ("icon-192.png", "icon-512.png"):
        check(name + " is written", (art / name).is_file())
    check("the choice is remembered",
          json.loads(portraits.SETTINGS_PATH.read_text(encoding="utf-8"))["selected"] == chosen)

    got = portraits.upload("second.jpg", b64(picture(400, 900, (30, 160, 60), "JPEG")))
    check("a JPEG is taken too", got["selected"].lower().endswith(".jpg"), got["selected"])
    check("the collection lists both, and the original first",
          [i["id"] for i in got["items"]][0] == "original" and len(got["items"]) == 3, got["items"])
    check("the original is still the first face, not the first upload",
          (art / "portraits" / "original" / "portrait.png").read_bytes() == hand)

    portraits.choose("original")
    check("choosing the original gives back exactly the hand-made files",
          (art / "portrait.png").read_bytes() == hand and (art / "icon.ico").read_bytes() == hand_ico)
    check("and takes away the ones it never had",
          not (art / "icon-192.png").exists() and not (art / "icon-down.ico").exists())
    got = portraits.choose(chosen)
    check("an earlier upload can be worn again", got["selected"] == chosen)

    for label, data, name in (("garbage", b64(b"not a picture"), "x.png"),
                              ("an SVG", b64(b"<svg xmlns='http://www.w3.org/2000/svg'/>"), "x.svg"),
                              ("nothing", "", "x.png")):
        try:
            portraits.upload(name, data)
            check(label + " is refused", False)
        except portraits.Refused as exc:
            check(label + " is refused, in words", "portrait" in str(exc) or "supported" in str(exc), exc)
    try:
        portraits.choose("../../identity.json")
        check("a path is not a choice", False)
    except portraits.Refused:
        check("a path is not a choice", True)

    # -- a home with no face of its own: the code's default is the original --
    art2 = scratch / "bare" / "artwork"
    art2.mkdir(parents=True)
    point_at(art2, scratch / "bare")
    portraits.upload("face.png", b64(picture(256, 256, (240, 200, 20))))
    default = home.CODE / "artwork" / "portrait.png"
    check("a bare home shows the code's default face as its original",
          (art2 / "portraits" / "original" / "preview.png").read_bytes() == default.read_bytes()
          and portraits.status()["items"][0]["url"].startswith("/artwork/portraits/original/preview.png"))
    check("while the new face is its own", all((art2 / n).is_file() for n in portraits.FACE))
    portraits.choose("original")
    check("choosing the original takes every drawn file away again, so the defaults show",
          not [n for n in portraits.FACE if (art2 / n).exists()],
          [n for n in portraits.FACE if (art2 / n).exists()])

    # -- the routes ---------------------------------------------------------
    point_at(art, scratch)
    app.home.ARTWORK = art
    people.TOKEN_DIR = scratch / "people"
    token = people.mint(home.OWNER)
    jar = home.COOKIE + "=" + token
    room = app.OneRoom(("127.0.0.1", 0), app.Handler)
    base = "http://127.0.0.1:" + str(room.server_port)
    threading.Thread(target=room.serve_forever, daemon=True).start()

    def ask(path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=data, headers={"Cookie": jar})
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    try:
        code, _, raw = ask("/api/portraits")
        st = json.loads(raw) if code == 200 else {}
        check("the room lists the portraits", code == 200 and st.get("selected") == chosen, (code, raw[:120]))
        code, _, raw = ask("/api/portraits/upload", {"name": "third face.png", "data": b64(picture(300, 300, (90, 20, 120)))})
        st = json.loads(raw) if code == 200 else {}
        check("an upload through the room is worn", code == 200 and st.get("selected") == "third face.png", (code, raw[:160]))
        code, ctype, raw = ask(st.get("current", {}).get("url", "/nowhere"))
        check("and the new face is served from the home's artwork", code == 200 and ctype.startswith("image/png"), (code, ctype))
        item = next((i for i in st.get("items", []) if i["id"] == "third face.png"), {})
        code, ctype, raw = ask(item.get("url", "/nowhere"))
        check("a picture with a space in its name is served by its address",
              code == 200 and ctype.startswith("image/png") and "%20" in item.get("url", ""), (code, item.get("url")))
        code, _, raw = ask("/api/portraits/choose", {"id": "not-there.png"})
        check("a choice that is not there is refused in words", code == 400 and b"choose one" in raw, (code, raw[:120]))
    finally:
        room.shutdown()

    print()
    if FAILED:
        print(str(len(FAILED)) + " failed")
        sys.exit(1)
    print("every face held")


if __name__ == "__main__":
    main()
