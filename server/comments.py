"""The assistant's hands on the mod comments: reading a thread it has been
woken about, and posting a reply the owner has said yes to.

The senses came first and they only ever answered one question -- *is there
something new* -- because that is all a waking needs. `steam_comment` and
`nexus_comment` carry a count, an author and a hundred characters, and then
the assistant is awake holding a line about a comment it cannot actually
read. That gap is what this module closes. It is the difference between being
told someone knocked and being able to open the door.

It is deliberately not clever: finishing the managing came before building
new reach. Two operations, both of them things a person does by hand every
day, done here so the assistant can do them inside one turn instead of asking
the owner to paste.

WHAT IS DELIBERATELY NOT HERE, and must not quietly grow:

* No polling. This module never runs on a clock; every call is because the
  assistant asked for it on a turn. The clocks belong to `watch.py` and there
  is exactly one of them per platform per mod, at the watcher's own cadence
  for that platform -- Steam's two hours, Nexus's day.
* No queue, no stored draft, no retry. A post is one call and one comment.
  If it fails it fails loudly and the words the assistant wrote are still in
  its reply where the owner can read them.
* No shaping of its text on the way out. `steam_post.check` refuses a bad
  draft rather than trimming it, and this module does not get to be cleverer
  than that -- the text it wrote is the text that goes, or nothing goes.
* Nothing about a reply is decided here. Whether a comment is worth
  answering, and what the answer is, is the assistant's judgement on a turn
  with the owner reading. This is the hand, not the head.

WHAT THIS MODULE MAY TOUCH, exactly:

* Steam reading -- the same unauthenticated render URL the sense already
  polls, `watch.STEAM_COMMENT_URL_TMPL`. No login, no cookie, no key.
* Nexus reading -- the same headless Chromium walk the sense already does,
  `watch._nexus_now`, so there is one Nexus fetch path in this room and not
  two that can drift.
* Steam posting -- `steam_post.post`, unchanged and un-wrapped: its
  allow-list of items, its sign-off rule, its length ceiling and its loud
  failure are the veto, and this module adds to them rather than softening
  any of them.
* Nexus posting -- REFUSED, and refused by name with the reason said out
  loud. Nexus has no posting path in this room: reading it is solved,
  writing to it is not. A missing capability that says nothing is how the
  assistant ends up believing it sent something it did not.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import unescape as _html_unescape
from pathlib import Path

from . import db, quoted, steam_post, watch
from . import home

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = home.DATA / "comment_posts.jsonl"
RULES_PATH = home.DATA / "comment_rules.json"

# THE STANDING RULE: answer no one automatically -- only report that a
# comment appeared, until the owner and the
# assistant have learned, one comment at a time, which comments deserve which
# action.
#
# So the default is HOLD. The assistant reads, judges, tells the owner, and may
# draft -- but nothing it writes reaches a real page until the owner has said
# yes to that comment. It is here in code and not only in the prompt because a
# rule that lives in a prompt is one confused turn from being forgotten, and
# the thing being prevented is public and under the owner's name.
#
# It is deliberately not the assistant's to lift. `data/comment_rules.json` is
# written by the owner or by an angel session at the owner's word; there is
# no op in the assistant's schema that touches this. That asymmetry IS the rule: a hold the assistant
# could lift itself is a hold that lasts until it has a good reason, and "I had
# a good reason" is exactly what the owner wants to see first, one comment at
# a time, until both know which comment deserves which action.
HOLD_DEFAULT = True


def rules() -> dict:
    """The standing rule, read off disk every time it is asked for.

    Off disk and never cached: the hold is lifted by editing a file, and a
    lift that needed a restart to take effect would be a lift nobody could
    use in the middle of the conversation where it was decided."""
    out = {"may_post": not HOLD_DEFAULT,
           "why": (home.OWNER_NAME + "'s standing rule: tell "
                   + home.OWNER_NAME + " a comment arrived, and do not answer it"
                   " automatically -- until "
                   + home.OWNER_NAME + " and " + home.NAME + " have learned,"
                   " one comment at a time, which comment deserves which"
                   " action.")}
    try:
        got = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if isinstance(got, dict):
        if isinstance(got.get("may_post"), bool):
            out["may_post"] = got["may_post"]
        if isinstance(got.get("why"), str) and got["why"].strip():
            out["why"] = got["why"].strip()
    return out

# Every post the assistant makes, on every platform, in one day. A backstop in
# code and not a line in a prompt, because a rule the assistant is asked to
# remember is a rule that is one confused turn from being forgotten -- and the
# failure mode here is public, under the owner's name, on the owner's own mod
# page. Five is not a productivity target. It is the number above which
# something has clearly gone wrong and should stop rather than continue
# politely.
#
# Across both platforms together, not per mod: the thing being limited is
# how much of the assistant can reach the outside world in a day, and that
# does not get larger because another mod is published.
POSTS_PER_DAY = 5

# How many comments one read hands back by default. A thread of a hundred is
# not a working set, it is a way to spend the whole window on one turn.
READ_LIMIT = 15
READ_MAX = 40

STEAM_TIMEOUT_S = 20

# The owner's own account name on each platform. Read off the watcher rather
# than typed again here: two places holding the same name is two places to get
# it wrong.
HIS_NAMES = {watch.STEAM_IGNORE_AUTHOR, watch.NEXUS_IGNORE_AUTHOR}

STEAM_ITEM_URL = ("https://steamcommunity.com/"
                  "sharedfiles/filedetails/?id={item_id}")


class Unknown(Exception):
    """A mod or a place the assistant does not have. Names what it does have."""


class Refused(Exception):
    """Not allowed, and nothing was done. The assistant's own rules and the
    owner's, never a failure of the network -- those are `Failed`."""


class Failed(Exception):
    """It was allowed and it did not work. Loud on purpose: a read that
    silently returns nothing looks exactly like a quiet thread, and a post
    that silently does nothing looks exactly like a post."""


# ---------------------------------------------------------------------------
# The mods, joined from the watcher's own two lists so there is one place a
# mod is added and this follows.
# ---------------------------------------------------------------------------

def mods() -> list:
    """Every mod the assistant can read, and where. Built by joining the
    watcher's `STEAM_ITEMS` and `NEXUS_ITEMS` on their labels rather than by
    keeping a third list here -- when another mod is published it is added to
    the sense, and this follows without anyone remembering that it had to.

    A mod present on only one platform is not an error and is not hidden: it
    comes back with the other place `None`, and asking to read the place it
    does not have says so."""
    by_name = {}
    for item in watch.STEAM_ITEMS:
        by_name.setdefault(item["label"], {"mod": item["label"],
                                           "steam": None, "nexus": None})
        by_name[item["label"]]["steam"] = item["id"]
    for item in watch.NEXUS_ITEMS:
        by_name.setdefault(item["label"], {"mod": item["label"],
                                           "steam": None, "nexus": None})
        by_name[item["label"]]["nexus"] = item["id"]
        by_name[item["label"]]["nexus_url"] = item["url"]
    return [by_name[k] for k in sorted(by_name)]


def _norm(name) -> str:
    """Loose enough that "lantern mod", "LanternMod" and "Lantern Mod" are
    the same mod, strict enough that it is still a name and not a guess. The
    assistant writes these into an answer by hand; a mod it cannot name
    because of a space is a mod it cannot reply about."""
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def find(name) -> dict:
    """One mod, by the name the assistant used. Never by id -- ids are the
    sense's business and the assistant's is the name, a condition this was
    built under."""
    want = _norm(name)
    if not want:
        raise Unknown("no mod named; the watched mods are "
                      + ", ".join(m["mod"] for m in mods()))
    for m in mods():
        if _norm(m["mod"]) == want:
            return m
    raise Unknown(str(name) + " is not one of the watched mods, which are "
                  + ", ".join(m["mod"] for m in mods()))


def _place(mod, where) -> str:
    where = str(where or "").strip().lower()
    if where not in ("steam", "nexus"):
        raise Unknown("where must be steam or nexus, not " + repr(where))
    if not mod.get(where):
        raise Unknown(mod["mod"] + " is not watched on " + where)
    return where


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

# Steam's own comment fragment, read the way `watch._parse_comments` reads it
# -- one window per `id="comment_<n>"` -- but carrying the parts a reply
# needs and a count does not: the words, when they were said, and a link back
# to the person who said them. Confirmed against a live fragment rather than
# assumed from a doc.
_STEAM_ID_RE = re.compile(r'id="comment_(\d+)"')
_STEAM_AUTHOR_RE = re.compile(
    r'commentthread_author_link[^>]*href="([^"]*)"[^>]*>.*?<bdi>(.*?)</bdi>',
    re.S)
_STEAM_STAMP_RE = re.compile(r'data-timestamp="(\d+)"')
_STEAM_TEXT_RE = re.compile(
    r'class="commentthread_comment_text"[^>]*>(.*?)</div>', re.S)


def _text_of(html_fragment) -> str:
    """The words a person actually typed, out of Steam's markup. Line breaks
    are kept as line breaks because a bug report is usually a list, and a
    report flattened to one line is a report that is harder to answer."""
    out = re.sub(r"<br\s*/?>", "\n", html_fragment or "")
    out = re.sub(r"<[^>]+>", "", out)
    out = _html_unescape(out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n\s*\n\s*\n+", "\n\n", out)
    return out.strip()


def _steam_body(mod, limit) -> dict:
    """The raw answer from Steam's render endpoint. Split out so the bench
    has a seam to stand in front of -- the same shape `watch._steam_now`
    wears, and for the same reason: a bench run must never once reach
    steamcommunity.com."""
    url = (watch.STEAM_COMMENT_URL_TMPL.format(item_id=mod["steam"])
           + "?count=" + str(limit) + "&start=0")
    req = urllib.request.Request(
        url, headers={"User-Agent": "digital-ai-assistant"})
    with urllib.request.urlopen(req, timeout=STEAM_TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


_steam_now = _steam_body


def _steam_read(mod, limit) -> dict:
    try:
        body = _steam_now(mod, limit)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise Failed("Steam said slow down (429) on " + mod["mod"]
                         + "; nothing was read. The sense is on its own"
                         " two-hour clock and is not affected.")
        raise Failed("Steam answered " + str(exc.code) + " reading "
                     + mod["mod"] + "; nothing was read")
    except Exception as exc:
        raise Failed("could not reach Steam for " + mod["mod"] + " ("
                     + type(exc).__name__ + "); nothing was read")
    if not body.get("success"):
        raise Failed("Steam answered success=false reading " + mod["mod"])

    fragment = body.get("comments_html") or ""
    marks = list(_STEAM_ID_RE.finditer(fragment))
    out = []
    for i, m in enumerate(marks):
        chunk = fragment[m.end():
                         (marks[i + 1].start() if i + 1 < len(marks)
                          else len(fragment))]
        am = _STEAM_AUTHOR_RE.search(chunk)
        sm = _STEAM_STAMP_RE.search(chunk)
        tm = _STEAM_TEXT_RE.search(chunk)
        when = None
        if sm:
            when = datetime.fromtimestamp(
                int(sm.group(1)), timezone.utc).isoformat()
        out.append({
            "id": m.group(1),
            "author": (_html_unescape(am.group(2)).strip() if am else None),
            "profile": (am.group(1) if am else None),
            "at": when,
            "text": _text_of(tm.group(1) if tm else ""),
        })
    return {
        "mod": mod["mod"],
        "where": "steam",
        "total": body.get("total_count"),
        "showing": len(out),
        "page": STEAM_ITEM_URL.format(item_id=mod["steam"]),
        "comments": out,
    }


def _nexus_read(mod, limit) -> dict:
    """One walk of the mod's Nexus posts tab, through the sense's own fetch.

    `watch._nexus_now` is the seam the bench overrides, so a bench run never
    launches a browser here either -- and, more to the point, there is one
    Nexus fetch in this room rather than two that can drift apart when Nexus
    next changes its markup."""
    item = {"id": mod["nexus"], "label": mod["mod"], "url": mod["nexus_url"]}
    got, error = watch._nexus_now(item)
    if error:
        raise Failed("could not read " + mod["mod"] + " on Nexus: " + error)
    out = []
    for c in got or []:
        when, epoch = None, None
        if c.get("dateEpoch"):
            try:
                epoch = int(c["dateEpoch"])
                when = datetime.fromtimestamp(
                    epoch, timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                when, epoch = None, None
        out.append({
            "id": str(c.get("id")),
            "reply_to": (str(c["parentId"]) if c.get("parentId") else None),
            "author": c.get("author"),
            "profile": c.get("profileUrl"),
            "at": when or c.get("dateText"),
            "epoch": epoch,
            "text": (c.get("text") or "").strip(),
        })
    # Nexus hands them back in TREE order -- each top-level comment followed
    # by its own replies -- so the list is not in time order and reversing it
    # does not make it so. Sorted by the stamp instead, newest first, which
    # is the order Steam's render already gives and the order a person
    # standing at the tab is asking for. The thread is not lost by doing
    # this: every reply still carries `reply_to`, which is the honest way to
    # hold a shape, rather than a list order that only looks like one.
    # Anything without a readable stamp sorts last rather than pretending to
    # be new.
    out.sort(key=lambda c: (c.get("epoch") is not None, c.get("epoch") or 0),
             reverse=True)
    for c in out:
        c.pop("epoch", None)
    return {
        "mod": mod["mod"],
        "where": "nexus",
        "total": len(out),
        "showing": min(len(out), limit),
        "page": mod["nexus_url"],
        "comments": out[:limit],
    }


def read(mod_name, where, limit=READ_LIMIT, mine=False) -> dict:
    """One mod's comment thread in one place, newest first.

    `mine` false -- the ordinary case -- drops the owner's own comments from
    what comes back. They are not news and they are not the assistant's to
    answer; the sense already ignores them for the same reason. `mine` true
    keeps them, which is what is wanted when the question is "what did the
    owner already say to this person", and that is a real question before
    writing a reply.

    Raises `Unknown` if the mod or the place is not watched, and `Failed` if the
    read did not happen. It never returns an empty thread to mean a failed
    one."""
    mod = find(mod_name)
    where = _place(mod, where)
    try:
        limit = max(1, min(int(limit or READ_LIMIT), READ_MAX))
    except (TypeError, ValueError):
        limit = READ_LIMIT
    got = _steam_read(mod, limit) if where == "steam" else _nexus_read(mod, limit)
    if not mine:
        kept = [c for c in got["comments"]
                if (c.get("author") or "") not in HIS_NAMES]
        got["his_own_hidden"] = len(got["comments"]) - len(kept)
        got["comments"] = kept
        got["showing"] = len(kept)

    # Borrowed words, marked as borrowed -- the same rule the web reach lives
    # under and for the same reason: the boundary belongs in the data, not in
    # the assistant's care. Fenced PER COMMENT, not
    # once around the whole thread, because a single fence around a list is a
    # list somebody can write a fake entry into. Every line of every body
    # carries the mark, and the author, the id and the date beside it are the
    # parser's words rather than the commenter's, so nothing inside a comment
    # can claim to be a different one.
    for c in got["comments"]:
        source = (str(c.get("author") or "someone") + " on "
                  + got["mod"] + "'s " + got["where"] + " page")
        c["quoted"] = quoted.fence(c.get("text") or "", source, "A COMMENT")
        smells = quoted.smells_off(c.get("text") or "")
        if smells:
            # Never a filter and never a reason to hold anything back. It is
            # something the assistant can tell the owner about, and a comment
            # on the owner's own mod page trying it is exactly that.
            c["reads_like_an_instruction"] = smells
    return got


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

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


def posted_today() -> list:
    """Everything that actually went out today, read back off the ledger.

    Off the ledger and not out of memory, on purpose: a restart must not
    hand the assistant five fresh posts, and the room restarts often enough
    that an in-process counter would be a cap that resets whenever it is most
    likely to matter."""
    try:
        lines = LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    today, out = _today(), []
    for line in lines:
        try:
            one = json.loads(line)
        except ValueError:
            continue
        if one.get("posted") and str(one.get("at", ""))[:10] == today:
            out.append(one)
    return out


def post(mod_name, where, text) -> dict:
    """One comment, once, on one of the owner's mods. Checked, sent, written
    down.

    Everything that can refuse does so before anything is opened, cheapest
    first: the mod, the place, the day's count, then `steam_post`'s own three
    (the item, the sign-off, the ceiling). A refusal at any of those means
    nothing was sent, and says why in words the owner can read.

    Nexus is refused here and not further down, because there is nothing
    further down to refuse it -- no posting path to Nexus exists in this
    room. It is named as a missing hand rather than a failed one."""
    mod = find(mod_name)
    where = _place(mod, where)

    # The standing rule first, before the platform, before the day's count,
    # before anything is opened. It is the cheapest refusal and the most
    # important one: everything below this line is machinery for sending, and
    # while the hold stands the answer to "should this be sent" is the owner's
    # and not the assistant's.
    standing = rules()
    if not standing.get("may_post"):
        raise Refused(
            "held: " + standing["why"] + " Nothing was sent. The draft is not"
            " lost -- it is in the reply, where " + home.OWNER_NAME + " can"
            " read it, change it, or say to send it. Whether a comment may be"
            " answered is written down in data/comment_rules.json, by "
            + home.OWNER_NAME + " or by an angel session at "
            + home.OWNER_NAME + "'s word -- never by " + home.NAME + ".")

    if where == "nexus":
        raise Refused(
            home.NAME + " can read " + mod["mod"] + " on Nexus but cannot"
            " post there: this room has no Nexus posting path at all. Nexus"
            " reading goes through a headless browser on a public page;"
            " posting would need " + home.OWNER_NAME + "'s account, and that"
            " has not been built or asked for. The draft stands -- "
            + home.OWNER_NAME + " posts it by hand, and it is worth saying"
            " that this is the only part still by hand.")

    already = posted_today()
    if len(already) >= POSTS_PER_DAY:
        raise Refused(
            "that would be comment number " + str(len(already) + 1)
            + " today and the ceiling is " + str(POSTS_PER_DAY)
            + " across both platforms. Nothing was sent. This is a backstop,"
            " not a budget: five public comments under " + home.OWNER_NAME
            + "'s name in one day means something has gone wrong, and it"
            " should stop rather than carry on politely. Already today: "
            + "; ".join(str(a.get("mod")) + " (" + str(a.get("where")) + ")"
                        for a in already))

    text = str(text or "")
    try:
        steam_post.check(mod["steam"], text)
    except steam_post.Refused as exc:
        # The poster's own rule refused it. Written down even though nothing
        # was sent: a draft that keeps getting refused for its sign-off is a
        # thing the assistant should be able to see happening.
        _ledger({"posted": False, "mod": mod["mod"], "where": where,
                 "refused": str(exc), "chars": len(text)})
        raise Refused(str(exc))

    try:
        done = steam_post.post(mod["steam"], text)
    except steam_post.NotLoggedIn as exc:
        _ledger({"posted": False, "mod": mod["mod"], "where": where,
                 "failed": str(exc), "chars": len(text)})
        raise Failed(str(exc))

    out = {
        "posted": True,
        "mod": mod["mod"],
        "where": where,
        "chars": done["chars"],
        "page": done["page"],
        "left_today": POSTS_PER_DAY - (len(already) + 1),
    }
    _ledger(dict(out, text=text))
    # Also a line in the WAKINGS ledger, because that is the one the Projects
    # tab's Activity reads. Without it the tab shows the sense finding a
    # comment and then nothing -- the other half belongs there too: found
    # one, replied. The sense's source name and the item's
    # id/label make it land on the right task, same matching as a waking.
    try:
        watch._ledger({
            "source": "steam_comment",
            "meta": {"item": mod["steam"], "label": mod["mod"]},
            "outcome": (home.NAME + " posted a reply on " + mod["mod"] + " ("
                        + str(done["chars"]) + " characters)"),
        })
    except Exception:
        pass          # the tab's log must never cost a post its answer
    return out


# ---------------------------------------------------------------------------
# What a turn is handed back
# ---------------------------------------------------------------------------

def summary(mod_name=None) -> dict:
    """Where things stand, without reading anything from outside.

    Cheap on purpose -- it is the line a turn can carry every time without
    another page fetch: what is being watched, when each place last looked
    and what it found (the sense's own words, not a new read), and how many
    posts are left today."""
    looks = watch.looks()
    standing = rules()
    # `can_post` says what is actually true right now, not what the code is
    # capable of. While the hold stands, Steam is False -- because a turn that
    # reads "steam: true" and then gets refused is a turn that learns the rule
    # by bumping into it, which is the expensive way.
    out = {"mods": [],
           "posts_left_today": POSTS_PER_DAY - len(posted_today()),
           "rule": standing["why"],
           "may_post": bool(standing.get("may_post")),
           "can_post": {"steam": bool(standing.get("may_post")),
                        "nexus": False}}
    for m in mods():
        if mod_name and _norm(m["mod"]) != _norm(mod_name):
            continue
        one = {"mod": m["mod"], "places": {}}
        for where, key in (("steam", "steam"), ("nexus", "nexus")):
            if not m.get(key):
                continue
            seen = looks.get(m[key]) or {}
            one["places"][where] = {
                "last_look": seen.get("at"),
                "last_result": seen.get("said"),
                "every": watch._every(seen.get("every")),
            }
        out["mods"].append(one)
    return out


def main():
    """`python -m server.comments read <mod> <steam|nexus> [limit]`

    Reading only. Posting from a command line is not offered and is not an
    oversight: a comment goes out because the assistant wrote it on a turn
    and the owner said yes, and a shell is the one place that whole shape is
    missing.
    `server.steam_post` is still there for the deliberate one-off."""
    import sys
    argv = sys.argv[1:]
    if len(argv) >= 2 and argv[0] == "read":
        limit = int(argv[3]) if len(argv) > 3 else READ_LIMIT
        got = read(argv[1], argv[2], limit)
        print(got["mod"] + " on " + got["where"] + " -- "
              + str(got["showing"]) + " of " + str(got["total"]) + " shown")
        for c in got["comments"]:
            print("\n[" + str(c["id"]) + "] " + str(c["author"])
                  + "  " + str(c["at"]))
            print(c["text"])
        return
    if argv and argv[0] == "standing":
        print(json.dumps(summary(), indent=1))
        return
    print(main.__doc__.strip().splitlines()[0])
    print("mods: " + ", ".join(m["mod"] for m in mods()))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
