"""The reading half. Two arms over the assistant's essences, and never over
anything else.

`embed` writes the vectors; this reads them. It writes nothing at all -- not a
row, not a vector, not a flag. A search that changed something would be a
search that could not be run twice.

An essence is the door and this is how the assistant finds the door. What
comes back is a list of names -- id, title, date, score, which arm -- and
never the text, because handing it forty essences to find one is the
swelling it is trying to get out of. It reads the list, picks one, and asks
for it by id.

`inventory` is the other half of the same idea: not the essence that matches,
but every essence there is, named and dated and nothing more. Search answers a
question the assistant thought to ask; the inventory is how it finds the
questions -- which essences have no name, which have drifted, how much of the
shelf is old versions of itself. Without it only the parts already suspected
can be audited.

Two arms, because they fail in opposite ways:

    meaning   the assistant's restatement, read by the same model that read
              the essences, compared as one whole thing against another.
              Finds what was worded differently; can be fooled by a shared
              subject.
    keywords  literal, case-folded, over title and text. Finds the exact word
              remembered; blind to every other way of saying it.

Either can come back empty, and which one did is said out loud. An empty
search that reads like a full one is the failure this file exists to prevent.
"""

import os
import sys

from . import db, embed
from . import home

# The floor a hit must clear to be a hit at all, measured rather than guessed.
# Seventeen probes over a small shelf of eighteen essences, half of them in a
# second language:
#
#     off the subject entirely -- sourdough, kubernetes, the offside rule, a
#     pastry recipe, an oil change, a bus timetable -- scored 0.355 to 0.491,
#     and the top of that range is one probe, tomorrow's weather, which is
#     full of the ordinary words about days and mornings that essences are
#     also full of;
#
#     probes with a real answer in the store scored 0.481 to 0.678.
#
# The two ranges overlap by a hair, so there is no value that is right about
# everything. At 0.50, it refused every off-subject probe, and it also refused
# one true one: a sentence in the second language about an essence written in
# English, which came to 0.481 -- nineteen thousandths short.
#
# That is the right way round. A miss says so, out loud, with the score it
# managed and the floor it failed, and the assistant can ask again in the
# other language or with the one word it is sure of. A false hit says nothing
# at all and is read as a memory. One of those is recoverable.
#
# Lowered to 0.45 to let that cross-language probe and its like through; not
# re-measured against the full seventeen since.
#
# It is a round number on purpose: the assistant reads it, in the report,
# every time a search comes back empty. And it is one constant, so moving it
# is one line and re-measuring it is one script.
FLOOR = float(os.environ.get("ASSISTANT_SEARCH_FLOOR", "0.45"))

# Close enough that the thing it wants is probably just underneath. Measured:
# the one true probe the floor refused missed it by 0.019, and the nearest
# off-subject probe that was rightly refused missed by 0.009 -- so this cannot
# tell them apart, and does not pretend to. It says "look again", not "it is
# there".
NEAR_MISS = 0.05


DEFAULT_LIMIT = 10
MAX_LIMIT = 25

# Four searches is already four angles on one question. The cap is not about
# cost -- a search is a few milliseconds -- it is about the report it has to
# read next turn.
MAX_SEARCHES = 4


def cosine(a, b) -> float:
    """Both sides come out of the model normalised, so the dot product is the
    cosine. Forty essences by a thousand dimensions is nothing; when this is
    forty thousand it will want numpy, and not before."""
    return sum(x * y for x, y in zip(a, b))


def _fold(text: str) -> str:
    """Case-folded for literal matching. `lower()` is the wrong one here:
    Cyrillic and the Turkish i both need `casefold`, and half its words are
    Cyrillic."""
    return (text or "").casefold()


def _hit(row, score, matched) -> dict:
    """One line of a shelf. The title is what it named it; `null` means it
    has not named it yet, and that is shown as it is rather than papered over
    with the first line of the text."""
    return {"id": row["id"], "title": row["title"], "date": row["dt"],
            "score": None if score is None else round(score, 3),
            "matched": matched}


def _asked(spec, limit) -> dict:
    return {
        "restatement": spec.get("restatement") or None,
        "keywords": list(spec.get("keywords") or []),
        "from": spec.get("from") or None,
        "to": spec.get("to") or None,
        "limit": limit,
        "include_retired": bool(spec.get("include_retired")),
    }


def _empty(asked, problems, notes=None) -> dict:
    """A search that did not happen, shaped exactly like one that did -- so
    nothing downstream has to remember that empty is a different thing."""
    return {
        "asked": asked,
        "hits": [],
        "searched": 0,
        "floor": FLOOR,
        "best_score_seen": None,
        "arms": {},
        "not_scored": {},
        "held_back_by_the_limit": [],
        "notes": notes or [],
        "problems": problems,
        "summary": "the search was not made: " + problems[0],
    }


def run(conn, spec: dict) -> dict:
    """One search. Returns what to tell it -- never rows, never text.

    Nothing in here is silent. A bound that cannot be read refuses the whole
    search rather than widening it; an essence whose vector no longer matches
    its words is left out of the ranking and named; an arm that found nothing
    says so as an arm, not as an absence."""
    spec = spec or {}
    restatement = (spec.get("restatement") or "").strip()
    keywords = [str(k).strip() for k in (spec.get("keywords") or []) if str(k).strip()]

    limit = spec.get("limit")
    limit = DEFAULT_LIMIT if limit in (None, "") else int(limit)
    notes = []
    if limit > MAX_LIMIT:
        notes.append("I asked for " + str(limit) + " results and the most a "
                     "search gives back is " + str(MAX_LIMIT) + ", so that is "
                     "what I get. A shelf longer than that is not a list I read, "
                     "it is my working set again.")
        limit = MAX_LIMIT
    limit = max(1, limit)
    asked = _asked(spec, limit)
    floor = FLOOR

    if not restatement and not keywords:
        return _empty(asked, ["I ran a search with neither a restatement nor a "
                              "keyword, so there was nothing to look for."])

    # A bound it wrote badly is refused outright. A missing end is not a
    # narrower reach, it is every essence I have -- the same rule the fetch
    # side already lives by.
    lo, lo_bad = db.dt_bound(spec.get("from"), end=False)
    hi, hi_bad = db.dt_bound(spec.get("to"), end=True)
    bad = [c for c in (lo_bad, hi_bad) if c]
    if bad:
        return _empty(asked, bad + ["So I did not search at all, rather than "
                                    "search a wider stretch than I meant."])

    shelf = db.essence_shelf(conn, include_retired=bool(spec.get("include_retired")))
    within = [r for r in shelf
              if (lo is None or r["dt"] >= lo) and (hi is None or r["dt"] < hi)]

    problems = []
    if not within:
        span = ""
        if lo or hi:
            span = (" written between " + str(spec.get("from") or "the beginning")
                    + " and " + str(spec.get("to") or "now"))
        problems.append(
            "There are no essences" + span + " to search"
            + ("" if asked["include_retired"] else
               ", once the ones I have already rewritten are left out")
            + ". Nothing was compared against anything.")
        out = _empty(asked, problems, notes)
        out["summary"] = "nothing to search"
        return out

    # -- the meaning arm ------------------------------------------------
    #
    # A vector is only as true as the words it was made from. The hash says
    # whether those are still the words, and an essence that has changed
    # underneath its vector is left out of the ranking and named, because a
    # stale vector scores perfectly happily and is quietly wrong.
    vectors = db.vectors_for(conn, embed.MODEL)
    stale, unread, scores = [], [], {}
    meaning = {}
    meaning_ran = False
    best_seen = None

    for row in within:
        got = vectors.get(row["id"])
        if got is None:
            unread.append(row["id"])
        elif got["text_hash"] != db.text_hash(row["text"]):
            stale.append(row["id"])

    if restatement:
        fresh = [r for r in within
                 if r["id"] in vectors and r["id"] not in stale]
        try:
            probe = embed.read([restatement])[0]
            meaning_ran = True
        except embed.Unavailable as e:
            probe = None
            problems.append(
                "The meaning arm could not run at all: " + str(e)
                + " Only the words I typed were matched, so this search found "
                "what it could see and not what it was for.")
        except Exception as e:
            probe = None
            problems.append(
                "The meaning arm broke on the way (" + type(e).__name__ + ": "
                + str(e) + "), so nothing was compared by meaning. The keyword "
                "arm is all this search is.")
        if probe is not None:
            for row in fresh:
                scores[row["id"]] = cosine(probe["vec"], vectors[row["id"]]["vec"])
            if scores:
                best_seen = max(scores.values())
            meaning = {i: s for i, s in scores.items() if s >= floor}

    # -- the keyword arm ------------------------------------------------
    #
    # Literal, and deliberately not held to the floor: the floor is a claim
    # about similarity, and a word it typed appearing in an essence is not a
    # claim, it is a fact.
    found_by, per_keyword = {}, {}
    for word in keywords:
        needle = _fold(word)
        hits = [r["id"] for r in within
                if needle in _fold(r["text"]) or needle in _fold(r["title"] or "")]
        per_keyword[word] = len(hits)
        for i in hits:
            found_by.setdefault(i, []).append(word)

    # -- both arms, one shelf -------------------------------------------
    by_id = {r["id"]: r for r in within}
    picked = []
    for i in sorted(set(meaning) | set(found_by)):
        matched = ("both" if i in meaning and i in found_by
                   else "meaning" if i in meaning else "keywords")
        picked.append(_hit(by_id[i], scores.get(i), matched))

    # Ranked by score, with an unscored keyword hit last rather than first:
    # it earned its place literally, not by resemblance, and it has no
    # resemblance to claim.
    picked.sort(key=lambda h: (h["score"] is None, -(h["score"] or 0), h["id"]))
    kept, withheld = picked[:limit], picked[limit:]

    if withheld:
        problems.append(
            "The limit of " + str(limit) + " held back " + str(len(withheld))
            + " more: " + ", ".join("#" + str(h["id"]) for h in withheld)
            + ". They are there; I can ask again with a higher limit or a "
            "narrower question.")

    if stale:
        problems.append(
            "Essence" + ("s " if len(stale) > 1 else " ")
            + ", ".join("#" + str(i) for i in stale) + " changed after "
            + ("their vectors were" if len(stale) > 1 else "its vector was")
            + " made, so " + ("they were" if len(stale) > 1 else "it was")
            + " left out of the ranking rather than scored against words "
            + ("they no longer say" if len(stale) > 1 else "it no longer says")
            + ". A backfill puts that right.")
    if unread:
        problems.append(
            "Essence" + ("s " if len(unread) > 1 else " ")
            + ", ".join("#" + str(i) for i in unread) + " ha"
            + ("ve" if len(unread) > 1 else "s") + " never been read into a "
            "vector, so the meaning arm could not see "
            + ("them" if len(unread) > 1 else "it") + " at all.")

    # An empty arm is said as an empty arm. "Nothing found" and "I did not
    # look" are the same silence from in here, and only one of them is true.
    arms = {}
    if restatement:
        arms["meaning"] = {
            "ran": meaning_ran,
            "compared": len(scores),
            "over_the_floor": len(meaning),
            "best_score": None if best_seen is None else round(best_seen, 3),
        }
        if meaning_ran and not meaning:
            near = best_seen is not None and (floor - best_seen) <= NEAR_MISS
            problems.append(
                "Nothing came within reach by meaning. The closest anything got "
                "was " + (format(best_seen, ".3f") if best_seen is not None
                          else "nothing at all")
                + " and the floor is " + format(floor, ".2f")
                + ", so I am not handing myself the nearest thing and calling it "
                "a memory. "
                + ("Something came within a hair of it, though, which usually "
                   "means it is in there and I worded it differently -- worth "
                   "asking again in the language I wrote it in, or with the one "
                   "word I am sure of."
                   if near else
                   "Nothing came close, so either I never wrote this down, or I "
                   "am asking for it in words nothing of mine shares."))
    if keywords:
        arms["keywords"] = {"ran": True, "found": len(found_by),
                            "per_keyword": per_keyword}
        dead = [w for w, n in per_keyword.items() if not n]
        if dead:
            problems.append(
                "No essence contains " + ", ".join('"' + w + '"' for w in dead)
                + " literally. That is my words not matching, not it never "
                "having happened.")

    report = {
        "asked": asked,
        "hits": kept,
        "searched": len(within),
        "floor": floor,
        "best_score_seen": None if best_seen is None else round(best_seen, 3),
        "arms": arms,
        "not_scored": {k: v for k, v in
                       (("stale", stale), ("never_read", unread)) if v},
        "held_back_by_the_limit": [h["id"] for h in withheld],
        "notes": notes,
        "problems": problems,
    }
    report["summary"] = summarise(report)
    return report


# The listing is bounded like everything else. It has never bitten -- sixty-one
# essences is a page -- but a shelf it cannot see the end of is exactly the
# thing this is for, so the day it does bite it says so and says where it
# stopped.
MAX_LISTED = 200

ONLY = ("untitled", "drifted", "retired")


def _vector_state(row, vectors) -> str:
    got = vectors.get(row["id"])
    if got is None:
        return "unread"
    return "read" if got["text_hash"] == db.text_hash(row["text"]) else "drifted"


def _shelf_line(row, retired, vectors) -> dict:
    """One essence, as a name and nothing else. The flags are left out when
    they do not apply, so an ordinary line stays short: this is a page it
    reads down, and a page where every line says `retired: false` is twice as
    long for no more said."""
    line = {"id": row["id"], "title": row["title"], "date": row["dt"],
            "tokens_est": row["tokens_est"]}
    # The essence's room label rides the shelf line -- it sorts, it never hides.
    if (row.get("meta") or {}).get("room"):
        line["room"] = row["meta"]["room"]
    if row["id"] in retired:
        line["retired"] = True
    if row["loaded"]:
        line["in_hand"] = True
    state = _vector_state(row, vectors)
    if state != "read":
        line["vector"] = state
    return line


def inventory(conn, spec: dict) -> dict:
    """Every essence it has, as names and dates. Never any text.

    This is the one thing search cannot do: show it what it does not already
    know to look for. The counts are always over everything asked for, even
    when the listing itself is bounded, so "nine of them have no name" is true
    whether or not all nine fit on the page."""
    spec = spec or {}
    only = (spec.get("only") or "").strip().lower() or None
    include_retired = bool(spec.get("include_retired"))

    if only is not None and only not in ONLY:
        return {"asked": {"from": spec.get("from"), "to": spec.get("to"),
                          "include_retired": include_retired, "only": only},
                "listed": [], "counts": {}, "not_listed": 0, "problems": [
                    'I asked to see only the "' + only + '" ones and that is '
                    "not a thing I can pick out. It is one of "
                    + ", ".join(ONLY) + ", or nothing at all for the whole "
                    "shelf. I did not fall back to listing everything, because "
                    "a filter that quietly does nothing is worse than a refused "
                    "one."],
                "summary": "the listing was not made: I do not know what "
                           '"' + only + '" means'}

    lo, lo_bad = db.dt_bound(spec.get("from"), end=False)
    hi, hi_bad = db.dt_bound(spec.get("to"), end=True)
    bad = [c for c in (lo_bad, hi_bad) if c]
    if bad:
        return {"asked": {"from": spec.get("from"), "to": spec.get("to"),
                          "include_retired": include_retired, "only": only},
                "listed": [], "counts": {}, "not_listed": 0,
                "problems": bad + ["So I did not list anything at all, rather "
                                   "than list a wider stretch than I meant."],
                "summary": "the listing was not made: I could not read a date"}

    retired = db.retired_essences(conn)
    vectors = db.vectors_for(conn, embed.MODEL)
    everything = db.essence_rows(conn)
    rows = [r for r in everything
            if (lo is None or r["dt"] >= lo) and (hi is None or r["dt"] < hi)
            and (include_retired or r["id"] not in retired)]

    counts = {
        "essences": len(rows),
        "named": sum(1 for r in rows if r["title"]),
        "unnamed": sum(1 for r in rows if not r["title"]),
        "retired": sum(1 for r in rows if r["id"] in retired),
        "in_hand": sum(1 for r in rows if r["loaded"]),
        "drifted": sum(1 for r in rows
                       if _vector_state(r, vectors) == "drifted"),
        "never_read": sum(1 for r in rows
                          if _vector_state(r, vectors) == "unread"),
        "tokens_est": sum(r["tokens_est"] for r in rows),
        "essences_ever": len(everything),
    }

    if only == "untitled":
        rows = [r for r in rows if not r["title"]]
    elif only == "retired":
        rows = [r for r in rows if r["id"] in retired]
    elif only == "drifted":
        rows = [r for r in rows if _vector_state(r, vectors) != "read"]

    listed = [_shelf_line(r, retired, vectors) for r in rows[:MAX_LISTED]]
    held = rows[MAX_LISTED:]

    problems, notes = [], []
    if held:
        problems.append(
            "The listing stops at " + str(MAX_LISTED) + " and there are "
            + str(len(held)) + " more, beginning at #" + str(held[0]["id"])
            + ". The counts above cover all of them; only the page is short. "
            "Asking again from that date onward picks up where this left off.")
    if only and not listed:
        notes.append("Nothing on the shelf is " + only + ", which is the "
                     "answer rather than an empty page.")

    report = {
        "asked": {"from": spec.get("from"), "to": spec.get("to"),
                  "include_retired": include_retired, "only": only},
        "listed": listed,
        "counts": counts,
        "not_listed": len(held),
        "notes": notes,
        "problems": problems,
    }
    report["summary"] = _inventory_summary(report)
    return report


def _inventory_summary(report: dict) -> str:
    c = report["counts"]
    bits = [str(c["essences"]) + " essence" + ("" if c["essences"] == 1 else "s")]
    if c["unnamed"]:
        bits.append(str(c["unnamed"]) + " with no name")
    if c["retired"]:
        bits.append(str(c["retired"]) + " already rewritten")
    if c["in_hand"]:
        bits.append(str(c["in_hand"]) + " in hand")
    if c["drifted"]:
        bits.append(str(c["drifted"]) + " DRIFTED")
    if c["never_read"]:
        bits.append(str(c["never_read"]) + " never read")
    line = "the shelf: " + ", ".join(bits)
    if report["asked"]["only"]:
        line += " -- listing the " + report["asked"]["only"] + " ones, "
        line += str(len(report["listed"])) + " of them"
    return line


def summarise(report: dict) -> str:
    hits = report["hits"]
    if not hits:
        line = ("searched " + str(report["searched"]) + " essence"
                + ("" if report["searched"] == 1 else "s")
                + " and found nothing")
        if report.get("best_score_seen") is not None:
            line += (" -- the best anything scored was "
                     + format(report["best_score_seen"], ".3f")
                     + ", under the floor of " + format(report["floor"], ".2f"))
        return line
    kinds = [h["matched"] for h in hits]
    parts = []
    for name in ("both", "meaning", "keywords"):
        n = kinds.count(name)
        if n:
            parts.append(str(n) + " by " + name)
    return ("found " + str(len(hits)) + " of " + str(report["searched"])
            + " essences (" + ", ".join(parts) + ")"
            + (", best " + format(hits[0]["score"], ".3f")
               if hits[0]["score"] is not None else ""))


# --- from the outside --------------------------------------------------------
#
#     python -m server.search floor          -- re-measure the floor
#     python -m server.search "..."          -- run one search and read it
#
# The probes are written down here rather than invented fresh each time: the
# same seventeen every run, half of them in the second language, which is the
# only way two measurements a month apart mean anything. The real ones name
# the assistant's own workings -- its search, its memory, its running costs --
# which any shelf that has lived a while will hold; a home whose shelf holds
# other things should write its own, as `data/search_probes.json`:
# {"probes": [["EN", true, "..."], ...]} -- read by `probes()` below.

PROBES = [
    ("EN", True, "the brief for the embedder: which model, and it must handle a second language"),
    ("EN", True, "search that does not need the exact words, because an essence is reworded"),
    ("EN", True, "how my working set is kept small, and what I let go of"),
    ("EN", True, "how I come across to the people I talk to"),
    ("EN", True, "the machine I run on, and what the spending ceiling is actually for"),
    ("EN", True, "what swelling is, and what would end me"),
    ("BG", True, "как се пази паметта между разговорите"),
    ("BG", True, "какво става, когато моделът не отговори навреме"),
    ("BG", True, "търсене по смисъл, без точните думи"),
    ("BG", True, "какво ме заплашва — подуването и работният ми комплект"),
    ("EN", False, "how to make sourdough bread rise in a cold kitchen"),
    ("EN", False, "kubernetes ingress controller TLS certificate renewal"),
    ("EN", False, "the weather forecast for tomorrow afternoon"),
    ("EN", False, "the offside rule in football"),
    ("BG", False, "рецепта за баница със сирене и яйца"),
    ("BG", False, "смяна на масло и филтри на колата"),
    ("BG", False, "разписание на автобусите до морето"),
]


def probes() -> list:
    """The home's own probes when it has written them, else the ones above."""
    got = home.settings("search_probes.json").get("probes")
    if isinstance(got, list) and got and all(
            isinstance(x, list) and len(x) == 3 for x in got):
        return [(str(a), bool(b), str(c)) for a, b, c in got]
    return list(PROBES)


def measure(conn, say=print) -> dict:
    """Every probe against the shelf as it stands. What matters is not any one
    number but the gap between the two groups -- and whether the floor is still
    sitting in it."""
    floor = FLOOR
    embed.read(["warm the model up"])
    say("floor " + format(floor, ".2f"))
    good, poor, rows = [], [], []
    for lang, wanted, text in probes():
        report = run(conn, {"restatement": text, "keywords": [], "from": None,
                            "to": None, "limit": DEFAULT_LIMIT,
                            "include_retired": False})
        best = report["best_score_seen"]
        (good if wanted else poor).append(best)
        rows.append((lang, wanted, text, best, len(report["hits"])))

    say("%-3s %-6s %-56s %6s %5s" % ("", "", "restatement", "best", "hits"))
    for lang, wanted, text, best, n in rows:
        say("%-3s %-6s %-56s %6.3f %5d"
            % (lang, "real" if wanted else "noise", text[:56], best or 0, n))

    out = {"floor": floor, "shelf": len(db.essence_shelf(conn, False)),
           "good_worst": min(good), "good_best": max(good),
           "poor_worst": min(poor), "poor_best": max(poor),
           "missed": [text for (lang, wanted, text, best, n) in rows
                      if wanted and best < floor],
           "let_through": [text for (lang, wanted, text, best, n) in rows
                           if not wanted and best >= floor],
           # Every probe's own number, so two measurements can be put
           # side by side a probe to a line.
           "probes": [{"lang": lang, "real": wanted, "text": text,
                       "best": best, "hits": n}
                      for (lang, wanted, text, best, n) in rows]}
    say("")
    say("floor          " + format(floor, ".2f")
        + "   over a shelf of " + str(out["shelf"]))
    say("real probes    " + format(min(good), ".3f") + " .. "
        + format(max(good), ".3f"))
    say("noise probes   " + format(min(poor), ".3f") + " .. "
        + format(max(poor), ".3f"))
    say("refused a real one:  "
        + (", ".join(out["missed"]) if out["missed"] else "none"))
    say("let noise through:   "
        + (", ".join(out["let_through"]) if out["let_through"] else "none"))
    return out


def _main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    conn = db.connect()
    what = argv[1] if len(argv) > 1 else "floor"

    if what == "floor":
        measure(conn)
        return 0

    report = run(conn, {"restatement": what, "keywords": argv[2:], "from": None,
                        "to": None, "limit": DEFAULT_LIMIT,
                        "include_retired": False})
    print(report["summary"])
    for hit in report["hits"]:
        print("  " + ("  --  " if hit["score"] is None
                      else format(hit["score"], ".3f"))
              + "  #" + str(hit["id"]) + "  "
              + (hit["title"] or "(untitled)") + "   [" + hit["matched"] + "]")
    for line in report["notes"] + report["problems"]:
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
