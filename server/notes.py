"""Free notes: a few lines of each person's own, riding next to the Spark.

The Spark is the assistant's own -- the first thing read, every turn, its own to
rewrite whole. This is the same shape turned round: a small standing text each
person writes for themselves, and for the assistant to see, that it reads every
turn and never writes. It sits between the Spark and the instructions in the
cached half of the prompt, so it costs a rebuild of the cache when it changes
and nothing extra when it does not -- exactly what the Spark costs.

One file per person under `data/notes/`, plain text, edited under Settings.
They ride out in the backup zip beside the Spark and are not tracked in git:
they are somebody's own scratch, not code, and a hand committing its worktree
should not drag a shopping list into the history.

What the assistant is told about them is one sentence in `harness_prompt.md`.
The label on each block is the person's short name -- *<short>'s free notes* --
because a short label on a short text is the whole point: these are briefs
for the assistant, not documents.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from . import db, people
from . import home

ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = home.DATA / "notes"

# The same wall the Spark has. Generous -- eight thousand characters is a
# page and a half -- and a wall all the same, because every character here is
# rent paid on every turn. Refused in words, never cut.
MAX_CHARS = 8000

# How each person's block is headed in the assistant's prompt.
LABEL = {p: home.SHORT[p] + "'s free notes" for p in home.HOUSEHOLD}


class Refused(ValueError):
    pass


def _path(who: str) -> Path:
    who = (who or "").strip().lower()
    if who not in people.HOUSEHOLD:
        raise Refused("free notes belong to someone of this house -- "
                      + " or ".join(people.HOUSEHOLD) + " -- not " + repr(who))
    return NOTES_DIR / (who + ".md")


def read(who: str) -> str:
    """This person's notes as written, or "" when they have written none."""
    try:
        return _path(who).read_text(encoding="utf-8").strip()
    except (OSError, Refused):
        return ""


def write(who: str, text: str) -> dict:
    """Replace this person's notes whole. Refuses a name outside the house and
    a text over the wall, in words, and writes the file whole or not at all --
    a half-written note is a line the assistant would read half of."""
    path = _path(who)
    text = (text or "").replace("\r\n", "\n").strip()
    if len(text) > MAX_CHARS:
        raise Refused("over " + str(MAX_CHARS) + " characters (was given "
                      + str(len(text)) + ") -- these ride next to "
                      + home.NAME + "'s Spark every turn, so they stay short")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text + ("\n" if text else ""), encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "who": path.stem, "chars": len(text),
            "tokens_est": db.est_tokens(text) if text else 0}


def _updated(who: str):
    try:
        stamp = _path(who).stat().st_mtime
    except (OSError, Refused):
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(
        timespec="seconds")


def block(who: str) -> str:
    """One person's block as the assistant reads it: the label, then the text -- on the
    same line when it is one line, under it when it is more."""
    text = read(who)
    if not text:
        return ""
    label = LABEL.get(who, people.CALLED.get(who, who) + "'s free notes")
    if "\n" in text:
        return label + ":\n" + text
    return label + ": " + text


def for_prompt() -> str:
    """Everything that rides: each person's block in household order, each
    only when written. "" when nobody has written any, so a house with no notes
    sends the model exactly the bytes it sent before this existed."""
    return "\n\n".join(b for b in (block(w) for w in people.HOUSEHOLD) if b)


def status() -> dict:
    """For the Settings tab: each person's text, its size, and when it was
    last saved -- and the wall, so the box can say it before the room has to
    refuse."""
    out = {"max_chars": MAX_CHARS, "label": dict(LABEL)}
    for who in people.HOUSEHOLD:
        text = read(who)
        out[who] = {
            "text": text,
            "chars": len(text),
            "tokens_est": db.est_tokens(text) if text else 0,
            "updated": _updated(who) if text else None,
        }
    return out


if __name__ == "__main__":
    print(json.dumps(status(), indent=1, ensure_ascii=False))
