"""The assistant's face: which picture it wears, kept in its own artwork.

A picture uploaded or chosen here is kept in `artwork/portraits/`, and the
face is drawn from it: the round portrait at the top of the page, the icons a
phone keeps on its home screen, and the two icons in the corner of the screen
(awake, and put down). All of it is written into the home's own `artwork/`,
never beside the code.

The face the home wore before the first change is kept whole in
`artwork/portraits/original/`, file for file, with a note of which files were
its own, so choosing it again gives back exactly that: its own pictures put
back, and any it never had taken away again so the defaults beside the code
show through -- a hand-made icon is not redrawn from a crop of itself. The
icon in the corner is read when it starts, so a new face shows there the next
time it does.
"""

import io
import json
import os
import shutil
import threading
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageOps

from . import home, wallpapers

ARTWORK = home.ARTWORK
COLLECTION = ARTWORK / "portraits"
ORIGINAL = COLLECTION / "original"
SETTINGS_PATH = home.DATA / "portrait.json"
ORIGINAL_ID = "original"

# Everything that is the face. The page asks for the first four at fixed
# names; the tray reads the two .ico files.
FACE = ("portrait.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png",
        "icon.ico", "icon-down.ico")
MAX_BYTES = wallpapers.MAX_BYTES
MAX_REQUEST_BYTES = wallpapers.MAX_REQUEST_BYTES
# The ring the page's glow is drawn around: a thin band, the room's own teal.
RING = (125, 223, 180, 255)
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

_LOCK = threading.RLock()


class Refused(ValueError):
    """A portrait the room will not use, with a reason fit for the page."""


def _url(path: Path) -> str:
    rel = path.resolve().relative_to(ARTWORK.resolve()).as_posix()
    try:
        stamp = str(int(path.stat().st_mtime))
    except OSError:
        stamp = "0"
    return "/artwork/" + quote(rel) + "?v=" + stamp


def _selected() -> str:
    try:
        got = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("selected")
        return got if isinstance(got, str) else ORIGINAL_ID
    except (OSError, ValueError, AttributeError):
        return ORIGINAL_ID


def _pictures() -> list:
    try:
        files = [p for p in COLLECTION.iterdir()
                 if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")]
    except OSError:
        files = []
    return sorted(files, key=lambda p: p.name.casefold())


def _current_face() -> Path:
    own = ARTWORK / "portrait.png"
    return own if own.exists() else home.CODE / "artwork" / "portrait.png"


def status() -> dict:
    items = []
    original = next((p for p in (ORIGINAL / "portrait.png", ORIGINAL / "preview.png") if p.exists()), None)
    items.append({"id": ORIGINAL_ID, "name": "the original face",
                  # Before the first change the original is simply what is
                  # worn, wherever it is served from.
                  "url": _url(original) if original else "/artwork/portrait.png"})
    for p in _pictures():
        items.append({"id": p.name, "name": p.stem, "url": _url(p)})
    face = _current_face()
    current = (_url(face) if ARTWORK.resolve() in face.resolve().parents
               else "/artwork/portrait.png")
    return {"selected": _selected(), "current": {"url": current},
            "items": items, "max_bytes": MAX_BYTES}


def _kept() -> list:
    try:
        got = json.loads((ORIGINAL / "kept.json").read_text(encoding="utf-8")).get("own")
        return [n for n in got if n in FACE] if isinstance(got, list) else None
    except (OSError, ValueError, AttributeError):
        return None


def _keep_original() -> None:
    """Before the first change: copy the home's own face files as they are,
    and note which they were, so the original can be given back exactly."""
    if _kept() is not None:
        return
    ORIGINAL.mkdir(parents=True, exist_ok=True)
    own = []
    for name in FACE:
        if (ARTWORK / name).is_file():
            shutil.copy2(ARTWORK / name, ORIGINAL / name)
            own.append(name)
    if "portrait.png" not in own:
        # Worn from the code's defaults: keep a picture of it to choose by.
        default = home.CODE / "artwork" / "portrait.png"
        if default.is_file():
            shutil.copy2(default, ORIGINAL / "preview.png")
    (ORIGINAL / "kept.json").write_text(json.dumps({"own": own}) + "\n", encoding="utf-8")


def _square(picture: Path) -> Image.Image:
    with Image.open(picture) as im:
        im.seek(0)                      # a GIF wears its first frame
        im = ImageOps.exif_transpose(im).convert("RGBA")
    w, h = im.size
    side = min(w, h)
    left = (w - side) // 2
    # A portrait's face sits high in the frame; a crop from the middle cuts
    # off foreheads. A quarter of the spare height from the top.
    top = (h - side) // 4
    return im.crop((left, top, left + side, top + side))


def _round(square: Image.Image, size: int) -> Image.Image:
    """The picture in a circle with the ring round it, drawn large and
    shrunk, so the edge is smooth."""
    big = size * 4
    ring = max(4, big // 40)
    out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    inner = square.resize((big - 2 * ring, big - 2 * ring), Image.LANCZOS)
    mask = Image.new("L", inner.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, inner.size[0] - 1, inner.size[1] - 1), fill=255)
    out.paste(inner, (ring, ring), mask)
    ImageDraw.Draw(out).ellipse((ring // 2, ring // 2, big - 1 - ring // 2, big - 1 - ring // 2),
                                outline=RING, width=ring)
    return out.resize((size, size), Image.LANCZOS)


def _down(face: Image.Image) -> Image.Image:
    """The put-down icon: the same face, grey and dimmer."""
    grey = ImageOps.grayscale(face.convert("RGB")).point(lambda v: int(v * 0.6))
    out = Image.merge("RGBA", (grey, grey, grey, face.getchannel("A")))
    return out


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _ico(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "ICO", sizes=ICO_SIZES)
    return buf.getvalue()


def _draw(picture: Path) -> dict:
    square = _square(picture)
    face = _round(square, 512)
    icon = _round(square, 256)
    return {
        "portrait.png": _png(face),
        "icon-192.png": _png(face.resize((192, 192), Image.LANCZOS)),
        "icon-512.png": _png(face),
        # Maskable: the phone cuts its own shape, so the picture fills the
        # square edge to edge, no ring and no transparency.
        "icon-maskable-512.png": _png(square.resize((512, 512), Image.LANCZOS).convert("RGB")),
        "icon.ico": _ico(icon),
        "icon-down.ico": _ico(_down(icon)),
    }


def _write(files: dict) -> None:
    ARTWORK.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        tmp = ARTWORK / (name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, ARTWORK / name)


def _save_selected(identifier: str) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"selected": identifier}) + "\n", encoding="utf-8")
    os.replace(tmp, SETTINGS_PATH)


def choose(identifier) -> dict:
    with _LOCK:
        if identifier == ORIGINAL_ID:
            own = _kept()
            if own is not None:
                _write({name: (ORIGINAL / name).read_bytes() for name in own})
                for name in FACE:
                    if name not in own:
                        (ARTWORK / name).unlink(missing_ok=True)
            _save_selected(ORIGINAL_ID)
            return status()
        picture = next((p for p in _pictures() if p.name == identifier), None)
        if picture is None:
            raise Refused("choose one of the pictures shown here")
        try:
            files = _draw(picture)
        except (OSError, ValueError) as exc:
            raise Refused(picture.name + " could not be drawn as a face: " + str(exc)) from exc
        _keep_original()
        _write(files)
        _save_selected(picture.name)
        return status()


def upload(name, data) -> dict:
    """Keep an uploaded picture in the collection and wear it at once."""
    try:
        raw = wallpapers._decode(data, "portrait")
        media, _ = wallpapers._validate(raw, name, "portrait")
    except wallpapers.Refused as exc:
        raise Refused(str(exc)) from exc
    wanted = wallpapers._safe_filename(name, wallpapers._extension(media))
    with _LOCK:
        COLLECTION.mkdir(parents=True, exist_ok=True)
        occupied = {p.name.casefold() for p in COLLECTION.iterdir() if p.is_file()}
        candidate, n = wanted, 2
        while candidate.casefold() in occupied:
            candidate = Path(wanted).stem + "-" + str(n) + Path(wanted).suffix
            n += 1
        try:
            with (COLLECTION / candidate).open("xb") as handle:
                handle.write(raw)
        except OSError as exc:
            raise Refused("the portrait could not be saved: " + str(exc)) from exc
        return choose(candidate)
