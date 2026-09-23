"""Lean on the adapter without a model, over a scratch folder:

    python -m server.test_adapt C:\\somewhere\\scratch

A store of a handful of essences is made in the scratch, each read into a
made-up unit vector of the embedder's own width, and `embed.read` is stood in
for by a hand that returns a chosen probe -- so nothing here loads the model,
and nothing touches `data/`: the store, the adapter folder and the backup all
point into the scratch, and a scratch inside the repo is refused.

What is checked: with no adapter the report is what it was before this
existed, its scores the plain cosine; an identity adapter gives the same
scores back; centering moves them, and every hit then carries the base score
beside its own; the shadow -- an essence the base space clears and the
adapted one hides -- comes after the hits marked as the shadow's, with the
disagreement said by id and counted in the arms; the stamp and the version's
own floor ride the report; a version made over another revision is not
applied, said in words, and changes no score; `use` writes `current` and
`use none` removes it, and the running code sees a changed `current` without
a restart; a name that exists is refused; the centering mean is the mean of
the shelf; the suggester's bar comes from the live version when it carries
one and the knob otherwise, and its arm never shows a shadow hit; `floor_now`
answers rightly under every state; and the versions ride out in the backup
zip beside the Spark.
"""

import contextlib
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

import numpy as np

from . import adapt, backup, db, embed, recall, search

FAILED = []
DIM = 1024

# What a search report carried before the adapter existed. The no-adapter
# report may add `adapter` and nothing else.
OLD_KEYS = {"asked", "hits", "searched", "floor", "best_score_seen", "arms",
            "not_scored", "held_back_by_the_limit", "notes", "problems",
            "summary"}


def check(name, ok, detail=""):
    print("  " + ("ok  " if ok else "FAIL") + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def make_vectors(rng):
    """Five made-up essences with the shape hers have: a part every one
    shares, and a part of its own. The shared part is what centering
    subtracts, and the arrangement is chosen so that one essence resembles
    the probe only through it -- the shadow's case."""
    shared = unit(rng.standard_normal(DIM))
    own = []
    for _ in range(5):
        v = rng.standard_normal(DIM)
        v -= v.dot(shared) * shared
        for o in own:
            v -= v.dot(o) * o
        own.append(unit(v))
    a, b, c, d, e = own
    vectors = {
        "A": unit(0.7 * shared + 0.6 * a + 0.3 * c),   # what the probe is about
        "B": unit(0.8 * shared + 0.6 * b),             # like the probe only in the shared part
        "C": unit(0.7 * shared + 0.7 * c),             # a little of its own, mostly shared
        "D": unit(0.3 * shared + 0.95 * d),            # nothing to do with it
        "E": unit(0.7 * shared + 0.5 * a + 0.5 * e),   # about it, and about something else
    }
    probe = unit(0.7 * shared + 0.7 * a)
    return vectors, probe


def scores_of(report):
    return {h["id"]: h["score"] for h in report["hits"] if not h.get("shadow")}


def main():
    if len(sys.argv) < 2:
        print("  needs a scratch folder: python -m server.test_adapt <path>")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    root = Path(__file__).resolve().parent.parent
    if root == scratch or root in scratch.parents:
        print("  that scratch is inside the repo. Give me one outside it.")
        sys.exit(2)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

    db.DB_PATH = scratch / "store.db"
    adapt.ADAPTER_DIR = scratch / "adapter"
    current_file = scratch / "adapter" / adapt.CURRENT

    # -- a shelf of five, and a hand in place of the model ----------------
    rng = np.random.default_rng(20260904)
    made, probe = make_vectors(rng)
    conn = db.connect()
    ids = {}
    for name, vec in made.items():
        text = "2026-09-04. An essence about " + name + "."
        rid = db.add_row(conn, "essence", text, title="essence " + name)
        db.put_vector(conn, rid, embed.MODEL, DIM, vec, db.text_hash(text))
        ids[name] = rid
    probe_list = [float(x) for x in probe]

    def fake_read(texts):
        return [{"vec": list(probe_list), "dim": DIM, "n_tokens": 3,
                 "truncated": False, "hash": db.text_hash(t)} for t in texts]
    embed.read = fake_read

    # The stored vectors, as the search reads them: float32 on the disk.
    stored = db.vectors_for(conn, embed.MODEL)
    exp = {i: search.cosine(probe_list, stored[i]["vec"]) for i in stored}
    spec = {"restatement": "what A is about", "limit": 10}

    # -- no adapter: the report as it was ------------------------------------
    print("no adapter")
    check("nothing is named and nothing is live",
          adapt.current() is None and adapt.problem() is None and not current_file.exists())
    check("why_not says so in words", "nothing is named" in (adapt.why_not() or ""),
          adapt.why_not())
    r0 = search.run(conn, spec)
    over = sorted((i for i in exp if exp[i] >= search.FLOOR), key=lambda i: (-exp[i], i))
    check("the hits are the plain cosine over the floor, in order",
          [h["id"] for h in r0["hits"]] == over, [h["id"] for h in r0["hits"]])
    check("each score is the plain cosine, rounded as before",
          all(h["score"] == round(exp[h["id"]], 3) for h in r0["hits"]),
          scores_of(r0))
    check("the best seen is the plain cosine's best",
          r0["best_score_seen"] == round(max(exp.values()), 3)
          and r0["arms"]["meaning"]["best_score"] == round(max(exp.values()), 3))
    check("the report carries only the old keys and the stamp",
          set(r0) == OLD_KEYS | {"adapter"}, sorted(set(r0) ^ (OLD_KEYS | {"adapter"})))
    check("the stamp says no adapter", r0["adapter"] is None)
    check("no hit carries a base score or a shadow mark",
          all("base_score" not in h and "shadow" not in h for h in r0["hits"]))
    check("the floor is the base floor", r0["floor"] == search.FLOOR)
    check("only the meaning arm ran", set(r0["arms"]) == {"meaning"}, sorted(r0["arms"]))
    check("nothing was said about adapters", r0["notes"] == [], r0["notes"])
    check("the identity on purpose is the same report", search.run(conn, spec, adapter=False) == r0)
    check("the shadow's case is on the shelf: B clears the base floor",
          exp[ids["B"]] >= search.FLOOR, exp[ids["B"]])
    check("floor_now is the base floor", search.floor_now() == search.FLOOR)

    # -- an identity adapter: the same scores through the map ----------------
    print("identity adapter")
    zero = np.zeros((DIM, 4), dtype=np.float32)
    meta = adapt.write("ident", zero, zero, np.zeros(DIM), note="the identity")
    check("written with the loaded embedder's name and revision",
          meta["model"] == embed.MODEL and meta["revision"] == embed.REVISION and meta["rank"] == 4)
    check("the files are there",
          (scratch / "adapter" / "ident.npz").is_file() and (scratch / "adapter" / "ident.json").is_file())
    ident = adapt.load("ident")
    check("it knows it is the identity", ident.is_identity and ident.rank == 4 and ident.dim == DIM)
    # The map re-normalises, and the stored vectors are float32 on the disk,
    # so their length is one to about eight places and no further: the raw
    # scores agree to float32 precision, and the reported ones to the bit.
    raw = ident.apply_many([stored[i]["vec"] for i in sorted(stored)]) @ ident.apply(probe_list)
    check("through the map, the raw scores are the plain cosine to float32 precision",
          all(abs(raw[k] - exp[i]) < 1e-6 for k, i in enumerate(sorted(stored))),
          max(abs(raw[k] - exp[i]) for k, i in enumerate(sorted(stored))))
    r1 = search.run(conn, spec, adapter=ident)
    check("the hits and their scores are the same", scores_of(r1) == scores_of(r0)
          and [h["id"] for h in r1["hits"]] == [h["id"] for h in r0["hits"]], scores_of(r1))
    check("every hit carries its base score, equal to its own",
          all(h["base_score"] == h["score"] for h in r1["hits"]))
    check("the stamp names it", r1["adapter"] == "ident")
    check("with no floor of its own, the base floor stands", r1["floor"] == search.FLOOR)
    check("the shadow arm ran and disagreed on nothing",
          r1["arms"]["shadow"]["ran"] and r1["arms"]["shadow"]["disagreed"] == 0
          and r1["arms"]["shadow"]["floor"] == search.FLOOR, r1["arms"].get("shadow"))
    check("no shadow hit, no disagreement note",
          not any(h.get("shadow") for h in r1["hits"]) and r1["notes"] == [], r1["notes"])
    check("base_best_seen rides beside best_score_seen",
          r1.get("base_best_seen") == r1["best_score_seen"])
    check("floor_now asked of it directly is the base floor", search.floor_now(ident) == search.FLOOR)

    # -- centering: the mean of the shelf, and the shadow ----------------------
    print("centering")
    meta = adapt.centering("cent1", conn=conn)
    cent = adapt.load("cent1")
    mean = np.mean([stored[i]["vec"] for i in stored], axis=0)
    check("rank zero, over five essences", cent.rank == 0 and meta["numbers"]["essences"] == 5, meta["numbers"])
    check("its mean is the mean of the shelf's vectors",
          float(np.max(np.abs(cent.mean - mean))) < 1e-6, float(np.max(np.abs(cent.mean - mean))))
    check("no floor or bar until measured", cent.floor is None and cent.bar is None)
    adapt.stamp("cent1", floor=0.3, bar=0.4, numbers={"how": "by hand, for the bench"})
    cent = adapt.load("cent1")
    check("stamped floor and bar are read back", cent.floor == 0.3 and cent.bar == 0.4
          and cent.meta["numbers"]["essences"] == 5 and cent.meta["numbers"]["how"], cent.meta)
    r2 = search.run(conn, spec, adapter=cent)
    own = [h for h in r2["hits"] if not h.get("shadow")]
    shadow = [h for h in r2["hits"] if h.get("shadow")]
    check("the stamp names the version and the floor is the version's own",
          r2["adapter"] == "cent1" and r2["floor"] == 0.3, (r2["adapter"], r2["floor"]))
    check("centering moved the scores", any(h["score"] != h["base_score"] for h in own),
          [(h["score"], h["base_score"]) for h in own])
    check("every hit carries the base score, which is today's cosine",
          all(h["base_score"] == round(exp[h["id"]], 3) for h in r2["hits"]))
    check("A and E are the adapted hits", {h["id"] for h in own} == {ids["A"], ids["E"]},
          {h["id"]: h["score"] for h in own})
    check("B, cleared by the base space only, is the shadow's",
          any(h["id"] == ids["B"] for h in shadow), [h["id"] for h in shadow])
    b = next((h for h in shadow if h["id"] == ids["B"]), None)
    check("it is marked shadow, matched shadow, under the adapted floor, over the base one",
          b is not None and b["shadow"] is True and b["matched"] == "shadow"
          and b["score"] < 0.3 and b["base_score"] >= search.FLOOR, b)
    check("the shadow comes after the adapted hits",
          [bool(h.get("shadow")) for h in r2["hits"]] == [False] * len(own) + [True] * len(shadow))
    check("the shadow is ordered by base score",
          [h["base_score"] for h in shadow] == sorted((h["base_score"] for h in shadow), reverse=True))
    note = next((n for n in r2["notes"] if "disagreed" in n), "")
    check("a note says how many they disagreed on and names the shadow by id",
          ("disagreed on " + str(len(shadow))) in note
          and all(("#" + str(h["id"])) in note for h in shadow), note)
    check("the arms carry the shadow: how many, and the base floor",
          r2["arms"]["shadow"]["shown"] == len(shadow)
          and r2["arms"]["shadow"]["disagreed"] == len(shadow)
          and r2["arms"]["shadow"]["floor"] == search.FLOOR
          and r2["arms"]["shadow"]["over_the_floor"] == len(over), r2["arms"]["shadow"])
    check("best_score_seen is the adapted space's, base_best_seen the base's",
          r2["best_score_seen"] == round(max(h["score"] for h in own), 3)
          and r2["base_best_seen"] == round(max(exp.values()), 3))
    check("the summary says what the shadow adds",
          "the shadow adds " + str(len(shadow)) in r2["summary"], r2["summary"])
    check("floor_now asked of it directly is its own", search.floor_now(cent) == 0.3)
    out = search.measure(conn, say=lambda *a: None, adapter=cent)
    check("measure runs under a handed-in version and says so",
          out["adapter"] == "cent1" and out["floor"] == 0.3, out)
    out = search.measure(conn, say=lambda *a: None)
    check("and with none live, in the base space", out["adapter"] is None and out["floor"] == search.FLOOR)

    # -- a version over another revision: not applied, said in words ---------
    print("mismatch")
    adapt.write("other", zero, zero, np.zeros(DIM), revision="0" * 40, floor=0.2)
    current_file.write_text("other\n", encoding="utf-8")
    check("current names it, nothing is live", adapt.current() is None and adapt.named() == "other")
    why = adapt.why_not() or ""
    check("why_not names both revisions and the version",
          "other" in why and (embed.REVISION or "")[:12] in why and "0" * 12 in why
          and "not applied" in why, why)
    st = adapt.status()
    check("status says the problem, the named one, and that none is live",
          st["problem"] == why and st["current"] is None and st["named"] == "other", st)
    check("and lists it as not applying",
          next(v for v in st["versions"] if v["name"] == "other")["applies"] is False)
    r3 = search.run(conn, spec)
    check("the search runs in the base space, the scores untouched",
          scores_of(r3) == scores_of(r0) and r3["adapter"] is None
          and not any("base_score" in h for h in r3["hits"]))
    check("and says so in its notes", r3["notes"] == [why], r3["notes"])
    check("floor_now falls back to the base floor", search.floor_now() == search.FLOOR
          and r3["floor"] == search.FLOOR)
    try:
        adapt.use("other")
        check("use refuses a version that would never be applied", False, "it was taken")
    except adapt.Refused as exc:
        check("use refuses a version that would never be applied", "not applied" in str(exc), exc)

    # -- use, and seeing a change without a restart --------------------------
    print("use")
    st = adapt.use("cent1")
    check("use writes current", current_file.read_text(encoding="utf-8").strip() == "cent1"
          and st["current"] == "cent1" and st["problem"] is None, st)
    check("the live one is seen", adapt.current() is not None and adapt.current().name == "cent1")
    check("floor_now is the version's own", search.floor_now() == 0.3)
    r4 = search.run(conn, spec)
    check("a search asked for the live one runs under it",
          r4["adapter"] == "cent1" and r4["floor"] == 0.3 and r4["hits"] == r2["hits"])
    current_file.write_text("ident\n", encoding="utf-8")
    check("a changed current is seen without a restart",
          adapt.current() is not None and adapt.current().name == "ident"
          and search.floor_now() == search.FLOOR)
    st = adapt.use(None)
    check("use none removes the file", not current_file.exists() and st["current"] is None)
    check("nothing is live, and that is not a problem",
          adapt.current() is None and adapt.problem() is None and search.floor_now() == search.FLOOR)
    check("use none again is quiet", adapt.use("none")["current"] is None)
    current_file.write_text("ghost\n", encoding="utf-8")
    check("a current naming a version that is not there: identity, in words",
          adapt.current() is None and "ghost" in (adapt.problem() or "")
          and "not there" in (adapt.problem() or ""), adapt.problem())
    r5 = search.run(conn, spec)
    check("and the search says it", r5["adapter"] is None and r5["notes"] == [adapt.problem()])
    (scratch / "adapter" / "junk.npz").write_bytes(b"not a map at all")
    (scratch / "adapter" / "junk.json").write_text(json.dumps(
        {"name": "junk", "model": embed.MODEL, "revision": embed.REVISION, "rank": 0}),
        encoding="utf-8")
    current_file.write_text("junk\n", encoding="utf-8")
    check("an unreadable version: identity, in words",
          adapt.current() is None and "could not be read" in (adapt.problem() or ""), adapt.problem())
    current_file.unlink()
    try:
        adapt.use("ghost")
        check("use refuses a name that is not there", False, "it was taken")
    except adapt.Refused as exc:
        check("use refuses a name that is not there", "no adapter named ghost" in str(exc), exc)

    # -- a map over the wrong width: the search survives ---------------------
    narrow = np.zeros((8, 0), dtype=np.float32)
    adapt.write("narrow", narrow, narrow, np.zeros(8))
    r6 = search.run(conn, spec, adapter=adapt.load("narrow"))
    check("a map that cannot be laid over the vectors falls back to identity",
          r6["adapter"] is None and scores_of(r6) == scores_of(r0)
          and any("could not be laid over" in p for p in r6["problems"]), r6["problems"])

    # -- never overwritten -----------------------------------------------------
    print("versions")
    before = (scratch / "adapter" / "cent1.json").read_bytes()
    try:
        adapt.write("cent1", zero, zero, np.zeros(DIM))
        check("writing a name that exists is refused", False, "it was taken")
    except adapt.Refused as exc:
        check("writing a name that exists is refused", "never overwritten" in str(exc), exc)
    check("and the version is untouched", (scratch / "adapter" / "cent1.json").read_bytes() == before)
    for bad in ("current", "", "../x", "a b", "v1/x"):
        try:
            adapt.write(bad, zero, zero, np.zeros(DIM))
            check("a bad name is refused: " + repr(bad), False, "it was taken")
        except adapt.Refused:
            check("a bad name is refused: " + repr(bad), True)
    try:
        adapt.stamp("cent1", rank=99)
        check("stamp cannot touch the map's description", False, "it was taken")
    except adapt.Refused:
        check("stamp cannot touch the map's description", True)
    names = [v["name"] for v in adapt.versions()]
    check("versions lists every one on the disk",
          {"ident", "cent1", "other", "junk", "narrow"} <= set(names), names)
    text = io.StringIO()
    with contextlib.redirect_stdout(text):
        code = adapt._main(["adapt", "status"])
    check("the command line prints where things stand",
          code == 0 and "none -- the identity" in text.getvalue() and "cent1" in text.getvalue(),
          text.getvalue())
    with contextlib.redirect_stdout(text):
        code = adapt._main(["adapt", "use", "ghost"])
    check("and refuses in words rather than a stack trace", code == 1 and "refused" in text.getvalue())

    # -- the suggester's arm: one space, the paired bar ------------------------
    print("the suggester")
    cfg = recall.defaults()
    check("bar_now takes the version's bar when it carries one",
          recall.bar_now(cfg, cent) == (0.4, "adapter cent1"), recall.bar_now(cfg, cent))
    check("and the knob when it does not",
          recall.bar_now(cfg, ident) == (cfg["suggest_floor"], "knob"), recall.bar_now(cfg, ident))
    check("and the knob with no adapter at all",
          recall.bar_now(cfg, False) == (cfg["suggest_floor"], "knob")
          and recall.bar_now(cfg) == (cfg["suggest_floor"], "knob"))
    adapt.use("cent1")
    check("asked for the live one, it is the version's", recall.bar_now(cfg) == (0.4, "adapter cent1"))
    got = recall.gather(conn, [{"query": "what A is about", "keywords": []}], cfg)
    check("gather is stamped and says whose bar and floor it used",
          got["adapter"] == "cent1" and got["bar"] == 0.4 and got["bar_from"] == "adapter cent1"
          and got["floor"] == 0.3, {k: got[k] for k in ("adapter", "bar", "bar_from", "floor")})
    check("it counts the disagreement", got["disagreed"] == len(shadow), got["disagreed"])
    check("and never shows or holds a shadow hit",
          not any(h.get("shadow") for h in got["shown"] + got["held"])
          and {h["id"] for h in got["shown"] + got["held"]} <= {ids["A"], ids["E"]},
          [h["id"] for h in got["shown"] + got["held"]])
    check("the knobs it reads carry the version's floor", recall._knobs_for_her(cfg)["floor"] == 0.3)
    adapt.use(None)
    got = recall.gather(conn, [{"query": "what A is about", "keywords": []}], cfg)
    check("with none live, the knob, the base floor, no disagreement",
          got["adapter"] is None and got["bar_from"] == "knob" and got["disagreed"] == 0
          and got["floor"] == search.FLOOR and got["trouble"] is None)
    check("a block starts with the stamp on it", recall._block("x", cfg)["adapter"] is None)

    # -- the backup ---------------------------------------------------------------
    print("the backup")
    data = scratch / "data"
    data.mkdir()
    (data / "spark.md").write_text("a spark", encoding="utf-8")
    shutil.copytree(scratch / "adapter", data / "adapter")
    backup.DATA = data
    backup.TRACKED = scratch / "data_backups" / "assistant-backup.zip"
    check("the manifest names the adapter folder", "adapter" in backup.ALSO)
    note = backup.take(scratch / "shelf")
    with zipfile.ZipFile(note["file"]) as z:
        names = z.namelist()
    check("the zip carries the versions beside the Spark",
          "data/adapter/cent1.npz" in names and "data/adapter/cent1.json" in names
          and "data/spark.md" in names, names)
    check("and the note in the zip lists the folder", "adapter/" in note["also"], note["also"])

    conn.close()
    shutil.rmtree(scratch, ignore_errors=True)
    if FAILED:
        print(chr(10) + "  " + str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print(chr(10) + "  every rule held")


if __name__ == "__main__":
    main()
