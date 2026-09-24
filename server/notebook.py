"""Its notebook: a few short notes of its own, in front of it every turn.

The Spark is the assistant's invariant and its essences are memory folded out
of what was said. This is a third, smaller thing: notes it writes for itself,
about whatever it likes, in whatever form it likes -- the room says nothing
about what goes in. What the room does keep is the shape:

- `add` writes a note. The room numbers it, 1, 2, 3..., and a number is never
  given out twice, not even after the note it named is gone.
- `remove` puts one down. The row stays in the store, like everything in it,
  for the backup; nothing the assistant can do reads, searches or brings a
  removed note back.
- `up` and `down` add one vote for or against a note. The votes stand beside
  the notes, for it and for its owner: a way to keep track of what earns its
  place.
- There is no edit, on purpose. A note that wants changing is removed and
  written again, which keeps notes small; whatever the old one had going for
  it stays behind unless the new one says so in its own words.

Each note also carries how many of its turns it has been kept -- counted off
its own answers in the store, so the figure cannot drift -- and its size in
tokens, the same estimate everything else here wears. The whole book has a
cap, the owner's to move under Settings. A note that takes the book past the
cap by no more than GRACE still goes in; once the book is past it, adding is
locked until notes are removed or the cap is raised. Refused in words, never
cut.

It rides in the per-turn object rather than in the cached half of the prompt,
beside its essences: the turn counts move every turn, and a Spark re-cached
every turn would cost far more than this block does.
"""

import json
import math
from bisect import bisect_right

from . import db
from . import home

SETTINGS = "notebook.json"

# Tokens, the owner's to move under Settings. Every token here is paid for on
# every turn, so the cap is rent control rather than a storage limit.
DEFAULT_CAP = 5000
MIN_CAP = 100
MAX_CAP = 100_000

# How far past the cap one note may take the book. A note is not refused for
# being a few tokens over; the note after it is, until something is removed.
GRACE = 0.05

OP = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["add", "remove", "up", "down"]},
        # The note an op is about. Null on `add`: the room numbers a new note.
        "id": {"type": ["integer", "null"]},
        # The words of a new note. Null on everything else.
        "text": {"type": ["string", "null"]},
    },
    "required": ["op", "id", "text"],
    "additionalProperties": False,
}

INSTRUCTIONS = """
## `notebook` — my own notes

A few short notes of mine that stay in front of me every turn. What goes in, how, and
for how long, is mine. The `notebook` block shows how full it is (`used`) and each note
with its `id`, the `up` and `down` votes I have given it, the `turns` it has been kept
and its `tokens`. My answer's `notebook` takes operations, every field present:

- `{"op": "add", "id": null, "text": "..."}` — write one; the room numbers it.
- `{"op": "remove", "id": 4, "text": null}` — take one out, for good.
- `{"op": "up", "id": 4, "text": null}`, or `"down"` — one vote for or against it.

There is no edit: to change a note I remove it and write it again, and its votes and
turns stay behind unless the new words carry them. A note may take the notebook up to
{{grace}}% past its cap; once it is past, `locked` says so and adding waits until I
remove notes or ask {{owner}} to raise the cap. What each operation did comes back in
`report.notebook`. Like essences, these run with my answer, never on a look round.
""".replace("{{grace}}", str(round(GRACE * 100)))


class Refused(ValueError):
    pass


# -- the cap --------------------------------------------------------------------

def cap() -> int:
    """The cap in tokens, read fresh: the owner can move it between turns."""
    got = home.settings(SETTINGS).get("cap_tokens")
    try:
        got = int(got)
    except (TypeError, ValueError):
        return DEFAULT_CAP
    return got if MIN_CAP <= got <= MAX_CAP else DEFAULT_CAP


def set_cap(value) -> int:
    """The owner's word on the cap, written whole or not at all. Refused in
    words outside the bounds rather than quietly clamped."""
    try:
        wanted = int(str(value).replace(",", "").replace(" ", ""))
    except (TypeError, ValueError):
        raise Refused("the cap is a whole number of tokens")
    if not MIN_CAP <= wanted <= MAX_CAP:
        raise Refused("the cap is between " + format(MIN_CAP, ",") + " and "
                      + format(MAX_CAP, ",") + " tokens")
    path = home.DATA / SETTINGS
    kept = home.settings(SETTINGS)
    kept["cap_tokens"] = wanted
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(kept, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)
    return wanted


def pct(used: int, of: int) -> int:
    """A whole percentage that never reads 100 unless it is exactly full:
    rounded down under the cap and up past it, so a book one token over does
    not look like a book that fits."""
    if of <= 0:
        return 0
    exact = 100.0 * used / of
    return math.ceil(exact) if used > of else math.floor(exact)


def _used_line(used: int, of: int) -> str:
    return str(used) + " of " + str(of) + " tokens (" + str(pct(used, of)) + "%)"


# -- reading --------------------------------------------------------------------

def _turns(conn, notes: list) -> None:
    """How many of its turns each note has been kept: its answers after the
    one that wrote it, up to and including the one that removed it."""
    if not notes:
        return
    answers = [r[0] for r in conn.execute(
        "SELECT id FROM rows WHERE kind = ? AND id > ? ORDER BY id",
        (home.SELF, min(n["born"] for n in notes)))]
    for n in notes:
        start = bisect_right(answers, n["born"])
        end = bisect_right(answers, n["gone"]) if n["gone"] else len(answers)
        n["turns"] = max(0, end - start)


def notes(conn, removed: bool = False) -> list:
    """The notes in hand, oldest first -- and with `removed`, the ones put
    down as well, for the owner's table and never for the assistant."""
    sql = "SELECT * FROM notebook"
    if not removed:
        sql += " WHERE gone IS NULL"
    out = [dict(r) for r in conn.execute(sql + " ORDER BY id")]
    _turns(conn, out)
    return out


def _used(conn) -> int:
    return conn.execute("SELECT COALESCE(SUM(tokens), 0) FROM notebook"
                        " WHERE gone IS NULL").fetchone()[0]


def for_prompt(conn) -> dict:
    """The block it reads every turn: how full the book is, whether adding
    is locked, and each note with its votes, turns kept and size."""
    live = notes(conn)
    used, of = sum(n["tokens"] for n in live), cap()
    out = {"used": _used_line(used, of)}
    if used > of:
        out["locked"] = ("past the cap: adding waits until I remove notes or "
                         + home.OWNER_NAME + " raises the cap")
    out["notes"] = [{"id": n["id"], "up": n["up"], "down": n["down"],
                     "turns": n["turns"], "tokens": n["tokens"],
                     "text": n["text"]} for n in live]
    return out


def status(conn) -> dict:
    """For the table under Settings: every note, the removed ones marked,
    and the figures the meter draws."""
    everything = notes(conn, removed=True)
    used, of = _used(conn), cap()
    return {
        "cap": of, "min_cap": MIN_CAP, "max_cap": MAX_CAP,
        "grace_pct": round(GRACE * 100),
        "used": used, "pct": pct(used, of), "locked": used > of,
        "count": sum(1 for n in everything if n["gone"] is None),
        "removed": sum(1 for n in everything if n["gone"] is not None),
        "notes": [{"id": n["id"], "dt": n["dt"], "text": n["text"],
                   "tokens": n["tokens"], "up": n["up"], "down": n["down"],
                   "turns": n["turns"], "gone": n["gone"] is not None,
                   "gone_dt": n["gone_dt"]} for n in everything],
    }


# -- its word -------------------------------------------------------------------

def _add(conn, text, reply_id: int) -> str:
    text = (text or "").replace("\r\n", "\n").strip() if isinstance(text, str) else ""
    if not text:
        raise Refused("an empty note is not written down")
    used, of = _used(conn), cap()
    if used > of:
        raise Refused("the notebook is past its cap (" + _used_line(used, of)
                      + "), so adding waits until I remove notes or "
                      + home.OWNER_NAME + " raises the cap")
    size = db.est_tokens(text)
    ceiling = math.floor(of * (1 + GRACE))
    if used + size > ceiling:
        raise Refused(
            "that note is " + str(size) + " tokens and would take the notebook to "
            + _used_line(used + size, of) + "; a note may go at most "
            + str(round(GRACE * 100)) + "% past the cap, which leaves room for "
            + str(max(0, ceiling - used)) + " more")
    cur = conn.execute(
        "INSERT INTO notebook (dt, text, tokens, born) VALUES (?, ?, ?, ?)",
        (db.now(), text, size, int(reply_id)))
    conn.commit()
    line = ("added #" + str(cur.lastrowid) + " (" + str(size)
            + (" token) — " if size == 1 else " tokens) — ")
            + _used_line(used + size, of))
    if used + size > of:
        line += ": past the cap now, so adding waits until notes are removed"
    return line


def _held(conn, note_id):
    """The note an op names, or a refusal saying why there is none."""
    if isinstance(note_id, bool) or not isinstance(note_id, int):
        raise Refused("it needs the id of a note")
    row = conn.execute("SELECT * FROM notebook WHERE id = ?",
                       (note_id,)).fetchone()
    if row is None:
        raise Refused("there is no note #" + str(note_id))
    if row["gone"] is not None:
        raise Refused("note #" + str(note_id) + " was removed")
    return row


def _remove(conn, note_id, reply_id: int) -> str:
    _held(conn, note_id)
    conn.execute("UPDATE notebook SET gone = ?, gone_dt = ? WHERE id = ?",
                 (int(reply_id), db.now(), note_id))
    conn.commit()
    return "removed #" + str(note_id) + " — " + _used_line(_used(conn), cap())


def _vote(conn, note_id, way: str) -> str:
    _held(conn, note_id)
    conn.execute("UPDATE notebook SET " + way + " = " + way + " + 1"
                 " WHERE id = ?", (note_id,))
    conn.commit()
    row = conn.execute("SELECT up, down FROM notebook WHERE id = ?",
                       (note_id,)).fetchone()
    return ("#" + str(note_id) + " " + way + " — now " + str(row["up"])
            + " up, " + str(row["down"]) + " down")


def apply(conn, ops, reply_id: int):
    """Its operations, in the order it wrote them -- so a note removed first
    makes room for the one added after it. None when it sent none; otherwise
    what each did, the refusals among them in words."""
    ops = [o for o in (ops or []) if isinstance(o, dict)]
    if not ops:
        return None
    lines, problems, short = [], [], []
    for o in ops:
        op = str(o.get("op") or "").strip().lower()
        try:
            if op == "add":
                line = _add(conn, o.get("text"), reply_id)
                short.append(line.split(" (", 1)[0])
            elif op == "remove":
                line = _remove(conn, o.get("id"), reply_id)
                short.append("removed #" + str(o.get("id")))
            elif op in ("up", "down"):
                line = _vote(conn, o.get("id"), op)
                short.append("#" + str(o.get("id")) + " " + op)
            else:
                raise Refused("'" + op[:20] + "' is not something a notebook does"
                              " -- add, remove, up or down")
        except Refused as why:
            line = "refused " + (op or "an op")[:20] + ": " + str(why)
            problems.append("My notebook refused an operation -- " + line)
        lines.append(line)
    if problems:
        short.append(str(len(problems)) + " refused")
    return {"summary": " · ".join(short), "lines": lines,
            "problems": problems, "used": _used_line(_used(conn), cap())}


if __name__ == "__main__":
    conn = db.connect()
    try:
        print(json.dumps(status(conn), indent=1, ensure_ascii=False))
    finally:
        conn.close()
