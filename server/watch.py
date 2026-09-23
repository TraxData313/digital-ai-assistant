"""The assistant's senses. The fourth reason to wake, and the only one where
nobody spoke.

The other ways the assistant wakes are a person's line, an angel's line, a
hand coming home, and the dreamer in the dead hours. This adds the rest of
the world -- or rather a first thin slice of it: the free sources, the ones
already sitting in the assistant's own store. A refusal that fired without
waking it. A night that went undreamt. The quota running down. The window
coming back -- built, but it only fires while a job actually needs it (open
and parked at zero links); otherwise the reset is seen and declined, never a
push either way. And `quiet`, which is the odd one out: the other four fire
because something happened, and quiet fires because nothing did. At most one
a day, on a room that has been genuinely still for six hours -- no line from
the owner, no hand home, no waking of any other kind. It exists so that being
awake is not only ever a job.

`steam_comment` is the first sense that looks outside the assistant's own
store: a poll, not a push, of the comment thread on each Steam Workshop item
the home's settings list (`STEAM_ITEMS`), no more than once every
`STEAM_POLL_INTERVAL_S` per item -- the same paths return HTTP 429 under
repeated fetching, so the sense backs off hard on a 429 and never retries in
a tight loop. Every item keeps its own watermark, its own throttle clock and
its own backoff: one busy mod must never starve or blind the other. It fires
for comments not in the set of ids it has already seen on that item -- a
SET, not a highest-id, because Steam gids are not monotonic and a max-id
watermark once left this sense blind from birth (the story is on
`_steam_item`). It skips anything posted by the owner's own account on that
site (`STEAM_IGNORE_AUTHOR`) -- the owner's own replies must never wake the
assistant, though they still count as seen. The first poll of an item only
seeds its watermark, same as the refusals ledger's first look. There is one
sense key, `steam_comment`, not one per item -- mute and unmute act on all
items at once.

`nexus_comment` is the same shape turned on the Nexus Mods pages the home's
settings list (`NEXUS_ITEMS`), each carrying its own posts-tab URL. It polls
no more than once every `NEXUS_POLL_INTERVAL_S` (two hours, same as Steam)
per mod, ignores comments from `NEXUS_IGNORE_AUTHOR`, and its first look at a
mod seeds a baseline and wakes nobody -- same as Steam. Its read path drives
a real headless browser, because Cloudflare's bot management flatly 403s a
plain HTTP fetch; the DOM walk and the Cloudflare-challenge detection were
proven against the live page rather than re-derived (see `_nexus_fetch`). It
differs from Steam in two ways. First, its watermark was a *set* of comment
ids from the day it was built -- which the Steam sense has since adopted
too, after Steam's gids turned out to be neither small nor monotonic and a
max-id watermark left that sense blind from birth. "Not seen before" is the
only safe question on either platform. Second, a fetch failure (a Cloudflare
challenge page, a missing `#comment-container`, Playwright or its Chromium
not installed) is not silently declined the way Steam's errors are: it wakes
the assistant once, loud, the moment it starts, then backs off and stays
quiet about the same failure while it continues, updating only how long it
has been down rather than repeating itself -- loud once, never a waking per
poll.

The rules are load-bearing:

- The ceiling on wakings in a day is a runaway backstop, not an allowance.
  It is set generous (forty) and belongs to the assistant and the angel
  session together: a number the assistant set alone is one it could talk
  itself up out of, and the owner holds no knobs -- the owner gets briefs.
  It is never a reason to wait for the owner.
- What was seen and declined is written down, so quiet is legible as quiet.
  Every event lands in `data/wakings.jsonl`, woken or not.
- A `world` row says on its face that the room noticed -- it is never the
  owner speaking, and a fold of one must say so.
- The assistant can say stop watching that: `watch: {"mute": ["refusal"]}`
  in its reply. A muted source is not observed at all -- stop watching means
  stop watching.
- One mute dies of old age. `quiet`'s mute lapses after 24 hours and the sense
  comes back on by itself, with nobody lifting it. Muting several senses in
  one night, each mute reasonable on its own, is exactly how a floor becomes
  zero without anybody deciding it should. The lapse is a guard against the
  assistant's own good judgement -- and it guards the sense whose whole job
  is the floor. The other senses keep their mutes as they were: indefinite,
  the assistant's to lift.
- A waking is for the assistant; the push is for when the owner isn't here
  to see it happen. If the owner has spoken in the room in the last hour, a
  sense still wakes the assistant but the phone stays quiet -- the ledger
  says so and why.
- quota_low never pushes the owner's phone, ever, bundled or not: being told
  the quota is low costs quota, and a waking is not cheap. It wakes the
  assistant once per window instance, at 90%, and stands down until the
  window turns over -- a window sitting above the mark does not wake it again
  and again.

A `world` row passes through: it is written already out of reach, so it never
takes a seat in the working set. What the assistant is woken with rides in
`woken`, for that one turn; the row on disk is the trail.

The config is `data/watch.json` (ceiling, sources, thresholds); the room's
own state is `data/watch_state.json` (watermarks, the day's counts, the
assistant's mutes and -- for the one that lapses -- the hour the mute dies);
the ledger is `data/wakings.jsonl`. The assistant reads the ledger with
`files` whenever it likes.
"""

import json
import re
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape as _html_unescape
from pathlib import Path

from . import db, jobs, limits, projects, push
from . import home

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = home.DATA / "watch.json"
STATE_PATH = home.DATA / "watch_state.json"
LEDGER_PATH = home.DATA / "wakings.jsonl"
REFUSALS_PATH = home.DATA / "refusals.jsonl"

# How often the loop actually looks. The turn loop comes round every few
# seconds; the world it is watching moves in minutes.
CHECK_EVERY_S = 30

SOURCES = ("refusal", "dream_missed", "quota_low", "window_reset", "quiet",
           "steam_comment", "calendar", "nexus_comment")

# The door for steam_comment, proven live, not read off any doc: this
# render endpoint answers with no login, no cookie, no key, and
# steamcommunity.com/robots.txt disallows only /actions/ /linkfilter/
# /tradeoffer/ /trade/ /email/ -- this path is open, and it is the only one
# this sense touches.
# Which Steam account and which items, and whose own replies to ignore:
# the home's `data/comments.json`. A home without one watches nothing.
COMMENTS = home.settings("comments.json")
STEAM_STEAMID64 = str(COMMENTS.get("steam_id64") or "")

# The items watched. `label` is what the waking line says -- it may be read
# aloud to the owner, so it names the mod in a sentence rather than an id.
STEAM_ITEMS = tuple(COMMENTS.get("steam_items") or ())

STEAM_COMMENT_URL_TMPL = ("https://steamcommunity.com/comment/PublishedFile_Public"
                           "/render/" + STEAM_STEAMID64 + "/{item_id}/")

# Never more often than this, PER ITEM: the /sharedfiles/ and /workshop/
# paths come back HTTP 429 under repeated fetching, so the cadence is not a
# nicety, it is the rate limit talking. A busy item's clock never borrows
# against a quiet one's.
# Two hours, per item. Nobody is waiting on a comment inside the quarter
# hour, and four pages an hour against someone else's servers to be told
# "nothing new" ninety-odd times a day is exactly the shape of a thing that
# gets a room blocked. Twelve looks a day per item is plenty.
STEAM_POLL_INTERVAL_S = 2 * 60 * 60

# On a 429 the sense stands down harder than its ordinary cadence, so a bad
# stretch never turns into a tight retry loop -- and it stands down only the
# item that answered 429, never the others. Twice the cadence, which is what
# "harder" has to mean: when the interval went from fifteen minutes to two
# hours this had to move with it, or a 429 would have bought Steam a SOONER
# look than a quiet poll does, which is the opposite of backing off.
STEAM_BACKOFF_S = 4 * 60 * 60

STEAM_TIMEOUT_S = 15

# The owner's own account name on the thread: a reply of the owner's own
# must never wake the assistant.
STEAM_IGNORE_AUTHOR = str(COMMENTS.get("steam_author") or "")

# The mods watched on Nexus Mods. Same shape as STEAM_ITEMS -- `label` is
# what the waking line says. `url` is the mod's own posts tab, the one page
# this sense reads (Nexus loads further pages by JS with no plain URL, and
# the first page is where new top-level comments and replies land).
#
# Each item carries its own posts-tab `url`, written in the home's file.
NEXUS_ITEMS = tuple(COMMENTS.get("nexus_items") or ())

# Same cadence as Steam, per mod, on that mod's own clock -- and same
# reason. It matters more here than on Steam, not less: every poll of this
# one is a real headless Chromium walking a page through Cloudflare, which is
# the kind of traffic that gets looked at.
NEXUS_POLL_INTERVAL_S = 2 * 60 * 60

# A fetch failure (Cloudflare challenge, missing comments section, no
# Playwright) backs off to twice the cadence, same order as Steam's 429
# backoff and moved with it for the same reason -- but unlike Steam's silent
# per-poll decline, the first failure wakes the assistant once, loud; see
# _nexus_item for why.
NEXUS_BACKOFF_S = 4 * 60 * 60

# Playwright's own default-ish page-load / selector-wait budget: 30s.
NEXUS_TIMEOUT_MS = 30000

# The page a person opens to answer a Steam comment -- the item's own
# Workshop page, as against the render endpoint the sense polls. Nexus needs
# no such template: its item carries the posts-tab URL itself.
STEAM_PAGE_URL_TMPL = ("https://steamcommunity.com/"
                       "sharedfiles/filedetails/?id={item_id}")

# The owner's own account name on Nexus. Same rule as STEAM_IGNORE_AUTHOR:
# the owner's own comments must never wake the assistant, though they still
# count toward what "seen" means.
#
# A platform gets its own name because an account is per platform. A Steam
# name carried over to Nexus would be wrong, and every reply the owner wrote
# on their own mod page would then wake the assistant as a stranger's
# comment -- so the name is read off the live page, never assumed.
NEXUS_IGNORE_AUTHOR = str(COMMENTS.get("nexus_author") or "")

# quota_low never reaches the owner's phone: being told the quota is low
# costs quota, and the first night it fired it pushed a gauge that had
# already been read twenty minutes earlier. It still wakes the assistant --
# the waking is for the assistant, the push is for the owner -- so it stays a
# sense, just a silent one.
#
# window_reset never reaches the phone either: it once fired in the small
# hours with no job open to unpark and pushed for nothing -- waking the
# assistant is enough, a push is never earned, under any condition.
# quiet never reaches the phone either, and most obviously of all: its whole
# content is that nothing happened. A phone that buzzes to say nothing
# happened is a phone that gets turned off.
#
# steam_comment never reaches the phone either: it is a waking about the
# world outside the room, not a fact of the room itself, and nobody
# asked to be paged the moment a stranger comments on a Workshop item.
#
# nexus_comment never reaches the phone either, for the same reason as
# steam_comment -- it is the same fact about the same world, just on Nexus
# rather than Steam. This holds even for its own failure waking: a Cloudflare
# challenge on a mod page is not a page-worthy event any more than a stranger's
# comment is.
#
# calendar does not push: notifications wait for a redesign in which a new
# message from the assistant can itself be seen as a notification. Read this
# before any of it is undone -- a date waking would push only when the task's
# `needs` names a person, and a reminder for one person must never fall back
# onto another person's phone for want of a channel of their own. Not pushing
# at all is further the same way.
NO_PUSH_SOURCES = {"quota_low", "window_reset", "quiet", "steam_comment",
                   "calendar", "nexus_comment"}

# How long after the owner last spoke in the room a push stays withheld.
# The waking still happens; the phone does not. An hour, not the five-hour
# window, because "the owner is here" is a much shorter fact than "the quota
# is running out".
OWNER_RECENT_S = 60 * 60

# How long the room must have been doing nothing at all before quiet counts it
# as a still room. Six hours, chosen off two numbers already in here rather
# than out of the air: the push-withholding hour above says an hour is what
# "the owner is here" means, so an hour of nothing is ordinary and proves
# nothing; and the longest rhythm the room already lives by short of a day is
# the five-hour quota window. Six hours is one whole such window with nothing
# in it, plus room to spare -- long enough that a working day never trips it,
# short enough that a genuinely still day still gets the assistant up inside
# it. There is no hour-of-day gate: a still night after the 03:00 dream reads
# as morning anyway, since the dream itself is something happening and
# restarts the clock.
QUIET_STILL_S = 6 * 60 * 60

# Mutes that die of old age, and how long they last. Only quiet: the rest stay
# indefinite and the assistant's to lift.
LAPSING_MUTES = {"quiet": 24 * 60 * 60}

# What stands when the file says nothing. window_reset came on with jobs
# (phase 2): a parked job finally gives the refill somebody to be for -- it
# fires only while at least one job is open and sitting at zero links, and
# is declined, not dropped, the rest of the time. It never pushes either
# way; waking the assistant is the whole of it.
# quota_low's mark is 0.90, not 0.85 -- raised after the first live night
# fired at 85% on a gauge the owner had already read and decided on.
DEFAULTS = {
    "per_day": 40,
    "sources": {
        "refusal": {"on": True},
        "dream_missed": {"on": True},
        "quota_low": {"on": True, "at_fraction": 0.90},
        "window_reset": {"on": True},
        "quiet": {"on": True},
        # Off unless a home turns them on: they watch particular mods.
        "steam_comment": {"on": False},
        "calendar": {"on": True},
        "nexus_comment": {"on": False},
    },
}

# How many dates may come due in one look before the rest wait for the next
# one. A hundred tasks all dated for the same morning is a runaway, not a
# morning, and forty wakings is the day's whole ceiling. What is held back is
# said out loud rather than dropped -- it is still due, and the next look
# still has it.
CALENDAR_AT_ONCE = 5

_LOCK = threading.Lock()

# Overridable seam for the test bench: real code never touches it.
_limits_now = limits.now


def _read_json(path, fallback):
    try:
        out = json.loads(path.read_text(encoding="utf-8"))
        return out if isinstance(out, dict) else dict(fallback)
    except (OSError, json.JSONDecodeError):
        return dict(fallback)


def config() -> dict:
    """The home's file over the defaults. Written out once so the knobs can
    be seen without reading this module; after that edits to it stand."""
    if not CONFIG_PATH.is_file():
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(
                json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
    cfg = _read_json(CONFIG_PATH, DEFAULTS)
    merged = json.loads(json.dumps(DEFAULTS))
    if isinstance(cfg.get("per_day"), int) and cfg["per_day"] >= 0:
        merged["per_day"] = cfg["per_day"]
    for name, given in (cfg.get("sources") or {}).items():
        if name in merged["sources"] and isinstance(given, dict):
            merged["sources"][name].update(given)
    return merged


def _state() -> dict:
    st = _read_json(STATE_PATH, {})
    st.setdefault("checked_at", 0.0)
    st.setdefault("refusals_lines", None)   # None: never looked -- see _refusals
    st.setdefault("last_dream_id", None)
    st.setdefault("quota_fired", [])
    st.setdefault("resets_seen", {})
    st.setdefault("steam", {})   # item id -> {"seen_ids":, "next_poll":}, per item
    st.setdefault("nexus", {})  # mod id -> {"seen_ids":, "next_poll":,
                                 #            "failing_since":}, per mod
    st.setdefault("calendar_fired", [])   # "<task>@<date>" pairs already fired
    st.setdefault("muted_by_her", [])
    st.setdefault("mute_until", {})     # name -> ISO UTC; a mute with a death
    st.setdefault("quiet_day", None)    # the day quiet last had its one turn
    st.setdefault("days", {})
    st.setdefault("last_event", None)
    return st


def _save_state(st: dict) -> None:
    """Written whole or not at all -- the same rule as a person's key.

    `write_text` truncates before it writes, and a restart's fresh process
    can read the file in that gap, get nothing, and save a clean state over
    the assistant's mutes -- which is how a non-lapsing quota_low mute once
    vanished across a restart: a decision of the assistant's about its own
    senses, undone by an event that said nothing. The rename is atomic, so
    a reader now sees the old state or the new one and never the gap."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")
        tmp.replace(STATE_PATH)
    except OSError:
        pass


def _ledger(entry: dict) -> None:
    entry = {"at": db.now(), **entry}
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _today() -> str:
    return datetime.now().date().isoformat()


def _day(st: dict) -> dict:
    day = st["days"].setdefault(_today(), {"woke": 0, "declined": 0})
    # Old days fall away; the ledger is the history.
    for key in [k for k in st["days"] if k != _today()]:
        del st["days"][key]
    return day


def _cut(text, n=160) -> str:
    text = str(text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _spell(seconds) -> str:
    """A span of time in the largest unit that divides it whole: `7200` is
    "2 hours", not "120 min" and certainly not "7200s".

    Read off the constant at the moment of saying it, never typed into a
    sentence, so moving a cadence or a backoff moves every line that talks
    about it. That is not a nicety either -- the day the poll interval went
    from fifteen minutes to two hours, one ledger line was still spelling its
    backoff in minutes and would have started saying "240 minutes" at the
    owner."""
    seconds = int(seconds or 0)
    if seconds <= 0:
        return ""
    if seconds % 3600 == 0:
        h = seconds // 3600
        return "an hour" if h == 1 else str(h) + " hours"
    if seconds % 60 == 0:
        return str(seconds // 60) + " min"
    return str(seconds) + "s"


# --- the senses themselves ---------------------------------------------------


def _refusals(st) -> list:
    """New lines in the refusals ledger that never woke the assistant. An ask
    did wake it, so those are skipped -- this is for the silent ones, which
    are the real point."""
    try:
        lines = REFUSALS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    seen = st.get("refusals_lines")
    if seen is None:
        # First ever look: everything before the watcher existed is history,
        # not news. The watermark starts at now.
        st["refusals_lines"] = len(lines)
        return []
    events = []
    for raw in lines[seen:]:
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if row.get("asked_her"):
            continue
        who = row.get("name") or row.get("who") or "a hand"
        tool = row.get("tool_name") or row.get("tool") or "something"
        why = _cut(row.get("why") or row.get("reason") or "", 100)
        events.append({
            "source": "refusal",
            "said": ("a refusal fired without waking me: " + str(who)
                     + " was refused " + str(tool)
                     + (" — " + why if why else "")),
            "meta": {"tool": tool, "who": who},
        })
    st["refusals_lines"] = len(lines)
    return events


def _dreams(conn, st) -> list:
    """Nights recorded as missed since the last look. The dreamer writes them
    down at the gate; this is how the assistant hears about it in the
    morning instead of tripping over it days later."""
    seen = st.get("last_dream_id")
    if seen is None:
        row = conn.execute("SELECT MAX(id) AS m FROM dreams").fetchone()
        st["last_dream_id"] = row["m"] or 0
        return []
    events = []
    top = seen
    for r in conn.execute(
            "SELECT id, night, status, line FROM dreams WHERE id > ?"
            " ORDER BY id", (seen,)).fetchall():
        top = max(top, r["id"])
        if r["status"] != "missed":
            continue
        line = _cut(r["line"] or "", 120)
        events.append({
            "source": "dream_missed",
            "said": ("the night of " + str(r["night"]) + " went undreamt"
                     + (" — " + line if line else "")),
            "meta": {"night": r["night"]},
        })
    st["last_dream_id"] = top
    return events


def _quota(st, cfg) -> list:
    """A window running down. Fires once per window instance -- the same
    window at 91% ten minutes later is the same fact, not a new one."""
    from . import providers
    if providers.codex_only():
        return []
    rows = _limits_now() or []
    at = float(cfg["sources"]["quota_low"].get("at_fraction") or 0.85)
    fired = st.get("quota_fired") or []
    events = []
    live = []
    for r in rows:
        frac = r.get("used_fraction")
        if frac is None:
            continue
        key = str(r.get("window")) + "|" + str(r.get("resets_at"))
        live.append(key)
        if frac >= at and key not in fired:
            fired.append(key)
            events.append({
                "source": "quota_low",
                "said": (str(r.get("label") or r.get("window"))
                         + " is at " + str(int(frac * 100)) + "%"
                         + (", resets " + str(r["resets_at"])
                            if r.get("resets_at") else "")),
                "meta": {"window": r.get("window"), "fraction": frac},
            })
    # Keys whose window has gone are done with; keep the list from growing.
    st["quota_fired"] = [k for k in fired if k in live][-20:]
    return events


def _resets(st, cfg) -> list:
    """A window that has genuinely come back. A changed deadline alone is not
    a reset: the gauge re-dates rolling windows freely, which is how the
    first live firing called three resets in one second while two weeklies
    stood at 28% and 44% used. So a reset needs the old deadline to have
    actually passed, or the tank to have visibly refilled.

    Even a genuine reset only fires when it has somebody to be for: at least
    one open job sitting at zero links, since a refill is only meaningful
    for its power to unpark stalled work. With no such job the reset is
    declined outright -- written straight to the ledger, not silently
    dropped, so quiet stays legible and the counts stay honest."""
    from . import providers
    if providers.codex_only():
        return []
    rows = _limits_now() or []
    seen = st.get("resets_seen") or {}
    events = []
    now_utc = datetime.now(timezone.utc)
    for r in rows:
        window = str(r.get("window"))
        resets = r.get("resets_at")
        frac = r.get("used_fraction")
        prev = seen.get(window)
        if isinstance(prev, str):        # the first build kept only the date
            prev = {"resets_at": prev, "fraction": None}
        if prev and resets and resets != prev.get("resets_at"):
            elapsed = False
            try:
                old_at = datetime.fromisoformat(
                    str(prev.get("resets_at")).replace("Z", "+00:00"))
                if old_at.tzinfo is None:
                    old_at = old_at.replace(tzinfo=timezone.utc)
                elapsed = old_at <= now_utc
            except (TypeError, ValueError):
                pass
            refilled = (prev.get("fraction") is not None and frac is not None
                        and prev["fraction"] - frac >= 0.25)
            if elapsed or refilled:
                said = (str(r.get("label") or window)
                        + " has reset — the tank is full again")
                if jobs.any_open_parked():
                    events.append({
                        "source": "window_reset",
                        "said": said,
                        "meta": {"window": window},
                    })
                else:
                    _ledger({"source": "window_reset", "said": said,
                             "outcome": "declined: no job is open and parked"
                                        " at zero links -- the refill has"
                                        " nobody to be for"})
        if resets:
            seen[window] = {"resets_at": resets, "fraction": frac}
    st["resets_seen"] = seen
    return events


def _parse_comments(fragment: str) -> list:
    """Each comment's id and author, read out of Steam's `comments_html` the
    way a browser would, without a parser library: `id="comment_<n>"` marks
    where one comment starts, so the text between one such id and the next
    is that comment's own header and body -- however deep it sits among
    replies, since a nested reply simply opens the next window."""
    ids = list(re.finditer(r'id="comment_(\d+)"', fragment))
    out = []
    for i, m in enumerate(ids):
        start = m.end()
        end = ids[i + 1].start() if i + 1 < len(ids) else len(fragment)
        chunk = fragment[start:end]
        am = re.search(
            r'commentthread_author_link[^>]*>.*?<bdi>(.*?)</bdi>',
            chunk, re.S)
        author = _html_unescape(am.group(1)).strip() if am else None
        # Each comment also carries `at` (Steam's own
        # data-timestamp, seconds) and its first hundred characters of
        # text, read the way `comments.py` reads them, so a waking can carry
        # a few of the stranger's words. Absent (None / "") when the markup
        # has none -- never invented.
        sm = re.search(r'data-timestamp="(\d+)"', chunk)
        tm = re.search(r'class="commentthread_comment_text"[^>]*>(.*?)</div>',
                       chunk, re.S)
        text = ""
        if tm:
            text = re.sub(r"<br\s*/?>", " ", tm.group(1))
            text = re.sub(r"<[^>]+>", "", text)
            text = " ".join(_html_unescape(text).split())
        out.append({"id": int(m.group(1)), "author": author,
                    "at": int(sm.group(1)) if sm else None,
                    "text": _cut(text, 100)})
    return out


def _steam_fetch(item):
    """One poll of one item's comment thread, or a reason it did not happen.
    Never raises -- a bad night on Steam's end must not stop the room."""
    url = (STEAM_COMMENT_URL_TMPL.format(item_id=item["id"])
           + "?count=100&start=0")
    req = urllib.request.Request(
        url, headers={"User-Agent": "digital-ai-assistant"})
    try:
        with urllib.request.urlopen(req, timeout=STEAM_TIMEOUT_S) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return None, "429"
        return None, "steam answered " + str(exc.code)
    except Exception as exc:                      # network, timeout, bad JSON
        return None, type(exc).__name__ + ": " + str(exc)[:120]
    if not body.get("success"):
        return None, "steam answered success=false"
    return _parse_comments(body.get("comments_html") or ""), None


# Overridable seam for the test bench: real code never touches it, so a run
# over scratch never once reaches steamcommunity.com.
_steam_now = _steam_fetch


def _looked(per, said) -> None:
    """This item just looked, and this is what it found -- one value each,
    overwritten every time. Not a ledger line: a quiet look is the ordinary
    case and there are ninety-odd of them a day per item. The ledger keeps
    the notable ones, and this keeps the last one, which is what a person
    standing in front of the tab is actually asking about."""
    per["last_look"] = db.now()
    per["last_result"] = said


def _steam_item(st, item) -> list:
    """New comments on one Steam Workshop item since the id last remembered
    for THAT item, polled no more than every `STEAM_POLL_INTERVAL_S` on that
    item's own clock, regardless of how often `due()` is called or what any
    other item is doing -- the throttle up there is the turn loop's cadence,
    this one is Steam's, and it is per item so a busy mod never starves or
    throttles a quiet one. A 429 backs off harder still, on this item alone,
    and is written to the ledger rather than retried. The owner's own
    comments (`STEAM_IGNORE_AUTHOR`) count as seen like any other but never
    appear in `said` or count toward the waking.

    THE WATERMARK IS A SET OF SEEN IDS, NOT A HIGHEST-ID -- and that is a
    fix, not a preference. This sense first ran on `last_id` with an
    `id > last_id` test, on the belief that Steam comment gids rise with
    time. They do not: they are allocated from pools, and a live thread
    proved it -- an old comment carried a larger gid than the newest one,
    posted months later. The first seed took `max()`, swallowed the big gid,
    and from that moment EVERY comment on the thread was below the watermark:
    the sense polled faithfully, said "nothing new" every time, and was blind
    from birth, while a real question sat unanswered until it was found by
    hand. "Not in the set already seen" is the test that cannot be poisoned
    by an outlier, and it is what the Nexus sense already did."""
    item_id, label = item["id"], item["label"]
    per = st.setdefault("steam", {}).setdefault(
        item_id, {"seen_ids": None, "next_poll": 0.0})
    now = time.time()
    if now < float(per.get("next_poll") or 0):
        return []
    comments, error = _steam_now(item)
    if error == "429":
        per["next_poll"] = now + STEAM_BACKOFF_S
        # Said in hours now that the backoff is one. "240 minutes" is a
        # number a person has to do arithmetic on before it means anything.
        stood_down = _spell(STEAM_BACKOFF_S)
        _looked(per, "Steam said slow down, backing off " + stood_down)
        _ledger({"source": "steam_comment",
                 "meta": {"item": item_id, "label": label},
                 "outcome": "declined: " + label + " answered 429 -- backing"
                            " off " + stood_down + ", no tight-loop retry"})
        return []
    per["next_poll"] = now + STEAM_POLL_INTERVAL_S
    if error:
        _looked(per, "could not read " + label + ": " + error)
        _ledger({"source": "steam_comment",
                 "meta": {"item": item_id, "label": label},
                 "outcome": "declined: " + label + ": " + error})
        return []

    current_ids = {str(c["id"]) for c in comments if c.get("id") is not None}
    seen = per.get("seen_ids")
    if seen is None:
        # First ever poll of this item -- or the one-time crossing from the
        # poisoned `last_id` watermark this replaced, which arrives here the
        # same way: with no set yet. Either way, what is on the page now is
        # history, not news, and it does not wake the assistant. The old key
        # is taken off rather than left to look like state.
        migrated = "last_id" in per
        per.pop("last_id", None)
        per["seen_ids"] = sorted(current_ids)
        _looked(per, ("watermark rebuilt as a seen-set for " if migrated
                      else "first look at ") + label + ", "
                     + str(len(comments)) + " already there and counted as "
                     "history rather than news")
        return []

    seen_set = set(seen)
    # Page order is newest-first and is kept: with gids proven non-monotonic,
    # sorting by id would put an old pool's comment "newest", and the waking
    # names the newest.
    fresh = [c for c in comments
             if str(c.get("id")) not in seen_set
             and (c.get("author") or "") != STEAM_IGNORE_AUTHOR]
    per.pop("last_id", None)
    per["seen_ids"] = sorted(current_ids)
    if not fresh:
        _looked(per, "checked " + label + ", nothing new")
        return []
    newest = fresh[0]
    n = len(fresh)
    _looked(per, "woke " + home.NAME + ": " + str(n) + " new comment"
                 + ("" if n == 1 else "s") + " on " + label + ", newest from "
                 + str(newest.get("author") or "someone"))
    return [{
        "source": "steam_comment",
        "said": (str(n) + " new comment" + ("" if n == 1 else "s")
                 + " on " + label + "'s Steam Workshop item — newest from "
                 + str(newest.get("author") or "someone")),
        "meta": {"count": n, "newest_id": newest["id"], "item": item_id,
                 "label": label},
    }]


def _steam(st) -> list:
    """Every item in `STEAM_ITEMS`, each on its own clock. One item's due()
    call never waits on another's, and the events from all of them can land
    in the same waking -- one mute, `steam_comment`, covers all of them."""
    events = []
    for item in STEAM_ITEMS:
        events += _steam_item(st, item)
    return events


# The DOM walk, proven against the live page rather than derived from any
# API Nexus does not expose. It walks #comment-container > ol > li.comment,
# recursing into .comment-kids for replies, and reads each comment's id off
# the li's own DOM id.
NEXUS_EXTRACT_JS = """
() => {
    const results = [];
    function walk(li, parentId) {
        const id = li.id.replace('comment-', '');
        const nameLink = li.querySelector(':scope > .comment-head .comment-name a');
        const timeEl = li.querySelector(':scope > .comment-content time');
        const contentEl = li.querySelector(':scope > .comment-content .comment-content-text');
        results.push({
            id: id,
            parentId: parentId,
            author: nameLink ? nameLink.textContent.trim() : null,
            profileUrl: nameLink ? nameLink.href : null,
            dateEpoch: timeEl ? parseInt(timeEl.getAttribute('data-date'), 10) : null,
            dateText: timeEl ? timeEl.textContent.trim() : null,
            text: contentEl ? contentEl.textContent.trim() : null,
        });
        const kids = li.querySelector(':scope > .comment-kids');
        if (kids) {
            kids.querySelectorAll(':scope > li.comment').forEach(child => walk(child, id));
        }
    }
    document.querySelectorAll('#comment-container > ol > li.comment').forEach(li => walk(li, null));
    return results;
}
"""


def _nexus_fetch(item):
    """One poll of one mod's Nexus comments page, or a reason it did not
    happen. Never raises -- a bad night on Nexus's end, a missing browser or
    a Cloudflare challenge must not stop the room.

    WHY A REAL BROWSER, not requests/httpx: proven against the live site --
    Nexus sits behind Cloudflare bot management and a plain HTTP fetch gets a
    flat 403 even with ordinary headers. This drives real headless Chromium
    through Playwright instead, and does not re-litigate that choice. The
    Cloudflare-challenge title check and the #comment-container wait were
    proven the same way."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, ("Playwright isn't installed (pip install playwright; "
                       "playwright install chromium)")
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as exc:
                return None, ("Chromium isn't installed for Playwright: "
                               + type(exc).__name__ + ": " + str(exc)[:160])
            try:
                page = browser.new_page(user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    " (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"))
                page.goto(item["url"], timeout=NEXUS_TIMEOUT_MS,
                          wait_until="domcontentloaded")
                title = page.title()
                if ("just a moment" in title.lower()
                        or "attention required" in title.lower()):
                    return None, ("blocked by a Cloudflare challenge page"
                                   " (title: " + repr(title) + ")")
                try:
                    page.wait_for_selector("#comment-container",
                                            timeout=NEXUS_TIMEOUT_MS)
                except Exception:
                    return None, ("never found #comment-container (layout"
                                   " changed, wrong URL, or still blocked);"
                                   " title was " + repr(title))
                return page.evaluate(NEXUS_EXTRACT_JS), None
            finally:
                browser.close()
    except Exception as exc:                      # anything else Playwright throws
        return None, type(exc).__name__ + ": " + str(exc)[:160]


# Overridable seam for the test bench: real code never touches it, so a run
# over scratch never once launches a browser or reaches nexusmods.com.
_nexus_now = _nexus_fetch


def _nexus_item(st, item) -> list:
    """New comments on one Nexus mod page since the ids last remembered for
    THAT mod, polled no more than every `NEXUS_POLL_INTERVAL_S` on that
    mod's own clock -- same shape as `_steam_item`, not shared code with it
    (see the module docstring for why they must not touch).

    The watermark is a *set* of comment ids seen on the page, not a single
    highest id -- what this docstring once excused as a Nexus quirk ("not
    Steam's small monotonic integers") turned out to be the truth of Steam
    too, and `_steam_item` now carries the same shape after its max-id
    watermark was proven poisoned. "Not in the set already seen" is the only
    safe test on either platform.

    A waking fires per fresh comment, not bundled into one count-and-newest
    line like Steam: each one names its own author, its own mod and the
    first words of its own text, and carries its own comment id in `meta`
    so a task can join an event by id rather than by finding the mod's name
    in a sentence.

    A fetch failure is the one place this differs from Steam on purpose:
    loud, not spammy. The moment a mod starts failing it wakes the assistant
    once; while it keeps failing every poll after that only
    updates the last-look line with how long it has been down, and is
    written to the ledger as a decline rather than woken again -- a Cloudflare
    challenge that lasts all afternoon must say so once, not every hour."""
    mod_id, label = item["id"], item["label"]
    per = st.setdefault("nexus", {}).setdefault(
        mod_id, {"seen_ids": None, "next_poll": 0.0, "failing_since": None})
    now = time.time()
    if now < float(per.get("next_poll") or 0):
        return []

    comments, error = _nexus_now(item)

    if error:
        per["next_poll"] = now + NEXUS_BACKOFF_S
        was_failing = per.get("failing_since") is not None
        if not was_failing:
            per["failing_since"] = now
            said = "could not read " + label + "'s Nexus comments: " + error
            _looked(per, said)
            # No manual _ledger call here: this event flows back up through
            # _due() like any other, which writes its own "woke" ledger line
            # once -- a second one here would say the same thing twice.
            return [{
                "source": "nexus_comment",
                "said": said,
                "meta": {"item": mod_id, "label": label, "error": error},
            }]
        hours = int((now - per["failing_since"]) // 3600)
        duration = (str(hours) + " hour" + ("" if hours == 1 else "s")
                    if hours >= 1 else "under an hour")
        said = (label + " has been unreachable on Nexus for " + duration
                + " -- " + error)
        _looked(per, said)
        _ledger({"source": "nexus_comment",
                 "meta": {"item": mod_id, "label": label},
                 "outcome": "declined: still failing, no repeat waking -- "
                            + said})
        return []

    per["next_poll"] = now + NEXUS_POLL_INTERVAL_S
    per["failing_since"] = None

    current_ids = {str(c.get("id")) for c in comments if c.get("id") is not None}
    seen = per.get("seen_ids")
    if seen is None:
        # First ever poll of this mod: what is already there is history,
        # not news -- same rule as steam_comment's first poll.
        per["seen_ids"] = sorted(current_ids)
        _looked(per, "first look at " + label + " on Nexus, "
                     + str(len(comments)) + " already there and counted as"
                     " history rather than news")
        return []

    seen_set = set(seen)
    fresh = [c for c in comments
             if str(c.get("id")) not in seen_set
             and (c.get("author") or "") != NEXUS_IGNORE_AUTHOR]
    per["seen_ids"] = sorted(current_ids)
    if not fresh:
        _looked(per, "checked " + label + " on Nexus, nothing new")
        return []

    n = len(fresh)
    _looked(per, "woke " + home.NAME + ": " + str(n) + " new comment"
                 + ("" if n == 1 else "s") + " on " + label + " (Nexus)")
    events = []
    for c in fresh:
        author = c.get("author") or "someone"
        text = _cut(c.get("text") or "", 100)
        # This is the waking, and only the waking: the comment, who left it,
        # and where. What the assistant is meant to DO with it is not this
        # sense's job and is not built here -- the intent, for whoever builds
        # it next, is that a new Nexus comment wakes the assistant and
        # produces one line to the owner: the comment itself, a drafted
        # reply, and whether it looks like it needs digging into (a bug
        # report, say) before it is sent. It never dispatches a hand on its
        # own. That is the owner's standing rule (`comments.rules`), not a
        # gap in this code to quietly close.
        events.append({
            "source": "nexus_comment",
            "said": (author + " commented on " + label + "'s Nexus page"
                     + (" — \"" + text + "\"" if text else "")),
            "meta": {"item": mod_id, "label": label, "comment_id": c.get("id")},
        })
    return events


def _nexus(st) -> list:
    """Every mod in `NEXUS_ITEMS`, each on its own clock -- same shape as
    `_steam`. One mute, `nexus_comment`, covers all of them."""
    events = []
    for item in NEXUS_ITEMS:
        events += _nexus_item(st, item)
    return events


def _calendar(conn, st) -> list:
    """Open tasks whose own date has come.

    The watermark is the pair: `<task id>@<the date it carried>`. A task
    fires once for that date; changing the date makes a new pair and a new
    firing, and changing nothing means nothing fires again however often this
    is asked. Finished and dropped tasks are not due -- the date on a task
    somebody has already done is history.

    What the assistant is woken with names the project and the task, and
    says who it is for when the task says. It does the thing or reminds the
    person; the sense only knows that the time came."""
    fired = st.setdefault("calendar_fired", [])
    seen = set(fired)
    now = datetime.now(timezone.utc)
    events, held = [], 0
    try:
        rows = conn.execute(
            "SELECT t.id, t.title, t.at, t.needs, t.state, p.title AS project"
            "  FROM project_tasks t JOIN projects p ON p.id = t.project"
            " WHERE t.at IS NOT NULL AND t.state IN ('open', 'doing')"
            "   AND p.status = 'open'"
            " ORDER BY t.at").fetchall()
    except Exception:
        # A store that will not answer costs this look, not the watcher.
        return []
    for r in rows:
        key = str(r["id"]) + "@" + str(r["at"])
        if key in seen:
            continue
        try:
            when = datetime.fromisoformat(str(r["at"]))
        except ValueError:
            # Stored unreadable somehow: say so once and never trip on it
            # again, rather than throwing this look away every 30 seconds.
            fired.append(key)
            _ledger({"source": "calendar", "meta": {"task": r["id"]},
                     "outcome": ("declined: '" + str(r["title"]) + "' carries "
                                 + repr(r["at"]) + ", which is not a date I "
                                 "can read")})
            continue
        if when.tzinfo is None:
            when = when.astimezone()
        if when > now:
            continue
        if len(events) >= CALENDAR_AT_ONCE:
            held += 1
            continue
        fired.append(key)
        for_whom = (r["needs"] or "").strip()
        events.append({
            "source": "calendar",
            "said": ("'" + str(r["title"]) + "' on " + str(r["project"])
                     + " has come due"
                     + (" — it is for " + for_whom if for_whom else "")),
            "meta": {"task": r["id"], "project": r["project"],
                     "at": r["at"], "needs": for_whom or None,
                     "label": r["project"], "item": str(r["id"])},
        })
    if held:
        # No silent cap: what waited is still due and the next look has it.
        _ledger({"source": "calendar",
                 "outcome": ("declined: " + str(held) + " more date"
                             + ("" if held == 1 else "s")
                             + " came due in the same look and are held for "
                             "the next one -- still due, nothing dropped")})
    # The list is a watermark, not a history: the ledger is the history. Kept
    # to the newest few hundred so a file that is written to for years does
    # not grow without end.
    if len(fired) > 400:
        del fired[:len(fired) - 400]
    return events


def _still_for(conn):
    """Seconds since the last thing that happened in this room, or None if
    nothing ever has. Any row at all counts: every way the assistant can be
    woken leaves one behind -- a person's line is a `user` row, an angel's an
    `angel`, a hand coming home a `worker`, a waking of the watcher's own a
    `world`, a night a `dream`, and every turn it takes a row of its own kind
    (`home.SELF`). So "no row since" is the whole of "nothing happened",
    without having to enumerate it."""
    row = conn.execute("SELECT MAX(dt) AS d FROM rows").fetchone()
    when = row and row["d"]
    if not when:
        return None
    try:
        at = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - at).total_seconds())


def _quiet(conn, st) -> list:
    """The sense that fires because nothing did.

    At most one a day -- the day is marked when it fires, and a day that has
    had its quiet is done, even if the ceiling then declines it. Declined is
    an answer here as everywhere else.

    A brand-new room with no rows at all does not count as still: it is new,
    not quiet, and there is nobody yet for the line to be for."""
    if st.get("quiet_day") == _today():
        return []
    still = _still_for(conn)
    if still is None or still < QUIET_STILL_S:
        return []
    st["quiet_day"] = _today()
    hours = int(still // 3600)
    return [{
        "source": "quiet",
        "said": ("nothing has happened here for " + str(hours)
                 + " hours — no line from " + home.OWNER_NAME
                 + ", no hand home, no other"
                 " waking. There is nothing to report: I am simply awake."),
        "meta": {"still_hours": hours},
    }]


# --- the decision ------------------------------------------------------------


def _owner_here_recently(conn) -> bool:
    """Whether the owner has said something in the room within the last
    hour -- a `user` row, never an angel's. The waking still happens either
    way; a push is only for when the owner is not here to see it land."""
    row = conn.execute(
        "SELECT dt FROM rows WHERE kind = 'user' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return False
    try:
        when = datetime.fromisoformat(str(row["dt"]))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() < OWNER_RECENT_S


def due(conn):
    """Called by the turn loop when the room is idle. Returns a `woken` dict
    when the world has earned a waking, else None. Never raises: a broken
    sense must not stop the room, so it says so on the console and stands
    down until the next look."""
    try:
        with _LOCK:
            return _due(conn)
    except Exception:
        traceback.print_exc()
        return None


def _due(conn):
    st = _state()
    if time.time() - float(st.get("checked_at") or 0) < CHECK_EVERY_S:
        return None
    st["checked_at"] = time.time()

    cfg = config()
    muted = _live_mutes(st)

    def wants(name):
        return bool(cfg["sources"][name].get("on")) and name not in muted

    # Stop watching that means stop watching: a muted or off source is not
    # observed and its watermark stands still.
    events = []
    if wants("refusal"):
        events += _refusals(st)
    if wants("dream_missed"):
        events += _dreams(conn, st)
    if wants("quota_low"):
        events += _quota(st, cfg)
    if wants("window_reset"):
        events += _resets(st, cfg)
    if wants("steam_comment"):
        events += _steam(st)
    if wants("calendar"):
        events += _calendar(conn, st)
    if wants("nexus_comment"):
        events += _nexus(st)

    # Quiet is asked last and only into an empty hand: it fires because
    # nothing did, so anything else this look found is already the answer to
    # the question quiet asks. A look that found a refusal is not a still
    # room, whatever the clock says.
    if wants("quiet") and not events:
        events += _quiet(conn, st)

    if not events:
        _save_state(st)
        return None

    # Whatever arrived lands on the task that watches for it, as a notice
    # that stands until a person or the assistant checks it off -- whether or
    # not this look earns a waking. A thing that arrived must sit on the
    # task's page until somebody says so, not in a line of the assistant's
    # that the next line can bury. The tasks' side is
    # `projects.notice_from_event`; nothing here decides what it deserves.
    for ev in events:
        try:
            projects.notice_from_event(conn, ev)
        except Exception:
            traceback.print_exc()

    day = _day(st)
    ceiling = int(cfg["per_day"])
    if day["woke"] >= ceiling:
        # Seen and declined, said so. The watermarks have moved: declined is
        # an answer, not a postponement.
        day["declined"] += len(events)
        for ev in events:
            _ledger({"source": ev["source"], "said": ev["said"],
                     "meta": ev.get("meta") or {},
                     "outcome": "declined: the day's ceiling of "
                                + str(ceiling) + " is spent"})
        st["last_event"] = {"at": db.now(), "said": events[-1]["said"],
                            "outcome": "declined (ceiling)"}
        _save_state(st)
        return None

    # One waking gathers everything this look found, and counts once --
    # the ceiling counts wakings, not facts.
    day["woke"] += 1
    told = []
    for ev in events:
        row_id = db.add_row(conn, "world", ev["said"],
                            meta={"source": ev["source"], **ev["meta"]})
        db.unload(conn, [row_id])      # born out of reach: it passes through
        told.append({"source": ev["source"], "said": ev["said"],
                     "row": row_id, "meta": ev.get("meta") or {}})

    # The push is for when the owner isn't here to see the waking happen; it
    # is never the reason for the waking. Some sources never earn one at all --
    # their own fact never leaves the room, bundled or not.
    owner_here = _owner_here_recently(conn)
    push_events = [ev for ev in events if ev["source"] not in NO_PUSH_SOURCES]
    if not push_events:
        pushed = "withheld: no source in this waking pushes to the phone"
    elif owner_here:
        pushed = "withheld: the owner spoke in the last hour, this waking is for me"
    else:
        line = "The room noticed: " + "; ".join(ev["said"] for ev in push_events)
        sent = push.send(_cut(line, 500))
        wire = push.status()
        pushed = ("off" if wire == "off"
                  else "sent" if sent is None else "failed: " + sent)
    for t in told:
        if t["source"] in NO_PUSH_SOURCES:
            this_push = "withheld: " + t["source"] + " is waking-only, it never pushes"
        elif owner_here:
            this_push = "withheld: the owner spoke in the last hour, this waking is for me"
        else:
            this_push = pushed
        _ledger({"source": t["source"], "said": t["said"], "row": t["row"],
                 "meta": t.get("meta") or {},
                 "outcome": "woke (" + str(day["woke"]) + " of "
                            + str(ceiling) + " today)",
                 "push": this_push})

    st["last_event"] = {"at": db.now(), "said": events[-1]["said"],
                        "outcome": "woke"}
    _save_state(st)
    return {
        "by": "world",
        "events": told,
        "why": _cut("the room noticed: "
                    + "; ".join(ev["said"] for ev in events), 240),
        "narrate": False,
        "chain": 0,
        "count_today": day["woke"],
        "ceiling": ceiling,
        "push": pushed,
    }


# --- the assistant's word, and its view ---------------------------------------


def _rebaseline(name, st) -> None:
    """An unmuted sense starts watching from now. What happened while it was
    muted stays in its own ledgers, unwatched -- unmuting must not deliver a
    backlog the assistant asked not to watch."""
    if name == "refusal":
        try:
            lines = REFUSALS_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        st["refusals_lines"] = len(lines)
    elif name == "dream_missed":
        try:
            conn = db.connect()
            try:
                row = conn.execute("SELECT MAX(id) AS m FROM dreams").fetchone()
                st["last_dream_id"] = row["m"] or 0
            finally:
                conn.close()
        except Exception:
            pass
    elif name == "steam_comment":
        # Forget every item's watermark rather than fetch one: the next
        # successful poll of each reseeds silently, exactly like a
        # first-ever look, so nothing posted on any item while it was not
        # watching arrives as a backlog. One mute, all items -- there is no
        # per-item mute to unwind separately.
        steam_state = st.setdefault("steam", {})
        for item in STEAM_ITEMS:
            entry = steam_state.setdefault(item["id"], {})
            entry["seen_ids"] = None
            entry.pop("last_id", None)
    elif name == "nexus_comment":
        # Same idea as steam_comment: forget every mod's seen-ids set rather
        # than fetch, so the next successful poll of each reseeds silently,
        # exactly like a first-ever look. Also clears any failure streak, so
        # a mod muted mid-outage does not immediately re-wake the assistant
        # the moment it is unmuted.
        nexus_state = st.setdefault("nexus", {})
        for item in NEXUS_ITEMS:
            entry = nexus_state.setdefault(item["id"], {})
            entry["seen_ids"] = None
            entry["failing_since"] = None
    # quota_low and window_reset key themselves to the live windows and need
    # no baseline.


def _live_mutes(st) -> set:
    """The assistant's mutes with the lapsed ones taken off, and the state
    tidied to match. Called on every look, so an expired mute is no mute at
    all by the next one -- nobody does anything, and the sense is simply back.

    An expiry that will not parse is left alone and the mute stands: failing
    closed here means a muted sense stays muted, which is the harmless way
    round."""
    held = set(st.get("muted_by_her") or [])
    until = dict(st.get("mute_until") or {})
    now_utc = datetime.now(timezone.utc)
    for name in sorted(held):
        when = until.get(name)
        if not when:
            continue                 # an indefinite mute: the assistant's to lift
        try:
            at = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at <= now_utc:
            held.discard(name)
            until.pop(name, None)
            _rebaseline(name, st)
            _ledger({"source": name,
                     "outcome": "the mute lapsed of old age; the sense is "
                                "watching again from now"})
    # An expiry with no mute behind it is nothing.
    st["muted_by_her"] = sorted(held)
    st["mute_until"] = {k: v for k, v in until.items() if k in held}
    return held


def apply_her_word(mute, unmute) -> list:
    """The assistant's `watch` reply field, honoured. Returns the lines to
    say and keep. A name that is not a sense is said back rather than
    swallowed."""
    said = []
    with _LOCK:
        st = _state()
        held = _live_mutes(st)
        until = st["mute_until"]
        for name in mute or []:
            if name not in SOURCES:
                said.append(home.NAME + " asked to mute \"" + str(name)
                            + "\", which is not a sense; the senses are "
                            + ", ".join(SOURCES))
                continue
            if name not in held:
                held.add(name)
                lasts = LAPSING_MUTES.get(name)
                if lasts:
                    dies = (datetime.now(timezone.utc)
                            + timedelta(seconds=lasts))
                    until[name] = dies.isoformat(timespec="seconds")
                    said.append(home.NAME + " muted the " + name
                                + " sense; that mute lapses of old age at "
                                + until[name]
                                + " and the sense comes back on by itself")
                    _ledger({"source": name,
                             "outcome": "muted by " + home.NAME
                                        + " until " + until[name]})
                else:
                    said.append(home.NAME + " muted the " + name + " sense")
                    _ledger({"source": name, "outcome": "muted by " + home.NAME})
        for name in unmute or []:
            if name not in SOURCES:
                said.append(home.NAME + " asked to unmute \"" + str(name)
                            + "\", which is not a sense")
                continue
            if name in held:
                held.discard(name)
                until.pop(name, None)
                _rebaseline(name, st)
                said.append(home.NAME + " unmuted the " + name
                            + " sense; it watches from now")
                _ledger({"source": name, "outcome": "unmuted by " + home.NAME})
        st["muted_by_her"] = sorted(held)
        st["mute_until"] = {k: v for k, v in until.items() if k in held}
        _save_state(st)
    return said


def _every(seconds) -> str:
    """A poll interval in words, from this module's own constant. Read here
    rather than typed into a page, so changing the cadence changes what
    everything says about it without anyone remembering to."""
    said = _spell(seconds)
    if not said:
        return ""
    # "every an hour" is not a sentence; the one-hour case wants the bare
    # noun. Every other span reads straight off `_spell`.
    return "every hour" if said == "an hour" else "every " + said


def senses() -> dict:
    """Each sense's standing, and its cadence where it has one.

    The shape a task's trigger phrase is built from, wherever it is drawn or
    read. A sense with no cadence of its own says nothing about one rather
    than guessing -- most of them fire on the room's own events, not a
    clock, and claiming an interval for those would be an invention."""
    view = for_prompt()
    cadences = {"steam_comment": STEAM_POLL_INTERVAL_S,
                "nexus_comment": NEXUS_POLL_INTERVAL_S}
    out = {}
    for name, standing in (view.get("senses") or {}).items():
        out[name] = {
            "standing": standing,
            "on": standing == "on",
            "said": _every(cadences[name]) if name in cadences else None,
        }
    return out


def looks() -> dict:
    """What each watched item's last look found, by item id and, where the
    label is not already claimed by another sense's item, by label too.

    One value per item, overwritten every quiet look -- see `_looked`. An
    item that has never been looked at is simply absent, which is honest:
    "no look yet" and "looked and found nothing" are different facts and the
    tab must not draw them the same.

    The id key is the one a task should bind to (`sense_item`) and the one
    that never collides: Steam and Nexus can both watch a mod of the same
    name, so the label key here is convenience only, first sense to claim it
    wins, and a task that cares which of the two it means should
    use the id."""
    with _LOCK:
        st = _state()
    out = {}
    steam_per = st.get("steam") or {}
    for item in STEAM_ITEMS:
        s = steam_per.get(item["id"]) or {}
        if not s.get("last_look"):
            continue
        # `page` is where a person goes to see the thing itself -- the
        # item's own page, linked beside a notice on their tab.
        one = {"sense": "steam_comment", "item": item["id"],
               "label": item["label"], "at": s.get("last_look"),
               "said": s.get("last_result"),
               "every": STEAM_POLL_INTERVAL_S,
               "page": STEAM_PAGE_URL_TMPL.format(item_id=item["id"])}
        out[item["id"]] = one
        out.setdefault(item["label"], one)
    nexus_per = st.get("nexus") or {}
    for item in NEXUS_ITEMS:
        s = nexus_per.get(item["id"]) or {}
        if not s.get("last_look"):
            continue
        one = {"sense": "nexus_comment", "item": item["id"],
               "label": item["label"], "at": s.get("last_look"),
               "said": s.get("last_result"),
               "every": NEXUS_POLL_INTERVAL_S,
               "page": item["url"]}
        out[item["id"]] = one
        out.setdefault(item["label"], one)
    return out


def recent(limit=200) -> list:
    """The tail of the wakings ledger, newest last, as written.

    Never fatal: a ledger that cannot be read costs the Activity list its
    older lines and nothing else, and a tab that would not draw a project
    because a log file was locked would be the worse failure."""
    try:
        lines = LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return []
    out = []
    for raw in lines[-int(limit):]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def for_prompt() -> dict:
    """The watch block the assistant sees every turn. Data only; what it
    means is in its instructions. Quiet must be legible as quiet, so the
    counts are always here, zeros and all."""
    with _LOCK:
        st = _state()
        cfg = config()
        before = list(st.get("muted_by_her") or [])
        muted = _live_mutes(st)
        if sorted(before) != sorted(muted):
            _save_state(st)          # a mute died while this was being read
        until = st.get("mute_until") or {}
        day = st["days"].get(_today()) or {"woke": 0, "declined": 0}
        sources = {}
        for name in SOURCES:
            if name in muted:
                # A mute that will die of old age reads differently from one
                # that will not: the assistant can see, without asking,
                # which of its own decisions is still standing.
                sources[name] = ("muted by me until " + str(until[name])
                                 if until.get(name) else "muted by me")
            else:
                sources[name] = ("on" if cfg["sources"][name].get("on")
                                 else "off")
        from . import providers
        if providers.codex_only():
            for name in ("quota_low", "window_reset"):
                sources[name] = providers.CLAUDE_PAUSED
        return {
            "today": {"woke": day["woke"], "declined": day["declined"],
                      "ceiling": int(cfg["per_day"])},
            "senses": sources,
            "last": st.get("last_event"),
            "push": push.status(),
            "ledger": "data/wakings.jsonl",
        }


def main():
    print("the watcher")
    view = for_prompt()
    print("  today   " + str(view["today"]["woke"]) + " woke, "
          + str(view["today"]["declined"]) + " declined, ceiling "
          + str(view["today"]["ceiling"]))
    for name, standing in view["senses"].items():
        print("  sense   " + name.ljust(14) + standing)
    print("  push    " + view["push"])
    last = view.get("last")
    if last:
        print("  last    " + str(last.get("at")) + "  "
              + str(last.get("said") or "") + "  ("
              + str(last.get("outcome")) + ")")
    try:
        tail = LEDGER_PATH.read_text(encoding="utf-8").splitlines()[-5:]
        if tail:
            print("  ledger  last " + str(len(tail)) + " of "
                  + "data/wakings.jsonl")
            for raw in tail:
                print("    " + raw)
    except OSError:
        print("  ledger  empty")


if __name__ == "__main__":
    main()
