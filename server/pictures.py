"""Pixels: what they weigh, where they live, and how they reach the assistant.

Without a browser the assistant can drive, a person's eye is the only render
the room has: a page that is broken for an hour stays broken for that hour,
because the assistant cannot look at it. Pictures are how it gets to look.

Three rules are the shape of this file.

**Pictures ride rows.** A picture is not a thing of its own with its own life
and its own timer -- it belongs to the line it arrived on. Dropping the row
drops the pixels; reaching the row back brings them home. A fade was the
alternative, and a fade is a decision that unmakes itself with nothing said.
So there is no timer here, and no sweeper. What is on disk stays on
disk, exactly as the store promises about words.

**The weight is said out loud.** A held picture is re-sent every turn it stays
loaded, so it is not a one-off cost -- it is rent. `tokens_for` is the API's own
arithmetic, width times height over 750, and it goes into the row's own
`tokens_est`, so the budget sums keep telling the truth and the assistant can
see what a picture costs it without being told.

**They come through the two locked doors and nowhere else.** The people's
room, and the angel door. Nothing here is reachable from a worker, from `web`,
or from `files`: a picture is a surface a stranger's text arrives on, and the
guarantee is the absence of the path, not a rule about how to use it.

The names on disk are the sha of what is in them, so the same screenshot sent
twice is one file, and a file can never be the wrong picture for its name.
"""

import base64
import hashlib
import math
import re
import struct
from pathlib import Path
from . import home

ROOT = Path(__file__).resolve().parent.parent
STORE = home.DATA / "pictures"

# What the room takes in one message. Four is not a technical limit -- it is
# how many pictures anybody sends at once about one thing, and a cap that is
# never reached is a cap nobody has to think about. Limits like this are set
# generous and raised in code when they pinch, not turned into a setting.
MAX_PER_MESSAGE = 4

# Per picture, arriving. The page shrinks a screenshot to about a tenth of
# this before it is ever sent, so anything near the ceiling is a photograph
# straight off a phone -- allowed, and worth refusing above.
MAX_BYTES = 8 * 1024 * 1024

# What Anthropic's own vision does to a picture before charging for it: a long
# edge over 1568, or an area over about 1.15 megapixels, is scaled down. We do
# the same arithmetic here rather than guessing, so the figure the assistant is
# handed is the figure that gets billed.
MAX_EDGE = 1568
MAX_AREA = 1_150_000
PIXELS_PER_TOKEN = 750

# Magic bytes, because a name is not evidence. Anything not recognised here is
# refused: an "image" the model cannot read is bytes that would cost a turn to
# find out about.
KINDS = {
    "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": (".jpg", b"\xff\xd8\xff"),
    "image/gif": (".gif", b"GIF8"),
    "image/webp": (".webp", None),        # RIFF....WEBP -- checked below
}


def sniff(data: bytes):
    """What this actually is, read off the front of it. None means we do not
    know, which is the only honest answer to give about bytes nobody can name."""
    if not data or len(data) < 16:
        return None
    for media, (_, magic) in KINDS.items():
        if magic and data.startswith(magic):
            return media
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _png_size(data: bytes):
    if len(data) < 24:
        return None
    w, h = struct.unpack(">II", data[16:24])
    return (w, h) if w and h else None


def _gif_size(data: bytes):
    if len(data) < 10:
        return None
    w, h = struct.unpack("<HH", data[6:10])
    return (w, h) if w and h else None


def _jpeg_size(data: bytes):
    """Walk the markers to the frame header. A JPEG carries its size in the
    middle of itself, not at the front, so there is no shortcut here."""
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        code = data[i + 1]
        if code in (0xD8, 0x01) or 0xD0 <= code <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            return None
        seg = struct.unpack(">H", data[i + 2:i + 4])[0]
        # Any start-of-frame carries the dimensions; the compression kind
        # differs and the two numbers do not.
        if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
            if i + 9 > n:
                return None
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return (w, h) if w and h else None
        if seg < 2:
            return None
        i += 2 + seg
    return None


def _webp_size(data: bytes):
    tag = data[12:16]
    try:
        if tag == b"VP8X" and len(data) >= 30:
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return (w, h)
        if tag == b"VP8 " and len(data) >= 30:
            w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return (w, h) if w and h else None
        if tag == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    except (struct.error, IndexError):
        return None
    return None


def size_of(data: bytes, media: str):
    """How big the picture is, read from its own header with no library. None
    when the header will not give it up, and the estimate below says so rather
    than inventing a number."""
    try:
        if media == "image/png":
            return _png_size(data)
        if media == "image/gif":
            return _gif_size(data)
        if media == "image/jpeg":
            return _jpeg_size(data)
        if media == "image/webp":
            return _webp_size(data)
    except (struct.error, IndexError, ValueError):
        return None
    return None


def shown_size(w: int, h: int):
    """The size the model will actually see, after its own scaling. Anything
    over the long edge or the area gets shrunk before it is read, and charging
    for pixels nobody looked at would put a lie in the budget."""
    w, h = max(1, int(w)), max(1, int(h))
    if max(w, h) > MAX_EDGE:
        scale = MAX_EDGE / max(w, h)
        w, h = max(1, round(w * scale)), max(1, round(h * scale))
    if w * h > MAX_AREA:
        scale = math.sqrt(MAX_AREA / (w * h))
        w, h = max(1, round(w * scale)), max(1, round(h * scale))
    return w, h


def tokens_for(size, n_bytes: int = 0) -> int:
    """What one picture costs, every turn it stays in the assistant's hands.

    The API's own rule: width times height over 750, on the picture as it will
    be seen. With no header to read, fall back to something deliberately
    pessimistic in bytes -- an estimate that runs low would understate the
    working set, and that number is one the assistant makes decisions with."""
    if size:
        w, h = shown_size(*size)
        return max(1, round(w * h / PIXELS_PER_TOKEN))
    return max(1, round(n_bytes / 600))


def tidy_name(name) -> str:
    """A name to show, not a path to open. Whatever arrives is reduced to one
    plain filename: no folders, no drive letters, nothing that could be read as
    somewhere to put something. The store's own names are shas either way, so
    this is a label and never a location."""
    raw = str(name or "").strip().replace("\\", "/").split("/")[-1]
    raw = re.sub(r"[\x00-\x1f]", "", raw)[:64].strip()
    return raw or "picture"


class Refused(ValueError):
    """A picture that will not be taken, and the words that say why. The figure
    is always in the message: a limit that will not say what it is is a limit
    nobody can work with."""


def keep(data: bytes, name=None) -> dict:
    """Put one picture in the store and hand back the pointer that rides the
    row. The same bytes twice are the same file -- the name is their sha -- so
    a screenshot sent again costs nothing new on disk."""
    if not data:
        raise Refused("an empty picture")
    if len(data) > MAX_BYTES:
        raise Refused(
            tidy_name(name) + " is " + str(round(len(data) / 1024 / 1024, 1))
            + " MB and the most one picture may be is "
            + str(MAX_BYTES // 1024 // 1024) + " MB")
    media = sniff(data)
    if not media:
        raise Refused(
            tidy_name(name) + " is not a picture I can read -- PNG, JPEG, GIF"
            " and WEBP are what the model takes")
    ext = KINDS[media][0]
    pid = hashlib.sha256(data).hexdigest()[:16]
    STORE.mkdir(parents=True, exist_ok=True)
    path = STORE / (pid + ext)
    if not path.exists():
        # Whole or not at all: a half-written picture under a name that claims
        # to be its own sha is the one file this store must never hold.
        tmp = path.with_suffix(ext + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    size = size_of(data, media)
    out = {
        "id": pid,
        "file": path.name,
        "media_type": media,
        "name": tidy_name(name),
        "bytes": len(data),
        "tokens": tokens_for(size, len(data)),
    }
    if size:
        out["w"], out["h"] = int(size[0]), int(size[1])
    return out


def keep_all(items, limit: int = MAX_PER_MESSAGE):
    """Every picture on one line, or a refusal saying which one and why.

    All or nothing on purpose. Half a message's pictures arriving silently is
    the shape of mistake that gets noticed three turns later, when the
    assistant answers about something it was never shown."""
    items = list(items or [])
    if len(items) > limit:
        raise Refused("that is " + str(len(items)) + " pictures and "
                      + str(limit) + " is the most on one line")
    out = []
    for item in items:
        raw = item.get("data") if isinstance(item, dict) else item
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(raw, str):
            # A data: URL from the page, or plain base64 from the command
            # line. Both are the same bytes wearing different clothes.
            raw = raw.split(",", 1)[-1] if raw.startswith("data:") else raw
            try:
                raw = base64.b64decode(raw, validate=True)
            except (ValueError, TypeError) as exc:
                raise Refused(tidy_name(name) + " did not arrive whole ("
                              + type(exc).__name__ + ")") from exc
        out.append(keep(raw, name))
    return out


def of_row(meta) -> list:
    """The pictures on a row, from its meta. One reader, so nothing anywhere
    else has to know the shape of what is stored."""
    if not isinstance(meta, dict):
        return []
    pics = meta.get("pictures")
    return [p for p in pics if isinstance(p, dict)] if isinstance(pics, list) else []


def weight(pics) -> int:
    """What a row's pictures add to its estimate."""
    return sum(int(p.get("tokens") or 0) for p in pics or [])


def path_of(pic) -> Path:
    """Where one picture's bytes are. The stored `file` is trusted no further
    than its shape -- a name from a row is still a name off a disk we wrote,
    and it is checked back to the store before anything opens it."""
    name = str(pic.get("file") or "")
    if not re.fullmatch(r"[0-9a-f]{16}\.(png|jpg|gif|webp)", name):
        raise Refused("not a name this store gives out: " + name[:40])
    return STORE / name


def read(pic) -> bytes:
    return path_of(pic).read_bytes()


def label(pics) -> str:
    """What a picture is to everything in the house that reads words and not
    pixels -- search, the dreamer, the digest, the voice. One line each,
    named, so a line that was only a picture is never an empty line."""
    return "\n".join("[picture: " + str(p.get("name") or "picture") + "]"
                     for p in pics or [])


def with_label(text: str, pics) -> str:
    """A row's words as a text-only reader should see them."""
    tag = label(pics)
    if not tag:
        return text or ""
    return (text + "\n" + tag) if (text or "").strip() else tag


def for_prompt(pics, start: int = 1) -> list:
    """What the assistant is told about the pictures on a row: which number
    each one is among those in its hands, its name, its size, and what it costs.

    The number is the whole point. The pictures themselves arrive after the
    JSON, in one run, and this is how the assistant knows which is which -- picture 3 is
    the third one attached, and this says which row it came in on."""
    out = []
    for i, p in enumerate(pics or []):
        item = {
            "n": start + i,
            "name": p.get("name") or "picture",
            "tokens_est": int(p.get("tokens") or 0),
        }
        if p.get("w") and p.get("h"):
            item["size"] = str(p["w"]) + "x" + str(p["h"])
        out.append(item)
    return out


def blocks(pics) -> list:
    """The picture blocks as the model takes them, in the order it was told.

    A picture whose bytes have gone missing is said out loud in the assistant's own line
    of the conversation rather than quietly dropped: a numbered thing that is
    not there would make every number after it wrong."""
    out, missing = [], []
    for p in pics or []:
        try:
            data = read(p)
        except (OSError, Refused):
            missing.append(p.get("name") or p.get("id") or "a picture")
            continue
        out.append({
            "type": "image",
            "source": {"type": "base64",
                       "media_type": p.get("media_type") or "image/png",
                       "data": base64.b64encode(data).decode("ascii")},
        })
    return out, missing
