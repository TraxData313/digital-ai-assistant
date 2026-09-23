"""The room's background pictures: one small, named collection.

The page never gets a folder name or a path from a browser.  It asks for this
catalogue, which names a built-in default and the supported files we have put
under ``artwork/wallpapers/`` in the home.  A selection is only one of those names,
and lives in ``data/wallpaper.json`` so it survives a room restart without
turning a household preference into a code change.
"""

import base64
import json
import os
import re
import threading
import unicodedata
from pathlib import Path
from urllib.parse import quote

from . import pictures
from . import home


ROOT = Path(__file__).resolve().parent.parent
ARTWORK = home.ARTWORK
# New pictures belong together, rather than being mixed with the app's icon and
# other artwork.  The original painting stays where it was, as the default.
COLLECTION = ARTWORK / "wallpapers"
DEFAULT_FILE = home.artwork("wallpaper.png")
SETTINGS_PATH = home.DATA / "wallpaper.json"

DEFAULT_ID = "default"
MAX_BYTES = 10 * 1024 * 1024
MAX_EDGE = 8192
MAX_PIXELS = 32_000_000
# JSON carries base64, so stop an oversized request before decoding it into a
# second in-memory copy.  The small allowance is for its name and JSON shape.
MAX_REQUEST_BYTES = (MAX_BYTES * 4 // 3) + 16_384

_LOCK = threading.RLock()


class Refused(ValueError):
    """A wallpaper the room will not keep, with a reason fit for Settings."""


def _media(data: bytes):
    media = pictures.sniff(data)
    if media not in pictures.KINDS:
        return None
    return media


def _extension(media: str) -> str:
    return pictures.KINDS[media][0]


def _safe_filename(name, extension: str) -> str:
    """Reduce an upload label to one safe, human-readable file name.

    This is deliberately not a path normalizer.  Slashes, drive separators,
    controls and leading dots are discarded before the value ever meets the
    collection directory.  The actual extension comes from the bytes, never
    the uploaded label.
    """
    raw = str(name or "").replace("\\", "/").split("/")[-1]
    raw = unicodedata.normalize("NFKC", raw)
    stem = Path(raw).stem
    stem = re.sub(r"[\x00-\x1f\x7f]+", "", stem)
    stem = re.sub(r"[^\w. -]+", "-", stem, flags=re.UNICODE)
    stem = re.sub(r"\s+", " ", stem).strip(" .-_")[:72]
    return (stem or "room-wallpaper") + extension


def _collection_file(name: str) -> Path:
    """Return one collection member, refusing every non-member spelling."""
    if not isinstance(name, str) or not re.fullmatch(
            r"[\w][\w .-]{0,91}\.(?:png|jpg|gif|webp)", name,
            flags=re.IGNORECASE):
        raise Refused("that is not a wallpaper in " + home.NAME + "'s collection")
    return COLLECTION / name


def _entry(identifier: str, path: Path, *, built_in=False):
    try:
        data = path.read_bytes()
    except OSError:
        return None
    media = _media(data)
    size = pictures.size_of(data, media) if media else None
    if not media or not size:
        return None
    w, h = size
    if w > MAX_EDGE or h > MAX_EDGE or w * h > MAX_PIXELS:
        return None
    url = ("/artwork/wallpaper.png" if built_in else
           "/artwork/wallpapers/" + quote(path.name))
    return {"id": identifier, "name": home.WALLPAPER_NAME if built_in else path.stem,
            "file": path.name, "url": url, "media_type": media,
            "bytes": len(data), "width": w, "height": h,
            "built_in": built_in}


def _items() -> list:
    out = []
    default = _entry(DEFAULT_ID, DEFAULT_FILE, built_in=True)
    if default:
        out.append(default)
    try:
        files = sorted((p for p in COLLECTION.iterdir() if p.is_file()),
                       key=lambda p: p.name.casefold())
    except OSError:
        files = []
    for path in files:
        try:
            _collection_file(path.name)
        except Refused:
            continue
        entry = _entry(path.name, path)
        if entry:
            out.append(entry)
    return out


def _saved_id() -> str:
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return DEFAULT_ID
    selected = value.get("selected") if isinstance(value, dict) else None
    return selected if isinstance(selected, str) else DEFAULT_ID


def status() -> dict:
    """The only wallpaper shape sent to the browser."""
    with _LOCK:
        items = _items()
        by_id = {item["id"]: item for item in items}
        selected = _saved_id()
        # A removed file must not leave the room with a broken background.
        # The original painting is the durable fallback whenever it is here.
        if selected not in by_id:
            selected = DEFAULT_ID if DEFAULT_ID in by_id else (items[0]["id"] if items else None)
        return {"items": items, "selected": selected,
                "current": by_id.get(selected),
                "max_bytes": MAX_BYTES,
                "supported": ["PNG", "JPEG", "GIF", "WEBP"]}


def _write_selected(identifier: str):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"selected": identifier}, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_PATH)


def choose(identifier) -> dict:
    """Persist a choice only after proving it is in the server catalogue."""
    with _LOCK:
        identifier = str(identifier or "")
        if identifier not in {item["id"] for item in _items()}:
            raise Refused("choose one of the wallpapers shown here")
        _write_selected(identifier)
        return status()


def _decode(data) -> bytes:
    if not isinstance(data, str):
        raise Refused("the wallpaper did not arrive as image data")
    # The browser's FileReader gives a data URL; accepting bare base64 also
    # keeps this endpoint simple to exercise without a browser.
    raw = data.split(",", 1)[-1] if data.startswith("data:") else data
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as exc:
        raise Refused("the wallpaper did not arrive whole") from exc


def _validate(data: bytes, name) -> tuple[str, tuple[int, int]]:
    label = _safe_filename(name, "") or "that file"
    if not data:
        raise Refused("the wallpaper is empty")
    if len(data) > MAX_BYTES:
        raise Refused(label + " is " + str(round(len(data) / 1024 / 1024, 1))
                      + " MB; wallpapers may be at most "
                      + str(MAX_BYTES // 1024 // 1024) + " MB")
    media = _media(data)
    if not media:
        raise Refused(label + " is not a supported image — PNG, JPEG, GIF and WEBP are allowed")
    size = pictures.size_of(data, media)
    if not size:
        raise Refused(label + " has no readable image dimensions")
    w, h = size
    if w > MAX_EDGE or h > MAX_EDGE or w * h > MAX_PIXELS:
        raise Refused(label + " is too large in pixels; the limit is "
                      + str(MAX_EDGE) + " px on either edge and "
                      + str(MAX_PIXELS // 1_000_000) + " megapixels")
    return media, size


def _available_name(wanted: str) -> str:
    """Find a case-insensitive unused name, preserving every existing image."""
    try:
        occupied = {p.name.casefold() for p in COLLECTION.iterdir() if p.is_file()}
    except OSError:
        occupied = set()
    if wanted.casefold() not in occupied:
        return wanted
    path = Path(wanted)
    for number in range(2, 10_000):
        candidate = path.stem + "-" + str(number) + path.suffix
        if candidate.casefold() not in occupied:
            return candidate
    raise Refused("too many wallpapers already have that name")


def upload(name, data) -> dict:
    """Validate, store without overwriting, and immediately choose an upload."""
    raw = _decode(data)
    media, _ = _validate(raw, name)
    wanted = _safe_filename(name, _extension(media))
    with _LOCK:
        COLLECTION.mkdir(parents=True, exist_ok=True)
        candidate = _available_name(wanted)
        # Exclusive creation closes the gap between the directory scan and the
        # write if two browser tabs happen to upload at once.
        while True:
            path = COLLECTION / candidate
            try:
                with path.open("xb") as handle:
                    handle.write(raw)
                break
            except FileExistsError:
                candidate = _available_name(candidate)
            except OSError as exc:
                raise Refused("the wallpaper could not be saved: " + str(exc)) from exc
        _write_selected(candidate)
        return status()
