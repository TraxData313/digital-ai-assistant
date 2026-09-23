"""The pairs the adapter learns from, gathered out of the store and kept.

A pair is one X, something in the shape of a query, and one label, the
essence that should come back for it. There are four sources; this file
reads them out of the store, and it only ever reads. Nothing here writes to
the store -- it is opened so that nothing can -- and what it makes lands
under `data/adapter/pairs/`, which stays out of git because it quotes the
store.

**Fold pairs**, the main set. Every LIVE essence stands for rows: its
`replaces`, followed by `db.trail` through any essence it was edited out of
or folded from, down to the conversation underneath. Of those rows only the
kinds the suggester is ever handed are kept -- `recall.READ_KINDS`: people,
the assistant, the angel, the hands; never a job, a world notice, a dream or
a lost line -- so every X-raw is shaped like what the query writer sees at
run time. A trail with no readable row gives no fold pair, and is counted.
X-raw is the last four readable rows, each labelled by its speaker and cut
exactly as the automatic memory cuts a line before reading it
(`recall.line_of`, `recall.cut_lines`, the default knobs) -- the shape the
suggester sees, without the prompt's own words around it. A longer trail
gives up to two more windows of four stepping back, the earliest taking
what is left; three at most, all grouped under their essence so no essence
is ever on both sides of a split. The window over the trail's last rows is
marked `last` -- on a lone window too -- so one X per essence is a filter
and not a guess. X-said, under `--said`, is what today's query writer makes
of each window: `recall.ask`, the same small model and prompt, run offline.
X-said decides for the suggester's arm, so training matches deployment --
and whenever the writer is retrained, these are made again.

**Miss pairs**, from the ledger. A turn where the automatic memory ran,
wrote a query, and did not name an essence the assistant then fetched by id
is a miss: X-said is the query actually written that turn, one pair per
subject when it wrote several, and X-raw beside it is the four lines it
read that turn, found again with `recall.lines_before`. Cross-episode and
deployment-shaped, thin, and the set that decides. Some are the writer's
own fault: asked to remember, it wrote about the remembering -- "asked me
to search my memory" -- and not about the thing, and no map over the
essences can mend a query that names no subject. They stay in the set,
marked `writer_fault`, and `show` counts the misses with and without them.
The rule is one regex, `WRITER_FAULT`, and nothing cleverer: the query
speaks of remembering or recalling, of searching, pulling, checking or
digging in memory, or of being asked to do one of those, in English or by a
Cyrillic word stem. Every X-said carries the mark, the fold windows' too;
the raw companions are valid as they are and carry `false`.

**Its own**: the assistant's own deliberate search whose restatement found an
essence it then fetched, that turn or the next, and which the automatic
memory had not named that turn -- the restatement and that essence. A
search from before the suggester existed counts, marked `recall_ran:
false`. These restatements are reported for their own arm, never averaged
with X-said.

**Sanity**: a fetch of an essence the automatic memory HAD named that turn
-- shown among its titles, or set aside by name, which is still a name in
front of the assistant -- and a search that found one it had named. The
naming may be why it was fetched. They train nothing and must not get
worse.

**Lineage and sisters**. Labels are live essences only. A fetched or found
essence that has since been edited or folded is mapped forward along
`replaces` to the live essence that holds it; with no live successor the
pair is dropped and counted. Live essences whose trail rows intersect -- a
fold that splits subjects leaves two essences over the same rows -- are
sisters, listed on every pair: downstream they are never each other's
negatives, and a hit on any sister counts. The same rule for a different
reason, **co-labels**: one X text often carries more than one label -- the
query of a turn on which two essences were fetched, the window two sisters
share, a search that found what it named and what
it had not -- and `x_index` lists every label attached to each distinct
text, so the trainer takes all of them as positives for that X and never
one of them as another's negative.

The file. One json, stamped with the moment it was made: the distinct X
texts, the pairs pointing into them by index, `x_index` with each text's
hash and its labels, the sisters, the lineage mappings made, the live
shelf, the counts. Under `--embed`, an npz beside it with one vector per
text in the same order, and each `x_index` entry gains the model's own
token count and its `truncated` mark, so a vector can never be quietly
about the wrong words. Never overwritten; made again in seconds.

    python -m server.adapt_pairs build [--store PATH] [--out DIR] [--said] [--embed] [--model KEY]
    python -m server.adapt_pairs show <file>
"""

import bisect
import json
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import db, embed, recall
from . import home

ROOT = Path(__file__).resolve().parent.parent
# A variable rather than a constant so a bench can point it at a scratch.
PAIRS_DIR = home.DATA / "adapter" / "pairs"

# What the suggester reads at run time: four lines. A window is that many
# rows of a trail; a trail gives at most this many windows.
WINDOW = 4
WINDOWS_MAX = 3

SETS = ("fold", "miss", "hers", "sanity")
X_KINDS = ("raw", "said", "hers")

# How the room writes a reach for an essence into the ledger's `asked`:
# taking one off the shelf whole, or reaching for the rows behind it. Both
# name the essence by id, which is the signal -- the assistant knew which
# one it wanted -- so both count as a fetch of it, and the pair says which.
BY_ID = {"itself": re.compile(r"^essence #(\d+) itself$"),
         "sources": re.compile(r"^the rows behind essence #(\d+)$")}

# The suggester joins several subjects' queries with this when it writes
# the one line the ledger keeps (`recall._joined`); split here to get them
# back one by one.
JOIN = " ; "

# How many texts go to the embedder at once. Its own batch is eight; this
# is only how often progress is said.
EMBED_CHUNK = 32

# The writer at fault: a query about remembering instead of about a thing.
# The shapes seen in the ledger ("asked me to search my memory", "what I
# remember about it when asked to pull the memory") and the shapes beside
# them: remember or recall as a verb; memory as the object of searching,
# pulling, checking, looking or digging; "asked me to" with one of those;
# and the same in Cyrillic, matched at the start of a word (помня, спомням
# си, припомни, паметта).
WRITER_FAULT = re.compile(
    r"\b(remember(s|ed|ing)?|recall(s|ed|ing)?)\b"
    r"|\b(search|pull|check|look|dig|comb)(s|ed|ing|ged|ging)?\b[^.;:!?]{0,24}?"
    r"\b(my|her|his|the|your|our|its) (memory|memories)\b"
    r"|\basked (me|her) to (remember|recall|search|check|look|find|pull|fetch|dig)\b"
    r"|(?<!\w)(помн|спомн|припомн|памет)",
    re.IGNORECASE)

# How many ids one SELECT ... IN (...) is handed. SQLite's own bound is far
# higher on this machine, but a thousand was the old one, and nothing here
# needs to find out which build it is talking to.
IN_CHUNK = 400


class Refused(ValueError):
    pass


def _rel(p) -> str:
    """A path as it is said to a person: relative to the repo when inside it."""
    try:
        return str(Path(p).resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def open_store(path=None):
    """The store -- or a copy of it -- opened so that nothing in here can
    write to it. The default is the live one, by way of `db.DB_PATH`, which
    a bench points elsewhere."""
    p = Path(path) if path else Path(db.DB_PATH)
    if not p.is_file():
        raise Refused("there is no store at " + str(p))
    conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn, p


def stamp_now() -> str:
    """The moment, made safe for a file's name."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


# -- lineage -----------------------------------------------------------------

class Lineage:
    """Every essence, which of them are live, and the way forward from one
    that is not.

    An edit retires its predecessor and a fold swallows several; both are
    written as `replaces` on the newer essence, so forward is the reverse
    of that -- from a retired essence to whatever names it, and on until a
    live one. A retired essence can be named by two, an edit and a fold both
    reaching for it; then every live end is found, the newest stands, and
    the ambiguity is counted rather than hidden. The live set is the same
    rule `db.essence_shelf` uses -- never named in any `replaces` -- and
    `build` checks the two agree."""

    def __init__(self, conn):
        self.replaces, self.dt = {}, {}
        for r in conn.execute(
                "SELECT id, dt, replaces FROM rows WHERE kind = 'essence'"):
            reps = json.loads(r["replaces"]) if r["replaces"] else []
            self.replaces[r["id"]] = [int(i) for i in reps]
            self.dt[r["id"]] = r["dt"]
        self.named_by = defaultdict(list)
        for e, reps in self.replaces.items():
            for x in reps:
                if x in self.replaces:
                    self.named_by[x].append(e)
        self.live = {e for e in self.replaces if e not in self.named_by}
        self.ambiguous = {}
        self._memo = {}

    def is_essence(self, eid) -> bool:
        return int(eid) in self.replaces

    def live_of(self, eid):
        """The live essence standing for `eid` today: itself when live, else
        the newest live end of the chain forward, else None -- a row that is
        not an essence, or a retired one whose every successor is retired
        too with nothing live at the end, which the store does not make."""
        eid = int(eid)
        if eid in self._memo:
            return self._memo[eid]
        if eid not in self.replaces:
            out = None
        elif eid in self.live:
            out = eid
        else:
            seen, ends, queue = {eid}, set(), list(self.named_by[eid])
            while queue:
                e = queue.pop(0)
                if e in seen:
                    continue
                seen.add(e)
                if e in self.live:
                    ends.add(e)
                else:
                    queue.extend(self.named_by[e])
            if len(ends) > 1:
                self.ambiguous[eid] = sorted(ends)
            out = max(ends) if ends else None
        self._memo[eid] = out
        return out


# -- the X side ----------------------------------------------------------------

class Texts:
    """The distinct X texts, each kept once and pointed at by index. A
    hundred windows that say the same four lines are one vector."""

    def __init__(self):
        self.list, self.index = [], {}

    def add(self, text: str) -> int:
        if text not in self.index:
            self.index[text] = len(self.list)
            self.list.append(text)
        return self.index[text]


def windows_of(rows, size: int = WINDOW, most: int = WINDOWS_MAX) -> list:
    """The windows of a trail, newest first: the last `size` rows, then the
    `size` before them, stepping back until `most` windows or no rows are
    left. The earliest window takes what is left when the trail does not
    divide evenly, so a trail of nine rows gives three windows and its
    first row is not thrown away for being alone."""
    rows = sorted(int(i) for i in rows)
    out, end = [], len(rows)
    while end > 0 and len(out) < most:
        start = max(0, end - size)
        out.append(rows[start:end])
        end = start
    return out


def raw_text(lines, cfg) -> str:
    """Lines as the embedder reads them for a pair: each labelled by its
    speaker and cut exactly as the query writer's prompt cuts it -- the
    same subject rule, the same caps, by the same function -- without the
    prompt's own words around them, which are the writer's and not the
    room's."""
    cut = recall.cut_lines([dict(ln) for ln in lines], cfg)
    return "\n".join(ln["who"] + ": " + ln["text"] for ln in cut)


def writer_fault(query) -> bool:
    """Whether a query is the writer pointing at the act of remembering
    rather than at a thing: `WRITER_FAULT` over the query's words, and
    nothing cleverer. "asked me to search my memory, suggesting I already
    had the information" is one; "the watcher needs a fifth sense called
    quiet" is not."""
    return bool(WRITER_FAULT.search(" ".join(str(query or "").split())))


def _rows_by_ids(conn, ids) -> dict:
    ids = sorted({int(i) for i in ids})
    out = {}
    for i in range(0, len(ids), IN_CHUNK):
        for row in db.rows_by_ids(conn, ids[i:i + IN_CHUNK]):
            out[row["id"]] = row
    return out


# -- fold pairs ---------------------------------------------------------------

def fold_pairs(conn, lin: Lineage, index: dict, cfg: dict, texts: Texts,
               say) -> dict:
    """Every live essence, its trail cut down to the rows the suggester
    reads, its windows, and its sisters. Returns the pairs, the windows
    with their lines still on them (the said pass needs the lines, and the
    file does not), the sisters, and the counts."""
    shelf = db.essence_shelf(conn, include_retired=False)
    if {r["id"] for r in shelf} != lin.live:
        raise Refused("the shelf and the lineage disagree about which "
                      "essences are live -- the two rules have drifted, and "
                      "nothing should be built over that")
    trails = {r["id"]: db.trail(index, r["id"]) for r in shelf}

    # Sisters: any shared row makes two live essences sisters. Over the
    # whole trail, unread rows too -- two essences folded from one job's
    # rows stand over the same ground whether or not a window is cut there.
    by_row = defaultdict(set)
    for e, tr in trails.items():
        for rid in tr["rows"]:
            by_row[rid].add(e)
    sisters = defaultdict(set)
    for es in by_row.values():
        if len(es) > 1:
            for e in es:
                sisters[e] |= es - {e}

    counts = Counter(essences_live=len(shelf))
    plan, need = [], set()
    for r in shelf:
        tr = trails[r["id"]]
        if tr["truncated"]:
            counts["trails_truncated"] += 1
        if tr["missing"]:
            counts["trails_with_missing_rows"] += 1
        if not tr["rows"]:
            counts["essences_without_rows"] += 1
            continue
        # Only the kinds the suggester is handed. A window over a job's
        # rows would be an X the query writer never sees at run time, and
        # a map fitted to it would be fitted to nothing.
        read = [i for i in tr["rows"] if index[i]["kind"] in recall.READ_KINDS]
        unread = len(tr["rows"]) - len(read)
        if unread:
            counts["essences_with_unread_rows"] += 1
            counts["trail_rows_unread"] += unread
        if not read:
            counts["essences_without_readable_rows"] += 1
            continue
        ws = windows_of(read)
        plan.append((r, tr, read, ws))
        need.update(i for w in ws for i in w)
    rows = _rows_by_ids(conn, need)

    pairs, windows = [], []
    for r, tr, read, ws in plan:
        made = 0
        for k, w in enumerate(ws):
            lines = [recall.line_of(rows[i]) for i in w if i in rows]
            if not lines:
                counts["windows_without_rows"] += 1
                continue
            # What the said pass and the file both keep about a window;
            # `last` is the window over the trail's last rows, the X-raw
            # of the plan, which a lone window is as well.
            meta = {"window": w, "window_no": k, "windows": len(ws),
                    "last": k == 0, "trail_rows": len(tr["rows"]),
                    "trail_read": len(read),
                    "kinds": sorted({index[i]["kind"] for i in w})}
            pairs.append(dict({
                "set": "fold", "x_kind": "raw", "x": texts.add(raw_text(lines, cfg)),
                "label": r["id"], "essence": r["id"],
                "turn": None, "event": None, "dt": r["dt"], "label_dt": r["dt"],
                "sisters": sorted(sisters.get(r["id"], ())),
                "mapped_from": None,
            }, **meta))
            windows.append(dict(meta, essence=r["id"], lines=lines, dt=r["dt"]))
            counts["fold_windows"] += 1
            made += 1
        if made:
            counts["essences_with_pairs"] += 1
    counts["essences_without_fold_pairs"] = len(shelf) - counts["essences_with_pairs"]
    say(str(counts["essences_with_pairs"]) + " live essences with readable rows "
        "behind them, " + str(counts["fold_windows"]) + " windows"
        + (", " + str(counts["essences_without_rows"]) + " with no rows"
           if counts["essences_without_rows"] else "")
        + (", " + str(counts["essences_without_readable_rows"]) + " with only "
           "unread rows" if counts["essences_without_readable_rows"] else "")
        + (", " + str(counts["trail_rows_unread"]) + " unread rows left out of "
           + str(counts["essences_with_unread_rows"]) + " trails"
           if counts["trail_rows_unread"] else "")
        + ", " + str(len(sisters)) + " with sisters")
    return {"pairs": pairs, "windows": windows,
            "sisters": {e: sorted(s) for e, s in sisters.items()},
            "trails": trails, "counts": counts}


# -- the ledger ----------------------------------------------------------------

def ledger(conn) -> dict:
    """The recall, fetch and search events, by the turn they hung off."""
    turns = defaultdict(lambda: {"recall": None, "fetch": [], "search": []})
    for ev in db.all_events(conn):
        if ev["kind"] not in ("recall", "fetch", "search") or ev["reply_row"] is None:
            continue
        t = turns[int(ev["reply_row"])]
        if ev["kind"] == "recall":
            # One per turn, always; were there two, the later one is the
            # one shown last.
            t["recall"] = ev
        else:
            t[ev["kind"]].append(ev)
    return turns


def recall_of(ev) -> dict:
    """What the suggester wrote and named on a turn, out of its event: the
    queries one per subject, the ids it showed as titles, and the ids it
    set aside by name. The ledger's shape has grown over time -- the
    first events have no `about`, no `set_aside` -- so every key is read
    as optional."""
    d = (ev or {}).get("detail") or {}

    def ids(key):
        return [int(t["id"]) for t in (d.get(key) or [])
                if isinstance(t, dict) and t.get("id") is not None]

    queries = [q.strip() for q in str(d.get("query") or "").split(JOIN)
               if q.strip()]
    return {"event": ev["id"], "dt": ev["dt"], "queries": queries,
            "titles": ids("titles"), "set_aside": ids("set_aside"),
            "read": int(d.get("read") or 0) or None,
            "missed": d.get("missed"), "late": bool(d.get("late"))}


def fetched_on(turn: dict) -> dict:
    """The essences the assistant reached for by id on a turn, out of its fetch events:
    id -> how (itself, sources), the event, its time. An essence reached for
    twice on one turn -- once in a round of looking, once at the end --
    is one reach."""
    out = {}
    for ev in turn["fetch"]:
        d = ev.get("detail") or {}
        for phrase in d.get("asked") or []:
            for how, pat in BY_ID.items():
                m = pat.match(str(phrase))
                if m:
                    out.setdefault(int(m.group(1)),
                                   {"how": how, "event": ev["id"], "dt": ev["dt"]})
    return out


def ledger_pairs(conn, lin: Lineage, turns: dict, cfg: dict, texts: Texts,
                 sisters: dict, say) -> dict:
    """The miss, hers and sanity pairs, out of the turns. Returns the pairs,
    the lineage mappings made for labels, the pairs dropped for want of a
    live successor, and the counts."""
    self_rows = [r["id"] for r in conn.execute(
        "SELECT id FROM rows WHERE kind = '" + home.SELF + "' ORDER BY id")]

    def next_turn(t):
        i = bisect.bisect_right(self_rows, t)
        return self_rows[i] if i < len(self_rows) else None

    counts = Counter()
    pairs, mapped, dropped = [], {}, []

    def label_of(eid, turn):
        live = lin.live_of(eid)
        if live is None:
            dropped.append({"turn": turn, "essence": eid})
        return live

    def paired(eid, label):
        """A pair was made under `label` for a reach at `eid`: if the two
        differ, that is a lineage mapping worth listing -- only then, so
        the list is of mappings that carry a pair and not of every retired
        id ever reached for."""
        if label != eid:
            mapped[eid] = label

    def either(eid, group):
        return eid in group or lin.live_of(eid) in group

    for T in sorted(turns):
        t = turns[T]
        rc = recall_of(t["recall"]) if t["recall"] else None
        named, aside = set(), set()
        if rc:
            counts["recall_events"] += 1
            counts["recall_with_query" if rc["queries"] else "recall_without_query"] += 1
            for group, key in ((named, "titles"), (aside, "set_aside")):
                for i in rc[key]:
                    group.add(i)
                    live = lin.live_of(i)
                    if live is not None:
                        group.add(live)
        has_query = bool(rc and rc["queries"])

        # -- what the assistant fetched by id, against what it named -------
        # One label pairs once per turn: a reach for a retired essence and
        # one for its successor are the same want, and a second copy of a
        # pair is a weight put on it by accident.
        F = fetched_on(t)
        labels_done = set()
        for eid, f in F.items():
            if not lin.is_essence(eid):
                counts["fetch_not_an_essence"] += 1
                continue
            counts["fetches_by_id"] += 1
            counts["fetches_" + f["how"]] += 1
            label = label_of(eid, T)
            if label is None:
                counts["pairs_dropped"] += 1
                continue
            if label in labels_done:
                counts["fetch_same_label_again"] += 1
                continue
            labels_done.add(label)
            if not has_query:
                counts["fetch_no_query_that_turn" if rc else
                       "fetch_no_recall_that_turn"] += 1
                continue
            if either(eid, named):
                kind, how_named = "sanity", "titles"
            elif either(eid, aside):
                kind, how_named = "sanity", "set_aside"
            else:
                kind, how_named = "miss", None
            counts["fetch_" + (kind if kind == "miss" else "named_" + how_named)] += 1
            paired(eid, label)
            base = {"label": label, "essence": label, "turn": T,
                    "event": rc["event"], "dt": rc["dt"],
                    "label_dt": lin.dt.get(label), "how": f["how"],
                    "named_how": how_named,
                    "sisters": sorted(sisters.get(label, ())),
                    "mapped_from": eid if eid != label else None}
            for k, q in enumerate(rc["queries"]):
                pairs.append(dict(base, set=kind, x_kind="said", x=texts.add(q),
                                  window=None, subject=k, subjects=len(rc["queries"]),
                                  writer_fault=writer_fault(q)))
            if kind == "miss":
                # The lines it read that turn, as it read them: the same
                # kinds, the same room, the rows before its reply. Valid
                # whatever the writer made of them, so never at fault.
                lines = recall.lines_before(conn, rc["read"] or cfg["lines"], T)
                if lines:
                    pairs.append(dict(base, set=kind, x_kind="raw",
                                      x=texts.add(raw_text(lines, cfg)),
                                      window=[ln["id"] for ln in lines],
                                      writer_fault=False))
                else:
                    counts["miss_without_lines"] += 1

        # -- the assistant's own searches, against what it then fetched -------
        T2 = next_turn(T)
        F2 = {eid: dict(f, turn=T) for eid, f in F.items()}
        if T2 is not None and T2 in turns:
            for eid, f in fetched_on(turns[T2]).items():
                F2.setdefault(eid, dict(f, turn=T2))
        for ev in t["search"]:
            for k, s in enumerate((ev.get("detail") or {}).get("searches") or []):
                counts["searches"] += 1
                rest = str((s.get("asked") or {}).get("restatement") or "").strip()
                if not rest:
                    counts["searches_without_restatement"] += 1
                    continue
                hits = set()
                for h in s.get("hits") or []:
                    if isinstance(h, dict) and h.get("id") is not None:
                        hits.add(int(h["id"]))
                        live = lin.live_of(h["id"])
                        if live is not None:
                            hits.add(live)
                # The earlier count's question, kept beside this one: did
                # the search find anything the suggester had named?
                if rc:
                    counts["searches_found_named" if (hits & named)
                           else "searches_found_unnamed"] += 1
                else:
                    counts["searches_before_the_suggester"] += 1
                found = [(eid, f) for eid, f in F2.items()
                         if lin.is_essence(eid) and either(eid, hits)]
                if not found:
                    counts["searches_nothing_fetched"] += 1
                    continue
                done = set()
                for eid, f in found:
                    label = label_of(eid, T)
                    if label is None:
                        counts["pairs_dropped"] += 1
                        continue
                    if label in done:
                        continue
                    done.add(label)
                    kind = "sanity" if (either(eid, named) or either(eid, aside)) else "hers"
                    counts["search_" + kind] += 1
                    paired(eid, label)
                    pairs.append({
                        "set": kind, "x_kind": "hers", "x": texts.add(rest),
                        "label": label, "essence": label, "window": None,
                        "turn": T, "event": ev["id"], "search": k,
                        "fetched_on": f["turn"], "recall_ran": has_query,
                        "dt": ev["dt"], "label_dt": lin.dt.get(label),
                        "how": f["how"],
                        "named_how": ("titles" if either(eid, named) else
                                      "set_aside" if either(eid, aside) else None),
                        "sisters": sorted(sisters.get(label, ())),
                        "mapped_from": eid if eid != label else None,
                    })
    say(str(counts["fetches_by_id"]) + " fetches by id, "
        + str(counts["fetch_miss"]) + " missed by the suggester; "
        + str(counts["searches"]) + " searches, "
        + str(counts["search_hers"]) + " found what was then fetched unnamed")
    return {"pairs": pairs, "mapped": mapped, "dropped": dropped,
            "counts": counts, "ambiguous": dict(lin.ambiguous)}


# -- X-said ------------------------------------------------------------------

def writer_ready(key) -> str:
    """Whether the query writer can be asked: None when its model is in
    memory, else why not, in words. Loads it when it is on the disk and
    not loaded -- the same call the room makes -- and never downloads."""
    if not key:
        return "no model is chosen for the automatic memory, and none was named"
    rec = recall._find(recall._models(), key)
    if rec is None:
        return "the model " + key + " is not on this disk"
    if not recall._instances(rec):
        # The writer's own bring-up, not the model's: that one gives the
        # room's chosen model the last word and frees what it just loaded
        # when the room has none chosen -- which is the case in a card
        # hour, the automatic memory being switched to none for it
        # (measured: "could not be loaded: loaded in 3.3 s").
        recall._bring_up_writer(key)
        rec = recall._find(recall._models(), key)
        if not recall._instances(rec):
            return "the model " + key + " could not be loaded: " + str(
                recall.STATE.get("writer_detail") or recall.STATE.get("writer_phase"))
    return None


def said_pass(pairs: list, windows: list, cfg: dict, told: dict, key,
              texts: Texts, sisters: dict, ask=None, ready=None,
              say=None) -> dict:
    """X-said for every fold window: today's query writer, asked exactly as
    the room asks it, its answer's subjects each becoming a pair under the
    window's essence. No room deadline here -- the writer's own wire allows
    it half a minute -- and a call that fails is counted and skipped; a
    server that has gone away stops the pass rather than failing four
    hundred times."""
    ask = ask or recall.ask
    ready = ready or writer_ready
    say = say or (lambda *a: None)
    out = {"model": key, "windows": len(windows), "asked": 0, "answered": 0,
           "failed": 0, "empty": 0, "pairs": 0, "not_run": None, "seconds": 0.0}
    try:
        why = ready(key)
    except (recall.NoServer, recall.LMError) as exc:
        why = str(exc)
    if why:
        out["not_run"] = why
        say("X-said not written: " + why)
        return out
    started = time.time()
    for n, w in enumerate(windows, 1):
        out["asked"] += 1
        try:
            got = ask(key, [dict(ln) for ln in w["lines"]], cfg, told)
        except recall.NoServer as exc:
            out["failed"] += len(windows) - n + 1
            out["not_run"] = "the server went away after " + str(n - 1) + ": " + str(exc)
            say("X-said stopped: " + out["not_run"])
            break
        except Exception as exc:   # one bad answer must not end the pass
            out["failed"] += 1
            say("  window " + str(n) + " failed: " + type(exc).__name__ + ": " + str(exc))
            continue
        subjects = [s for s in (got.get("subjects") or []) if s.get("query")]
        if not subjects:
            out["empty"] += 1
            continue
        out["answered"] += 1
        for k, s in enumerate(subjects):
            pairs.append({
                "set": "fold", "x_kind": "said", "x": texts.add(s["query"]),
                "label": w["essence"], "essence": w["essence"],
                "window": w["window"], "window_no": w["window_no"],
                "windows": w["windows"], "last": w["last"], "kinds": w["kinds"],
                "trail_rows": w["trail_rows"], "trail_read": w["trail_read"],
                "subject": k, "subjects": len(subjects),
                "about": s.get("about") or None, "flaw": got.get("flaw"),
                "still_flawed": got.get("still_flawed"),
                "used_about": bool(got.get("used_about")),
                "writer_fault": writer_fault(s["query"]),
                "turn": None, "event": None, "dt": w["dt"], "label_dt": w["dt"],
                "sisters": sorted(sisters.get(w["essence"], ())),
                "mapped_from": None,
            })
            out["pairs"] += 1
        if n % 20 == 0 or n == len(windows):
            say("  " + str(n) + "/" + str(len(windows)) + " windows, "
                + format(time.time() - started, ".0f") + " s")
    out["seconds"] = round(time.time() - started, 1)
    return out


# -- vectors ------------------------------------------------------------------

def x_index_of(texts: list, pairs: list) -> list:
    """One entry per distinct X text, in the texts' order: its hash and
    every label any pair attaches to it -- the co-labels, across every set,
    since an essence fetched on a query is a right answer to it whether
    the suggester had named it or not. The token count and the `truncated`
    mark stay None until the embed pass fills them."""
    by_x = defaultdict(set)
    for p in pairs:
        by_x[p["x"]].add(p["label"])
    return [{"hash": db.text_hash(t), "labels": sorted(by_x.get(i, ())),
             "n_tokens": None, "truncated": None}
            for i, t in enumerate(texts)]


def embed_pass(texts: list, index: list, read=None, say=None) -> tuple:
    """Every distinct X read into a vector, in order. Returns the array and
    the `x_index` handed in, each entry now carrying the model's own token
    count and whether it was cut -- the `truncated` mark, passed along as
    every caller of `embed.read` must -- and its hash as the embedder
    computed it, which is the same function over the same text."""
    read = read or embed.read
    say = say or (lambda *a: None)
    vecs = []
    for i in range(0, len(texts), EMBED_CHUNK):
        chunk = texts[i:i + EMBED_CHUNK]
        for j, rec in enumerate(read(chunk)):
            vecs.append(rec["vec"])
            index[i + j].update(hash=rec.get("hash") or index[i + j]["hash"],
                                n_tokens=rec.get("n_tokens"),
                                truncated=bool(rec.get("truncated")))
        say("  " + str(min(i + EMBED_CHUNK, len(texts))) + "/" + str(len(texts)))
    return np.asarray(vecs, dtype=np.float32), index


# -- the build ------------------------------------------------------------------

def build(store=None, out_dir=None, said: bool = False,
          embed_vectors: bool = False, model=None, ask=None, ready=None,
          read=None, say=None) -> tuple:
    """Read a store, make the pairs, write the file. Returns (doc, path).
    The store is opened read-only; `out_dir` is made if it is not there."""
    say = say or (lambda *a: None)
    conn, path = open_store(store)
    out_dir = Path(out_dir) if out_dir else Path(PAIRS_DIR)
    cfg = recall.settings()
    knobs = recall.defaults()     # what shapes X-raw: the knobs as written
    cfg = dict(cfg, lines=knobs["lines"], line_tokens=knobs["line_tokens"])
    say("reading " + _rel(path) + " read-only")
    try:
        lin = Lineage(conn)
        index = db.trail_index(conn)
        texts = Texts()
        fold = fold_pairs(conn, lin, index, cfg, texts, say)
        turns = ledger(conn)
        led = ledger_pairs(conn, lin, turns, cfg, texts, fold["sisters"], say)
        pairs = fold["pairs"] + led["pairs"]
        said_out = None
        if said:
            key = model or cfg.get("model")
            say("asking the query writer for X-said over "
                + str(len(fold["windows"])) + " windows"
                + (" (" + str(key) + ")" if key else ""))
            said_out = said_pass(pairs, fold["windows"], cfg, recall.config(),
                                 key, texts, fold["sisters"], ask=ask,
                                 ready=ready, say=say)
        titles = {}
        for r in conn.execute("SELECT id, dt, title FROM rows WHERE kind = 'essence'"):
            titles[r["id"]] = {"title": r["title"], "dt": r["dt"]}
    finally:
        conn.close()

    counts = Counter(fold["counts"])
    counts.update(led["counts"])
    counts["essences"] = len(lin.replaces)
    counts["essences_retired"] = len(lin.replaces) - len(lin.live)
    counts["texts"] = len(texts.list)
    counts["pairs"] = len(pairs)
    counts["writer_fault_said"] = sum(1 for p in pairs if p.get("writer_fault"))
    counts["writer_fault_miss"] = sum(1 for p in pairs
                                      if p["set"] == "miss" and p.get("writer_fault"))
    x_index = x_index_of(texts.list, pairs)
    counts["texts_with_co_labels"] = sum(1 for x in x_index if len(x["labels"]) > 1)
    labels = sorted({p["label"] for p in pairs})
    doc = {
        "stamp": stamp_now(),
        "made": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "store": _rel(path),
        "embedder": {"model": embed.MODEL, "revision": embed.REVISION},
        "knobs": {"lines": cfg["lines"], "line_tokens": cfg["line_tokens"]},
        "window": WINDOW, "windows_max": WINDOWS_MAX,
        "shelf": {"essences": len(lin.replaces), "live": sorted(lin.live),
                  "retired": counts["essences_retired"]},
        "labels": {str(i): titles.get(i) for i in labels},
        "sisters": {str(e): s for e, s in sorted(fold["sisters"].items())},
        "lineage": {str(k): v for k, v in sorted(led["mapped"].items())},
        "ambiguous": {str(k): v for k, v in sorted(led["ambiguous"].items())},
        "dropped": led["dropped"],
        "texts": texts.list,
        "pairs": pairs,
        "said": said_out,
        "x_index": x_index,
        "vectors": None,
        "counts": dict(sorted(counts.items())),
    }
    vectors = None
    if embed_vectors:
        say("reading " + str(len(texts.list)) + " texts into vectors with "
            + embed.MODEL)
        vectors, doc["x_index"] = embed_pass(texts.list, x_index, read=read, say=say)
    where = write_file(doc, out_dir, vectors)
    say("wrote " + _rel(where))
    return doc, where


def write_file(doc: dict, out_dir: Path, vectors=None) -> Path:
    """The json, and the npz beside it when there are vectors. Never over
    an existing stamp -- a pairs file is what a version's `trained_on` hash
    points back at, and two files under one name would make the hash a
    liar -- and each written whole or not at all."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    js = out_dir / (doc["stamp"] + ".json")
    npz = out_dir / (doc["stamp"] + ".npz")
    if js.exists() or npz.exists():
        raise Refused("there is already a pairs file stamped " + doc["stamp"]
                      + " in " + _rel(out_dir) + "; a second in the same "
                      "second is not made over it")
    if vectors is not None:
        if len(doc.get("x_index") or []) != len(doc["texts"]) or \
                vectors.shape[0] != len(doc["texts"]):
            raise Refused("the vectors do not line up with the texts: "
                          + str(vectors.shape[0]) + " vectors, "
                          + str(len(doc["texts"])) + " texts")
        doc["vectors"] = npz.name
        tmp = npz.with_name(npz.name + ".tmp")
        with open(tmp, "wb") as f:
            np.savez(f, vectors=vectors,
                     hashes=np.array([x["hash"] for x in doc["x_index"]]))
        tmp.replace(npz)
    tmp = js.with_name(js.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(js)
    return js


def load(path) -> dict:
    """A pairs file, read back. Refused in words when it is not one."""
    p = Path(path)
    if not p.is_file():
        raise Refused("there is no pairs file at " + str(p))
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(str(p) + " could not be read (" + type(exc).__name__
                      + ": " + str(exc) + ")")
    if not isinstance(doc, dict) or "pairs" not in doc or "texts" not in doc:
        raise Refused(str(p) + " is not a pairs file")
    return doc


def vectors_of(path, doc=None) -> tuple:
    """The vectors beside a pairs file, checked against it: (array, note).
    None and the reason when there are none or they do not line up."""
    doc = doc or load(path)
    if not doc.get("vectors"):
        return None, "none"
    npz = Path(path).parent / doc["vectors"]
    if not npz.is_file():
        return None, doc["vectors"] + " is named and not there"
    try:
        with np.load(npz) as got:
            vectors, hashes = got["vectors"], [str(h) for h in got["hashes"]]
    except Exception as exc:
        return None, npz.name + " could not be read (" + type(exc).__name__ + ")"
    want = [x["hash"] for x in doc.get("x_index") or []]
    if vectors.shape[0] != len(doc["texts"]) or hashes != want:
        return None, npz.name + " does not line up with the texts"
    cut = sum(1 for x in doc["x_index"] if x.get("truncated"))
    return vectors, (npz.name + ": " + str(vectors.shape[0]) + " vectors of "
                     + str(vectors.shape[1]) + ", " + str(cut) + " truncated")


# -- saying what is in a file ------------------------------------------------

def _short(text: str, n: int = 80) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def describe(doc: dict, path=None, vectors_note=None) -> list:
    """The summary, as lines: counts per set and per x kind, windows per
    essence, sisters, the miss and hers pairs one per line, the lineage
    mappings and the pairs dropped, and the notes. The same lines end a
    build and answer `show`."""
    pairs = doc["pairs"]
    c = doc.get("counts") or {}
    per_set = Counter(p["set"] for p in pairs)
    per_kind = Counter(p["x_kind"] for p in pairs)
    per_both = Counter((p["set"], p["x_kind"]) for p in pairs)
    lines = []
    if path:
        lines.append("pairs      " + _rel(path))
    lines.append("made       " + str(doc.get("made")) + "  over "
                 + str(doc.get("store")) + ", read-only")
    emb = doc.get("embedder") or {}
    lines.append("embedder   " + str(emb.get("model")) + "  pinned to "
                 + (str(emb.get("revision") or "")[:12] or "nothing"))
    shelf = doc.get("shelf") or {}
    lines.append("shelf      " + str(len(shelf.get("live") or [])) + " live essences of "
                 + str(shelf.get("essences")) + ", " + str(shelf.get("retired"))
                 + " retired")
    lines.append("pairs      " + str(len(pairs)) + " over " + str(len(doc["texts"]))
                 + " distinct X texts, " + str(len(doc.get("labels") or {}))
                 + " labels")
    lines.append("sets       " + "  ".join(s + " " + str(per_set.get(s, 0)) for s in SETS))
    lines.append("x kinds    " + "  ".join(k + " " + str(per_kind.get(k, 0)) for k in X_KINDS))
    lines.append("per set    " + " · ".join(
        s + ": " + ", ".join(k + " " + str(per_both.get((s, k), 0))
                             for k in X_KINDS if per_both.get((s, k)))
        for s in SETS if per_set.get(s)) or "per set    none")
    windows = Counter(p["essence"] for p in pairs
                      if p["set"] == "fold" and p["x_kind"] == "raw")
    if windows:
        ws = sorted(windows.values())
        rows = sorted({(p["essence"], p["trail_rows"]) for p in pairs
                       if p["set"] == "fold" and p["x_kind"] == "raw"})
        rs = sorted(r for _, r in rows)
        lines.append("windows    " + str(len(windows)) + " essences with fold pairs: "
                     "min " + str(ws[0]) + ", median " + str(statistics.median(ws))
                     + ", max " + str(ws[-1]) + " windows each; rows per trail min "
                     + str(rs[0]) + ", median " + str(statistics.median(rs))
                     + ", max " + str(rs[-1])
                     + (" · " + str(c.get("essences_without_rows")) + " live "
                        "essences with no rows behind them"
                        if c.get("essences_without_rows") else ""))
    else:
        lines.append("windows    no fold pairs")
    sis = doc.get("sisters") or {}
    lines.append("sisters    " + str(len(sis)) + " essences have any"
                 + ("; the largest family " + str(1 + max(len(v) for v in sis.values()))
                    if sis else ""))
    co = [x for x in (doc.get("x_index") or []) if len(x.get("labels") or []) > 1]
    lines.append("co-labels  " + str(len(co)) + " X text" + ("" if len(co) == 1 else "s")
                 + " carr" + ("ies" if len(co) == 1 else "y") + " more than one label"
                 + ("; the most on one " + str(max(len(x["labels"]) for x in co))
                    if co else "")
                 + ("" if doc.get("x_index") is not None else " (no x_index in this file)"))
    mapped = doc.get("lineage") or {}
    dropped = doc.get("dropped") or []
    lines.append("lineage    " + str(len(mapped)) + " label"
                 + ("" if len(mapped) == 1 else "s") + " mapped forward to a live "
                 "successor" + (" (" + ", ".join(k + "→" + str(v) for k, v in mapped.items()) + ")"
                                if mapped else "")
                 + " · " + str(len(dropped)) + " pair" + ("" if len(dropped) == 1 else "s")
                 + " dropped for want of one"
                 + ((" (" + ", ".join("#" + str(d["essence"]) + " on turn " + str(d["turn"])
                                      for d in dropped) + ")") if dropped else "")
                 + ((" · ambiguous: " + ", ".join(k + "→" + str(v) for k, v in
                                                  (doc.get("ambiguous") or {}).items()))
                    if doc.get("ambiguous") else ""))
    miss = [p for p in pairs if p["set"] == "miss"]
    faults = [p for p in miss if p.get("writer_fault")]
    lines.append("miss       " + str(len(miss)) + " pair" + ("" if len(miss) == 1 else "s")
                 + " (" + str(per_both.get(("miss", "said"), 0)) + " said, "
                 + str(per_both.get(("miss", "raw"), 0)) + " raw) · the writer at fault on "
                 + str(len(faults)) + " said, about remembering and not a thing: "
                 + str(len(miss) - len(faults)) + " pair"
                 + ("" if len(miss) - len(faults) == 1 else "s") + " without them")
    told = [p for p in pairs if p["set"] in ("miss", "hers")]
    lines.append("miss/hers  " + str(len(told)) + " pair" + ("" if len(told) == 1 else "s")
                 + (", one per line:" if told else ""))
    for p in sorted(told, key=lambda p: (p["turn"] or 0, p["set"], p["x_kind"], p["x"])):
        lines.append("  " + p["set"].ljust(6) + " " + p["x_kind"].ljust(5)
                     + "  turn " + str(p["turn"]).ljust(5) + "  label " + str(p["label"]).ljust(5)
                     + (" (was " + str(p["mapped_from"]) + ")" if p.get("mapped_from") else "")
                     + '  "' + _short(doc["texts"][p["x"]]) + '"'
                     + ("  [writer fault]" if p.get("writer_fault") else ""))
    lines.append("ledger     recall events " + str(c.get("recall_events", 0))
                 + ", " + str(c.get("recall_with_query", 0)) + " with a query · fetches by id "
                 + str(c.get("fetches_by_id", 0)) + " (" + str(c.get("fetches_itself", 0))
                 + " whole, " + str(c.get("fetches_sources", 0)) + " for the rows behind): "
                 + str(c.get("fetch_named_titles", 0)) + " named, "
                 + str(c.get("fetch_named_set_aside", 0)) + " set aside by name, "
                 + str(c.get("fetch_miss", 0)) + " missed, "
                 + str(c.get("fetch_no_query_that_turn", 0)) + " with no query that turn, "
                 + str(c.get("fetch_no_recall_that_turn", 0)) + " with no recall that turn")
    lines.append("           searches " + str(c.get("searches", 0)) + ", "
                 + str(c.get("searches", 0) - c.get("searches_without_restatement", 0))
                 + " with a restatement: " + str(c.get("searches_found_named", 0))
                 + " found what the suggester had named, "
                 + str(c.get("searches_found_unnamed", 0)) + " found only what it had not, "
                 + str(c.get("searches_before_the_suggester", 0)) + " ran with no suggester · "
                 + str(c.get("search_hers", 0) + c.get("search_sanity", 0))
                 + " search-and-fetch pairs, " + str(c.get("searches_nothing_fetched", 0))
                 + " searches with nothing found then fetched")
    said = doc.get("said")
    if said is None:
        lines.append("said       not run (--said asks the query writer for one X per window)")
    elif said.get("not_run") and not said.get("asked"):
        lines.append("said       not written: " + str(said["not_run"]))
    else:
        fold_faults = sum(1 for p in pairs if p["set"] == "fold"
                          and p["x_kind"] == "said" and p.get("writer_fault"))
        lines.append("said       " + str(said.get("model")) + ": " + str(said.get("asked"))
                     + " of " + str(said.get("windows")) + " windows asked, "
                     + str(said.get("answered")) + " answered, " + str(said.get("empty"))
                     + " empty, " + str(said.get("failed")) + " failed, "
                     + str(said.get("pairs")) + " pairs in " + str(said.get("seconds")) + " s"
                     + ", the writer at fault on " + str(fold_faults)
                     + (" · " + str(said["not_run"]) if said.get("not_run") else ""))
    lines.append("vectors    " + (vectors_note or ("none (--embed reads every X into one)"
                                                    if not doc.get("vectors") else doc["vectors"])))
    notes = {k: v for k, v in c.items()
             if k.startswith(("trails_", "trail_rows_unread", "windows_", "miss_without",
                              "fetch_not", "fetch_same", "pairs_dropped",
                              "essences_without", "essences_with_unread",
                              "writer_fault", "texts_with_co"))}
    if notes:
        lines.append("notes      " + ", ".join(k.replace("_", " ") + " " + str(v)
                                                for k, v in sorted(notes.items())))
    return lines


def show(path) -> list:
    doc = load(path)
    _, note = vectors_of(path, doc)
    return describe(doc, path, vectors_note=note)


# -- the command line -----------------------------------------------------------

def _main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    what = (argv[1] if len(argv) > 1 else "").lower()
    try:
        if what == "build":
            args = argv[2:]
            opts = {"store": None, "out": None, "model": None}
            flags = {"said": False, "embed": False}
            i = 0
            while i < len(args):
                a = args[i]
                if a in ("--said", "--embed"):
                    flags[a[2:]] = True
                elif a in ("--store", "--out", "--model") and i + 1 < len(args):
                    opts[a[2:]] = args[i + 1]
                    i += 1
                else:
                    print("  build takes --store PATH, --out DIR, --model KEY, "
                          "--said and --embed, not " + repr(a))
                    return 2
                i += 1
            doc, where = build(store=opts["store"], out_dir=opts["out"],
                               said=flags["said"], embed_vectors=flags["embed"],
                               model=opts["model"], say=print)
            _, note = vectors_of(where, doc)
            print()
            for line in describe(doc, where, vectors_note=note):
                print(line)
            return 0
        if what == "show":
            if len(argv) < 3:
                print("  show needs a pairs file")
                return 2
            for line in show(argv[2]):
                print(line)
            return 0
    except Refused as exc:
        print("  refused: " + str(exc))
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
