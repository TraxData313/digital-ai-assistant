"""Lean on the pairs builder without a model, over a scratch folder:

    python -m server.test_adapt_pairs C:\\somewhere\\scratch

A small life is made in the scratch with the store's own functions: a
conversation, an essence folded from its first rows, an edit of that essence
(so the first is retired), an essence folded through two others, two
sisters over the same rows, an essence with nine rows behind it, an
essence over two said rows lying among three the suggester never reads (a
job, a world notice, a dream) and one over those three unread rows alone,
and a ledger of recall, fetch and search events in the exact shapes the
room writes -- including one fetch in the shape the ledger had before
`asked` existed, and one turn whose query is about remembering rather than
a thing. The query writer and the embedder are stood in for by hands, so
nothing here calls LM Studio or loads a model; the live store, the live
pairs folder and `data/` are never touched, and a scratch inside the repo
is refused.

What is checked: every set's count; X-raw is the last four rows labelled by
speaker and cut exactly as the query writer's prompt cuts them; three
windows for the nine-row trail and one for the short one; unread kinds are
left out of a trail before a window is cut, an essence with only unread
rows gives no pair and is counted, and every window's `kinds` are readable
ones; the window over the trail's last rows is marked `last`, a lone one
too; sisters listed both ways and never including self; a retired essence
is never a label and a fetch of it maps to its live successor; a miss
pair's X is the recall's query, one per subject, and its raw companion is
the four lines before that turn; a query about remembering is the writer's
fault on the miss and on the fold's said pairs, never on a raw companion,
and `show` counts the misses with and without; `x_index` lists every label
a text carries, with or without vectors; hers and sanity pairs classified
right, a search from before the suggester marked so; a recall with empty
titles and no fetch gives nothing; `lines_before` reads what `last_lines`
would have; the file round-trips; `--said` with a stubbed writer gives one
pair per subject under its window's meta; `--embed` with a stubbed
embedder writes an npz aligned to `x_index`; the build never writes to the
store; and `show` runs.
"""

import contextlib
import hashlib
import io
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

import numpy as np

from . import adapt_pairs as ap, db, recall

FAILED = []
DIM = 1024


def check(name, ok, detail=""):
    print("  " + ("ok  " if ok else "FAIL") + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- the room's own shapes, as the live ledger writes them ------------------

def recall_detail(query, titles=(), set_aside=(), missed=None, late=False):
    return {
        "by": "Gemma 4 E4B", "asked_for": "google/gemma-4-e4b",
        "answered_by": "google/gemma-4-e4b", "read": 4,
        "about": "the thing", "query": query, "keywords": [],
        "titles": [{"id": i, "title": "essence " + str(i), "date": "2026-09-01T00:00:00+00:00",
                    "score": 0.6, "first": "first words …", "in_hand": False}
                   for i in titles],
        "set_aside": [{"id": i, "title": "essence " + str(i),
                       "date": "2026-09-01T00:00:00+00:00", "because": "does not look related"}
                      for i in set_aside],
        "under_bar": False, "held_back": 3, "held_back_best": 0.5, "searched": 5,
        "best_score_seen": 0.6, "took_s": 1.0, "model_s": 0.5, "search_s": 0.2,
        "read_s": None, "note": None, "missed": missed,
        "settings": {"timeout_s": 15.0, "lines": 4, "line_tokens": 200, "titles_min": 3,
                     "titles_max": 7, "gap": 0.03, "suggest_floor": 0.55, "read_top": 7,
                     "read_tokens": 400, "floor": 0.45, "prompt_file": "server/recall_config.py"},
        "late": late, "before_reply": True,
    }


def fetch_detail(asked, brought_back=(), already_here=(), through=(), rounds=None):
    d = {
        "asked": list(asked), "brought_back": list(brought_back),
        "already_here": list(already_here), "not_found": [],
        "held_back_by_the_bound": [], "through_essences": list(through),
        "tokens_est": 100, "index": [], "project_notes": [], "project_activity": [],
        "notes": [], "problems": [],
        "limits": {"max_rows": 40, "max_tokens": 6000, "max_index_rows": 200},
        "summary": "reached back for " + str(len(brought_back)) + " rows",
    }
    if rounds:
        d["while_looking"] = rounds
        d["why"] = "going to look"
    return d


def search_detail(*searches):
    runs = []
    for restatement, hits, keywords in searches:
        runs.append({
            "asked": {"restatement": restatement, "keywords": list(keywords),
                      "from": None, "to": None, "limit": 10, "include_retired": False},
            "hits": [{"id": i, "title": "essence " + str(i),
                      "date": "2026-09-01T00:00:00+00:00", "score": 0.6, "matched": "meaning"}
                     for i in hits],
            "searched": 5, "floor": 0.45, "best_score_seen": 0.6,
            "arms": {"meaning": {"ran": True, "compared": 5, "over_the_floor": len(hits),
                                 "best_score": 0.6}},
            "not_scored": {}, "held_back_by_the_limit": [], "notes": [], "problems": [],
            "summary": "found " + str(len(hits)) + " of 5 essences",
        })
    return {"searches": runs, "problems": [], "floor": 0.45, "model": "BAAI/bge-m3",
            "summary": "searched " + str(len(runs)) + " ways"}


LONG = "start " + "middle " * 200 + "end"

# The query of a turn on which the writer pointed at the remembering and
# not at a thing -- a real turn's shape, the words changed.
Q2 = "He asked me to search my memory for what A had said."

# Two fault queries in the shapes the rule was written for, and clean ones
# beside them, so the rule is pinned to the cases it was written for.
FAULTS = ("He asked me to search my memory, suggesting I already had the answer he wanted.",
          "The boat on the lake—what I remember about it when he asked me to pull the memory for him.",
          "Помоли ме да си спомня "
          "какво каза за лодката")
CLEAN = ("Bread rises slowly in a cold kitchen; I learned that the trick is in waiting "
         "rather than adding more yeast when the dough looks flat.",
         "The watcher needs a fifth sense called `quiet`, which fires when nothing has happened at all.",
         "The automatic memory's floor of 0.45 and the suggest bar",
         "Pepper the cat and the kite he laughed about")


def make_a_store(conn) -> dict:
    """The life described at the top. Returns the ids by name."""
    ids = {}
    for i in range(1, 15):                              # rows 1-14
        db.add_row(conn, "user" if i % 2 else "assistant", "t" + str(i))
    ids["A"] = db.add_row(conn, "essence", "A: rows one to four", replaces=[1, 2, 3, 4], title="A")
    ids["A2"] = db.add_row(conn, "essence", "A, edited", replaces=[ids["A"]], title="A edited")
    ids["B"] = db.add_row(conn, "essence", "B: rows five to eight", replaces=[5, 6, 7, 8], title="B")
    ids["E"] = db.add_row(conn, "essence", "E: rows thirteen and fourteen", replaces=[13, 14], title="E")
    ids["F"] = db.add_row(conn, "essence", "F: B and E folded", replaces=[ids["B"], ids["E"]], title="F")
    ids["S1"] = db.add_row(conn, "essence", "S1: rows nine to twelve, one subject",
                           replaces=[9, 10, 11, 12], title="S1")
    ids["S2"] = db.add_row(conn, "essence", "S2: rows nine to twelve, the other subject",
                           replaces=[9, 10, 11, 12], title="S2")
    first = None
    for i in range(22, 31):                              # rows 22-30, nine of them
        rid = db.add_row(conn, "user" if i % 2 == 0 else "assistant",
                         LONG if i == 30 else "t" + str(i))
        first = first or rid
    assert first == 22
    ids["N"] = db.add_row(conn, "essence", "N: nine rows", replaces=list(range(22, 31)), title="N")
    assert ids["N"] == 31
    turns = {}
    for name, n in (("T1", 32), ("T2", 34), ("T3", 36), ("T4", 38), ("T5", 40), ("T6", 42)):
        rid = db.add_row(conn, "assistant", "the reply " + str(n))
        assert rid == n, (rid, n)
        turns[name] = rid
        db.add_row(conn, "user", "his line " + str(n + 1))
    ids.update(turns)
    A2, S1, S2, N, A, F = ids["A2"], ids["S1"], ids["S2"], ids["N"], ids["A"], ids["F"]

    # T1: the suggester named A2 and not S1; the assistant fetched both; its
    # search found N and A2. One fetch in the shape the ledger had on day one.
    db.add_event(conn, turns["T1"], "recall", "automatic memory: searched",
                 recall_detail("q1a ; q1b", titles=[A2]))
    db.add_event(conn, turns["T1"], "fetch", "reached back for 2 rows: #7, #8",
                 {"reloaded": [7, 8], "tokens_est": 20, "withheld": [],
                  "limits": {"max_rows": 40, "max_tokens": 6000}})
    db.add_event(conn, turns["T1"], "fetch", "while looking, round 1: reached back",
                 fetch_detail(["essence #" + str(S1) + " itself", "essence #" + str(A2) + " itself"],
                              brought_back=[S1], already_here=[A2], rounds=1))
    db.add_event(conn, turns["T1"], "search", "searched 1 way",
                 search_detail(("r1", [N, A2], [])))
    # T2: named N; the assistant fetched N (a confirmation) and the RETIRED A
    # (a miss, mapped to A2); its search found N and S1. The query that turn is the
    # writer's fault: about the remembering, not the thing.
    db.add_event(conn, turns["T2"], "recall", "automatic memory: searched",
                 recall_detail(Q2, titles=[N]))
    db.add_event(conn, turns["T2"], "fetch", "reached back",
                 fetch_detail(["essence #" + str(N) + " itself", "essence #" + str(A) + " itself"],
                              brought_back=[N, A]))
    db.add_event(conn, turns["T2"], "search", "searched 1 way",
                 search_detail(("r2", [N, S1], [])))
    # T3: it named nothing and nothing was fetched. A keyword-only search.
    db.add_event(conn, turns["T3"], "recall", "automatic memory: searched",
                 recall_detail("q3", titles=[]))
    db.add_event(conn, turns["T3"], "search", "searched 1 way",
                 search_detail((None, [S2], ["word"])))
    # T4: no suggester at all; the assistant's search found S1 and it was fetched.
    db.add_event(conn, turns["T4"], "search", "searched 1 way",
                 search_detail(("r4", [S1], [])))
    db.add_event(conn, turns["T4"], "fetch", "reached back",
                 fetch_detail(["essence #" + str(S1) + " itself"], brought_back=[S1]))
    # T5: named S2 and set S1 aside by name; the assistant reached for the rows behind
    # S2 and for S1 itself -- both confirmations.
    db.add_event(conn, turns["T5"], "recall", "automatic memory: searched",
                 recall_detail("q5", titles=[S2], set_aside=[S1]))
    db.add_event(conn, turns["T5"], "fetch", "reached back",
                 fetch_detail(["the rows behind essence #" + str(S2), "essence #" + str(S1) + " itself"],
                              brought_back=[9, 10, 11, 12, S1], through=[S2], rounds=1))
    # T6: the suggester ran out of time -- no query -- and A2 was fetched.
    db.add_event(conn, turns["T6"], "recall", "automatic memory: missed",
                 recall_detail(None, missed="it ran out of time at 15.0 s", late=True))
    db.add_event(conn, turns["T6"], "fetch", "reached back",
                 fetch_detail(["essence #" + str(A2) + " itself"], brought_back=[A2]))
    # Rows the suggester never reads, lying among two it does, and two
    # essences over them: U stands for all five, J for the three unread
    # ones alone.
    for i, (kind, text) in enumerate((("job", "a job ran"), ("user", "his line 45"),
                                      ("world", "a notice"), ("assistant", "the reply 47"),
                                      ("dream", "a dream")), 44):
        rid = db.add_row(conn, kind, text)
        assert rid == i, (rid, i)
    ids["U"] = db.add_row(conn, "essence", "U: two said rows among three unread",
                          replaces=[44, 45, 46, 47, 48], title="U")
    ids["J"] = db.add_row(conn, "essence", "J: unread rows only",
                          replaces=[44, 46, 48], title="J")
    conn.commit()
    return ids


def main():
    try:   # a failing check prints what it saw, arrows and Cyrillic included
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) < 2:
        print("  needs a scratch folder: python -m server.test_adapt_pairs <path>")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    root = Path(__file__).resolve().parent.parent
    if root == scratch or root in scratch.parents:
        print("  that scratch is inside the repo. Give me one outside it.")
        sys.exit(2)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

    db.DB_PATH = scratch / "store.db"
    ap.PAIRS_DIR = scratch / "pairs"
    conn = db.connect()
    ids = make_a_store(conn)
    A, A2, B, E, F, S1, S2, N, U, J = (ids[k] for k in ("A", "A2", "B", "E", "F", "S1", "S2", "N", "U", "J"))
    T1, T2, T3, T4, T5, T6 = (ids[k] for k in ("T1", "T2", "T3", "T4", "T5", "T6"))
    cfg = dict(recall.settings(), **{k: recall.defaults()[k] for k in ("lines", "line_tokens")})

    # -- the pieces on their own ----------------------------------------------
    print("pieces")
    check("nine rows give three windows, the first alone",
          ap.windows_of(range(1, 10)) == [[6, 7, 8, 9], [2, 3, 4, 5], [1]], ap.windows_of(range(1, 10)))
    check("eight give two, four give one, six give two",
          ap.windows_of(range(1, 9)) == [[5, 6, 7, 8], [1, 2, 3, 4]]
          and ap.windows_of(range(1, 5)) == [[1, 2, 3, 4]]
          and ap.windows_of(range(1, 7)) == [[3, 4, 5, 6], [1, 2]])
    check("thirteen give three and the first row is left out",
          ap.windows_of(range(1, 14)) == [[10, 11, 12, 13], [6, 7, 8, 9], [2, 3, 4, 5]])
    check("no rows give no windows", ap.windows_of([]) == [])
    check("the writer's fault: the two English ones and a Cyrillic one, and none of the clean",
          all(ap.writer_fault(q) for q in FAULTS) and not any(ap.writer_fault(q) for q in CLEAN),
          [ap.writer_fault(q) for q in FAULTS + CLEAN])
    check("the kinds the suggester reads are one tuple, in recall",
          recall.READ_KINDS == ("user", "assistant", "angel", "worker", "tell"), recall.READ_KINDS)
    lin = ap.Lineage(conn)
    check("live is what the shelf says",
          lin.live == {r["id"] for r in db.essence_shelf(conn)} == {A2, F, S1, S2, N, U, J}, lin.live)
    check("a retired essence maps forward: A to A2, B and E to F",
          lin.live_of(A) == A2 and lin.live_of(B) == F and lin.live_of(E) == F)
    check("a live one maps to itself, a said row to nothing",
          lin.live_of(N) == N and lin.live_of(1) is None and not lin.is_essence(1))
    check("no ambiguity in this life", lin.ambiguous == {}, lin.ambiguous)

    # line_of: who speaks, to a reader of words
    who = lambda kind, **meta: recall.line_of({"id": 0, "kind": kind, "text": "x", "meta": meta or None})["who"]
    check("speakers are labelled as the suggester labels them",
          who("user") == "Sam" and who("assistant") == "Ada" and who("angel") == "angel Ada"
          and who("user", who="lee") == "Leona" and who("tell") == "Ada, to a hand"
          and who("worker", name="reader") == "a hand (reader)" and who("worker") == "a hand"
          and who("world") == "world")
    pic = recall.line_of({"id": 0, "kind": "user", "text": "",
                          "meta": {"pictures": [{"name": "cat.png", "file": "0" * 16 + ".png"}]}})
    check("a picture-only line carries the picture's name", pic["text"] == "[picture: cat.png]", pic)

    # lines_before against last_lines, on the scratch
    last = recall.last_lines(conn, 4)
    before = recall.lines_before(conn, 4, 10 ** 9)
    check("lines_before past the end is last_lines", before == last and len(last) == 4, (before, last))
    got = [ln["id"] for ln in recall.lines_before(conn, 4, T1)]
    check("lines_before the first reply are the four said lines before it, essences skipped",
          got == [27, 28, 29, 30], got)
    got = recall.lines_before(conn, 4, T2)
    check("and before the second, the reply and his line are in",
          [ln["id"] for ln in got] == [29, 30, T1, 33] and got[2]["who"] == "Ada", got)

    # cut_lines under lines_text: the same cut, the same subject rule
    lines = recall.lines_before(conn, 4, T1)
    prompt = recall.lines_text([dict(ln) for ln in lines], cfg, "ASK")
    cut = recall.cut_lines([dict(ln) for ln in lines], cfg)
    check("lines_text still opens as it did and ends with the ask",
          prompt.startswith("The last 4 lines of the room, oldest first.") and prompt.endswith("\n\nASK"))
    check("the subject is the newest line, cut at both ends, marked",
          cut[-1]["role"] == "subject" and cut[-1]["cut"] and " … " in cut[-1]["text"]
          and ("THE SUBJECT, counts most -- Sam: " + cut[-1]["text"]) in prompt, cut[-1])
    check("the earlier lines are context, whole", all(c["role"] == "context" and not c["cut"] for c in cut[:-1]))
    check("a nod last makes the line before it the subject",
          [c["role"] for c in recall.cut_lines([{"who": "Ada", "text": "a real line"},
                                                 {"who": "Sam", "text": "ok, lets find that"}], cfg)]
          == ["subject", "nod"])
    raw = ap.raw_text(lines, cfg)
    check("X-raw is the labelled lines, cut the same way, and nothing else",
          raw == "\n".join(c["who"] + ": " + c["text"] for c in cut)
          and raw.endswith(cut[-1]["text"]) and "THE SUBJECT" not in raw, raw[:120])

    conn.close()

    # -- the build, read-only ------------------------------------------------
    print("build")
    store = scratch / "store.db"
    before_bytes = sha(store)
    doc, where = ap.build(store=store, out_dir=scratch / "pairs1", say=lambda *a: None)
    check("the store's bytes are what they were", sha(store) == before_bytes)
    check("and no journal was left beside it", not list(scratch.glob("store.db-*")))
    check("the file is under the scratch, stamped", where.parent == scratch / "pairs1"
          and where.name == doc["stamp"] + ".json" and where.is_file(), where)
    check("nothing landed in the repo's pairs folder", not (root / "data" / "adapter" / "pairs").exists()
          or not any((root / "data" / "adapter" / "pairs").glob(doc["stamp"] + "*")))
    pairs = doc["pairs"]
    per = Counter((p["set"], p["x_kind"]) for p in pairs)
    check("the sets: fold 9 raw, miss 3 said + 2 raw, hers 2, sanity 5 said + 2 hers",
          per == Counter({("fold", "raw"): 9, ("miss", "said"): 3, ("miss", "raw"): 2,
                          ("hers", "hers"): 2, ("sanity", "said"): 5, ("sanity", "hers"): 2}), per)
    check("twenty-three pairs, every set and kind known",
          len(pairs) == 23 and all(p["set"] in ap.SETS and p["x_kind"] in ap.X_KINDS for p in pairs))
    live = set(doc["shelf"]["live"])
    check("the shelf is the seven live essences, three retired",
          live == {A2, F, S1, S2, N, U, J} and doc["shelf"]["retired"] == 3 and doc["shelf"]["essences"] == 10)
    check("every label is live; no retired essence is ever one",
          all(p["label"] in live for p in pairs) and not any(p["label"] in (A, B, E) for p in pairs))
    check("the labels map carries a title and a date for each",
          set(doc["labels"]) == {str(i) for i in {p["label"] for p in pairs}}
          and doc["labels"][str(N)]["title"] == "N")

    # fold pairs and windows
    fold = [p for p in pairs if p["set"] == "fold"]
    per_essence = {}
    for p in fold:
        per_essence.setdefault(p["essence"], []).append(p["window"])
    check("three windows for the nine-row trail, newest first",
          per_essence.get(N) == [[27, 28, 29, 30], [23, 24, 25, 26], [22]], per_essence.get(N))
    check("one for the short one, through the edit to the rows",
          per_essence.get(A2) == [[1, 2, 3, 4]], per_essence.get(A2))
    check("the fold-through reaches both essences' rows, two windows",
          per_essence.get(F) == [[7, 8, 13, 14], [5, 6]], per_essence.get(F))
    check("the sisters have one window each, the same rows",
          per_essence.get(S1) == per_essence.get(S2) == [[9, 10, 11, 12]])
    u = [p for p in fold if p["essence"] == U]
    check("unread kinds are left out before the window is cut: U's window is its two said rows",
          per_essence.get(U) == [[45, 47]] and len(u) == 1 and u[0]["kinds"] == ["assistant", "user"]
          and u[0]["trail_rows"] == 5 and u[0]["trail_read"] == 2 and u[0]["windows"] == 1
          and doc["texts"][u[0]["x"]] == "Sam: his line 45\nAda: the reply 47", u)
    check("every window's kinds are ones the suggester reads",
          all(set(p["kinds"]) <= set(recall.READ_KINDS) for p in fold))
    check("an essence over unread rows alone gives no pair and is no label",
          U in per_essence and J not in per_essence and not any(p["label"] == J for p in pairs))
    last = [p for p in fold if p["last"]]
    check("the window over the trail's last rows is marked last, one per essence, a lone one too",
          sorted(p["essence"] for p in last) == sorted(per_essence)
          and all(p["window_no"] == 0 for p in last)
          and next(p["window"] for p in last if p["essence"] == N) == [27, 28, 29, 30]
          and next(p["window"] for p in last if p["essence"] == F) == [7, 8, 13, 14]
          and next(p["last"] for p in fold if p["essence"] == A2)
          and not any(p["last"] for p in fold if p["window_no"] > 0), [(p["essence"], p["window"]) for p in last])
    a2 = next(p for p in fold if p["essence"] == A2)
    check("X-raw is the last four rows with speaker labels",
          doc["texts"][a2["x"]] == "Sam: t1\nAda: t2\nSam: t3\nAda: t4", doc["texts"][a2["x"]])
    n_last = next(p for p in fold if p["essence"] == N and p["window_no"] == 0)
    check("the long subject line is cut both ends, as the prompt cuts it",
          doc["texts"][n_last["x"]].endswith("Sam: " + cut[-1]["text"]) and " … " in doc["texts"][n_last["x"]])
    check("a fold pair records what it should",
          n_last["label"] == N and n_last["windows"] == 3 and n_last["trail_rows"] == 9
          and n_last["trail_read"] == 9 and n_last["last"] is True
          and n_last["kinds"] == ["assistant", "user"] and n_last["turn"] is None
          and n_last["dt"] == n_last["label_dt"] and n_last["mapped_from"] is None
          and "writer_fault" not in n_last, n_last)

    # sisters
    # U and J share their unread rows: sisters over the whole trail, since
    # two essences over one job's rows stand over the same ground whether
    # or not a window is ever cut there.
    check("sisters listed both ways, never self; shared unread rows make sisters too",
          doc["sisters"] == {str(S1): [S2], str(S2): [S1], str(U): [J], str(J): [U]}
          and all(p["sisters"] == [S2] for p in pairs if p["label"] == S1)
          and all(p["sisters"] == [S1] for p in pairs if p["label"] == S2)
          and all(p["sisters"] == [J] for p in pairs if p["label"] == U)
          and all(p["label"] not in p["sisters"] for p in pairs), doc["sisters"])
    check("an essence with no sister carries an empty list",
          all(p["sisters"] == [] for p in pairs if p["label"] in (N, A2, F)))

    # miss pairs
    miss_said = [p for p in pairs if p["set"] == "miss" and p["x_kind"] == "said"]
    miss_raw = [p for p in pairs if p["set"] == "miss" and p["x_kind"] == "raw"]
    t1 = sorted((doc["texts"][p["x"]], p["subject"], p["subjects"]) for p in miss_said if p["turn"] == T1)
    check("the miss X is the recall's query, one pair per subject",
          t1 == [("q1a", 0, 2), ("q1b", 1, 2)] and all(p["label"] == S1 for p in miss_said if p["turn"] == T1), t1)
    check("its raw companion is the four lines before that turn",
          [p["window"] for p in miss_raw if p["turn"] == T1] == [[27, 28, 29, 30]]
          and doc["texts"][next(p for p in miss_raw if p["turn"] == T1)["x"]] == raw)
    t2 = [p for p in miss_said if p["turn"] == T2]
    check("the fetch of the retired essence is a miss on its live successor, and says so",
          len(t2) == 1 and t2[0]["label"] == A2 and t2[0]["mapped_from"] == A
          and doc["texts"][t2[0]["x"]] == Q2 and doc["lineage"] == {str(A): A2}, (t2, doc["lineage"]))
    check("and its raw companion reads past the earlier reply",
          [p["window"] for p in miss_raw if p["turn"] == T2] == [[29, 30, T1, 33]])
    check("a miss pair carries the recall event, its time, and how the assistant reached",
          all(p["event"] and p["dt"] and p["how"] == "itself" and p["named_how"] is None
              for p in miss_said + miss_raw))
    check("the query about remembering is the writer's fault, on the miss and on the sanity pair it also made",
          t2[0]["writer_fault"] is True
          and all(p["writer_fault"] is True for p in pairs if p["x_kind"] == "said" and p["turn"] == T2)
          and len([p for p in pairs if p["x_kind"] == "said" and p["turn"] == T2]) == 2)
    check("the other queries are not, and no raw companion ever is; hers carry no mark",
          all(p["writer_fault"] is False for p in miss_said if p["turn"] == T1)
          and all(p["writer_fault"] is False for p in miss_raw)
          and all("writer_fault" not in p for p in pairs if p["x_kind"] == "hers"))

    # hers and sanity
    hers = sorted((p["label"], p["turn"], p["fetched_on"], p["recall_ran"], doc["texts"][p["x"]])
                  for p in pairs if p["set"] == "hers")
    check("hers: the assistant's search found N, fetched next turn, unnamed by that turn's recall",
          hers[1] == (N, T1, T2, True, "r1"), hers)
    check("hers: a search from before the suggester, fetched the same turn",
          hers[0] == (S1, T4, T4, False, "r4"), hers)
    sane_said = sorted((p["label"], p["turn"], p["named_how"], p["how"], doc["texts"][p["x"]])
                       for p in pairs if p["set"] == "sanity" and p["x_kind"] == "said")
    check("sanity from fetches: A2 twice on T1 (two queries), N on T2, S2 via its rows and S1 set aside on T5",
          sane_said == [(A2, T1, "titles", "itself", "q1a"), (A2, T1, "titles", "itself", "q1b"),
                        (S1, T5, "set_aside", "itself", "q5"), (S2, T5, "titles", "sources", "q5"),
                        (N, T2, "titles", "itself", Q2)], sane_said)
    sane_hers = sorted((p["label"], p["turn"], doc["texts"][p["x"]])
                       for p in pairs if p["set"] == "sanity" and p["x_kind"] == "hers")
    check("sanity from searches: A2 found by r1 and named, N found by r2 and named",
          sane_hers == [(A2, T1, "r1"), (N, T2, "r2")], sane_hers)
    check("the same label is not paired twice with one search (A and A2 both fetched)",
          len([p for p in pairs if p["x_kind"] == "hers" and p["turn"] == T1]) == 2)
    check("empty titles and no fetch give nothing; a recall with no query gives nothing",
          not any(p["turn"] in (T3, T6) for p in pairs))
    c = doc["counts"]
    check("the counts say what happened",
          c["recall_events"] == 5 and c["recall_with_query"] == 4 and c["recall_without_query"] == 1
          and c["fetches_by_id"] == 8 and c["fetches_itself"] == 7 and c["fetches_sources"] == 1
          and c["fetch_named_titles"] == 3 and c["fetch_named_set_aside"] == 1 and c["fetch_miss"] == 2
          and c["fetch_no_query_that_turn"] == 1 and c["fetch_no_recall_that_turn"] == 1
          and c["searches"] == 4 and c["searches_without_restatement"] == 1
          and c["searches_found_named"] == 2 and c["searches_before_the_suggester"] == 1
          and c["search_hers"] == 2 and c["search_sanity"] == 2
          and c["essences_live"] == 7 and c["fold_windows"] == 9 and c["pairs"] == 23, c)
    check("and the notes count the unread rows, the essence with no fold pair, the faults, the co-labels",
          c["essences_with_unread_rows"] == 2 and c["trail_rows_unread"] == 6
          and c["essences_without_readable_rows"] == 1 and c["essences_without_fold_pairs"] == 1
          and c["essences_with_pairs"] == 6 and c.get("essences_without_rows", 0) == 0
          and c["writer_fault_miss"] == 1 and c["writer_fault_said"] == 2
          and c["texts_with_co_labels"] == 7, c)
    check("nothing dropped, nothing said, no vectors",
          doc["dropped"] == [] and doc["said"] is None and doc["vectors"] is None)
    check("texts are kept once: q1a is one text for two pairs",
          doc["texts"].count("q1a") == 1 and len({p["x"] for p in pairs if doc["texts"][p["x"]] == "q1a"}) == 1)

    # co-labels: every label a text carries, on x_index, with no vectors yet
    xi = doc["x_index"]
    by_x = {}
    for p in pairs:
        by_x.setdefault(p["x"], set()).add(p["label"])
    check("x_index is there without vectors: one entry per text, its hash, no count, no mark",
          len(xi) == len(doc["texts"]) and all(x["hash"] == db.text_hash(t) for x, t in zip(xi, doc["texts"]))
          and all(x["n_tokens"] is None and x["truncated"] is None for x in xi))
    check("and lists every label attached to each text, across the sets",
          all(x["labels"] == sorted(by_x.get(i, ())) for i, x in enumerate(xi)))
    labels_of = lambda text: xi[doc["texts"].index(text)]["labels"]
    check("q1a carries the miss's S1 and the sanity's A2; q5 the two it named; Q2 the miss and the confirmation",
          labels_of("q1a") == sorted([S1, A2]) and labels_of("q5") == sorted([S1, S2])
          and labels_of(Q2) == sorted([A2, N]), (labels_of("q1a"), labels_of("q5"), labels_of(Q2)))
    check("the sisters' shared window carries both; N's last window is also the T1 miss's raw, so both",
          labels_of(doc["texts"][next(p["x"] for p in fold if p["essence"] == S1)]) == sorted([S1, S2])
          and labels_of(raw) == sorted([S1, N]), (labels_of(raw)))
    check("seven texts carry more than one label, none more than two",
          sum(1 for x in xi if len(x["labels"]) > 1) == 7 and max(len(x["labels"]) for x in xi) == 2)

    # round trip and show
    check("the file round-trips", ap.load(where) == doc)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = ap._main(["adapt_pairs", "show", str(where)])
    shown = out.getvalue()
    check("show runs and says the counts",
          code == 0 and "sets       fold 9  miss 5  hers 2  sanity 7" in shown
          and "x kinds    raw 11  said 8  hers 4" in shown
          and "lineage    1 label mapped forward to a live successor (" + str(A) + "→" + str(A2) + ")" in shown
          and "sisters    4 essences have any; the largest family 2" in shown
          and "co-labels  7 X texts carry more than one label; the most on one 2" in shown
          and "vectors    none" in shown, shown)
    check("show counts the misses with and without the writer's faults, and marks them in the list",
          "miss       5 pairs (3 said, 2 raw) · the writer at fault on 1 said, about remembering "
          "and not a thing: 4 pairs without them" in shown
          and shown.count("[writer fault]") == 1
          and any(ln.endswith("[writer fault]") and ' label ' + str(A2) in ln and "miss   said"
                  in ln for ln in shown.splitlines()), shown)
    check("show lists the miss and hers pairs one per line",
          shown.count("\n  miss ") == 5 and shown.count("\n  hers ") == 2 and '"q1a"' in shown, shown)
    check("show's notes carry the new counts",
          "essences without fold pairs 1" in shown and "essences without readable rows 1" in shown
          and "trail rows unread 6" in shown and "writer fault miss 1" in shown
          and "texts with co labels 7" in shown, shown)
    try:
        ap.write_file(dict(doc), scratch / "pairs1")
        check("a stamp that exists is refused", False, "it was written over")
    except ap.Refused as exc:
        check("a stamp that exists is refused", "already" in str(exc), exc)
    try:
        ap.build(store=scratch / "nowhere.db", out_dir=scratch / "pairs0")
        check("a store that is not there is refused", False)
    except ap.Refused as exc:
        check("a store that is not there is refused", "no store" in str(exc), exc)

    # -- --said, with a hand for the writer ------------------------------------
    print("--said")
    asked = []

    def fake_ask(key, lines, cfg_, told):
        asked.append([ln["id"] for ln in lines])
        if lines[0]["id"] == 9:          # the sisters' window: one failure
            raise recall.LMError("the model answered nothing usable")
        first = "said about "
        if lines[0]["id"] == 45:         # U's window: the writer at fault
            first = "he asked me to remember what was said about "
        return {"subjects": [{"about": "a", "query": first + str(lines[-1]["id"])},
                             {"about": "b", "query": "second thought on " + str(lines[-1]["id"])}],
                "query": "joined", "about": "a ; b", "flaw": None, "still_flawed": None,
                "used_about": False, "raw": "{}", "sent": "...", "answered_by": key,
                "stats": {}}

    doc2, where2 = ap.build(store=store, out_dir=scratch / "pairs2", said=True, model="stub-model",
                            ask=fake_ask, ready=lambda key: None, say=lambda *a: None)
    check("the store is still untouched", sha(store) == before_bytes)
    said = [p for p in doc2["pairs"] if p["set"] == "fold" and p["x_kind"] == "said"]
    check("every window was asked once, in the order of the fold pairs",
          asked == [p["window"] for p in doc2["pairs"] if p["set"] == "fold" and p["x_kind"] == "raw"], asked)
    check("two said pairs per answered window; the two sister windows failed",
          len(said) == 14 and doc2["said"]["asked"] == 9 and doc2["said"]["answered"] == 7
          and doc2["said"]["failed"] == 2 and doc2["said"]["pairs"] == 14 and doc2["said"]["model"] == "stub-model",
          doc2["said"])
    raw_of = {(p["essence"], tuple(p["window"])): p for p in doc2["pairs"]
              if p["set"] == "fold" and p["x_kind"] == "raw"}
    check("each said pair sits under its window's essence with the same window",
          all((p["essence"], tuple(p["window"])) in raw_of and p["label"] == p["essence"] for p in said))
    check("and carries its window's meta: last, the kinds, the counts",
          all(p[k] == raw_of[(p["essence"], tuple(p["window"]))][k]
              for p in said for k in ("last", "kinds", "window_no", "windows", "trail_rows", "trail_read")))
    check("subjects are numbered and the queries are the writer's",
          sorted((p["subject"], p["subjects"]) for p in said) == [(0, 2)] * 7 + [(1, 2)] * 7
          and all(doc2["texts"][p["x"]].endswith(str(p["window"][-1])) for p in said))
    check("the fold's said pairs carry the writer's fault too: U's first, and no other",
          [p["essence"] for p in said if p["writer_fault"]] == [U]
          and doc2["counts"]["writer_fault_said"] == 3 and doc2["counts"]["writer_fault_miss"] == 1)
    check("the other sets are as before", Counter((p["set"], p["x_kind"]) for p in doc2["pairs"] if not
                                                    (p["set"] == "fold" and p["x_kind"] == "said")) == per)
    doc3, _ = ap.build(store=store, out_dir=scratch / "pairs3", said=True, model="gone",
                       ask=fake_ask, ready=lambda key: "the model gone is not on this disk",
                       say=lambda *a: None)
    check("a writer that is not there: no said pairs, said in words",
          doc3["said"]["not_run"] == "the model gone is not on this disk" and doc3["said"]["asked"] == 0
          and not any(p["x_kind"] == "said" and p["set"] == "fold" for p in doc3["pairs"]), doc3["said"])

    # -- --embed, with a hand for the embedder --------------------------------
    print("--embed")
    longest = max(doc2["texts"], key=len)

    def fake_read(texts):
        out = []
        for t in texts:
            seed = int(hashlib.sha256(t.encode("utf-8")).hexdigest()[:8], 16)
            v = np.random.default_rng(seed).standard_normal(DIM)
            v /= np.linalg.norm(v)
            out.append({"vec": [float(x) for x in v], "dim": DIM, "n_tokens": len(t.split()),
                        "truncated": t == longest, "hash": db.text_hash(t)})
        return out

    doc4, where4 = ap.build(store=store, out_dir=scratch / "pairs4", said=True, model="stub-model",
                            ask=fake_ask, ready=lambda key: None, embed_vectors=True, read=fake_read,
                            say=lambda *a: None)
    npz = where4.with_suffix(".npz")
    check("the npz is named in the json and is there", doc4["vectors"] == npz.name and npz.is_file())
    with np.load(npz) as got:
        vectors, hashes = got["vectors"], [str(h) for h in got["hashes"]]
    check("one vector per distinct text, in order, float32",
          vectors.shape == (len(doc4["texts"]), DIM) and vectors.dtype == np.float32
          and hashes == [db.text_hash(t) for t in doc4["texts"]])
    check("x_index is aligned: the hash of each text, the count, the mark",
          [x["hash"] for x in doc4["x_index"]] == hashes
          and all(x["n_tokens"] == len(t.split()) for x, t in zip(doc4["x_index"], doc4["texts"]))
          and sum(1 for x in doc4["x_index"] if x["truncated"]) == 1
          and doc4["x_index"][doc4["texts"].index(longest)]["truncated"])
    by_x4 = {}
    for p in doc4["pairs"]:
        by_x4.setdefault(p["x"], set()).add(p["label"])
    check("and the labels are still on it after the embed pass, the said texts' too",
          all(x["labels"] == sorted(by_x4.get(i, ())) for i, x in enumerate(doc4["x_index"]))
          and doc4["x_index"][doc4["texts"].index("said about 4")]["labels"] == [A2])
    check("the vectors are the hand's, by text",
          all(np.allclose(vectors[i], fake_read([t])[0]["vec"], atol=1e-6)
              for i, t in enumerate(doc4["texts"])))
    got, note = ap.vectors_of(where4)
    check("vectors_of reads them back and says how many",
          got is not None and got.shape == vectors.shape and "1 truncated" in note, note)
    check("a pair's x still points into the same list the vectors follow",
          all(0 <= p["x"] < vectors.shape[0] for p in doc4["pairs"]))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ap._main(["adapt_pairs", "show", str(where4)])
    check("show says the vectors and the said pass",
          "vectors    " + npz.name + ": " + str(len(doc4["texts"])) + " vectors of 1024, 1 truncated" in out.getvalue()
          and "said       stub-model: 9 of 9 windows asked, 7 answered, 0 empty, 2 failed, 14 pairs" in out.getvalue()
          and " s, the writer at fault on 1" in out.getvalue(),
          out.getvalue())
    npz.unlink()
    _, note = ap.vectors_of(where4)
    check("a missing npz is said, not crashed", "not there" in note, note)
    check("the store is untouched after everything", sha(store) == before_bytes)

    shutil.rmtree(scratch, ignore_errors=True)
    if FAILED:
        print(chr(10) + "  " + str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print(chr(10) + "  every rule held")


if __name__ == "__main__":
    main()
