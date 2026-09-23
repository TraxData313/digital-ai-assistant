"""Lean on the trainer without a model, over a scratch folder:

    python -m server.test_adapt_train C:\\somewhere\\scratch

A toy life in the embedder's own width. Forty essences go into a scratch
store, each read into a made-up unit vector with the shape hers have: a
shared "format" direction, carried in a different amount by each, plus a
direction of its own -- and a second shared direction, the "tongue", that
half of them carry one way and half the other, which no mean can take out
and a rank can. Queries are an essence's own part plus noise plus the
shared direction, and a tongue of their own choosing. Two essences are
sisters over one text; one text carries two co-labels; six miss queries and
six confirmations have ranks the bench works out for itself; four are hers,
two of them with a suggester. One essence is retired, one has no vector and
one a drifted vector, so the shelf being ranked is not the whole store. The
pairs file is written in the builder's own shape with `write_file`, the
embedder is stood in for by a hand, and the adapter folder and the reports
point into the scratch: nothing here loads a model, and nothing touches
`data/` -- the bench checks that last thing itself.

What is checked: identity reproduces the plain-cosine ranks, the hand-worked
ones included; centering takes the shared direction out of the shelf and
lifts the toy MRR; training lowers the loss and beats identity on held-out
toy MRR, cross-episode and same-episode; the candidates mask keeps the
label, its sisters and its co-labels out of the negatives and the reverse
mask does the same the other way; hard-negative mining never returns a
positive; the paired bootstrap gives an interval of the right sign for a
constructed gain and one across zero for pure noise; a constructed drop
out of the top seven is flagged by id and a constructed worsening is
counted; the floor rule gives back today's placement in the base space and
keeps it in proportion elsewhere; `measure` writes the versions with the
stamp, the floor and the bar, and the report, and `adapt.current()` is
still None afterwards; the same seed gives the same U and V and another
seed does not; and nothing under the repo's `data/` was written.
"""

import json
import shutil
import sys
from pathlib import Path

import numpy as np

from . import adapt, adapt_pairs as ap, adapt_train as at, db, embed, search

FAILED = []
DIM = 1024
N = 40
SEED = 20260905


def check(name, ok, detail=""):
    shown = "" if detail is None else str(detail)
    print("  " + ("ok  " if ok else "FAIL") + "  " + name
          + (("  -- " + shown) if shown and not ok else ""))
    if not ok:
        FAILED.append(name)


def unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def snapshot(folder: Path) -> dict:
    out = {}
    if folder.is_dir():
        for p in folder.rglob("*"):
            if p.is_file():
                st = p.stat()
                out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


# -- the toy -------------------------------------------------------------------

def make_life(rng):
    """The vectors: the shared direction, the tongue, forty own directions
    orthogonal to both and to each other, and the essences built from them."""
    shared = unit(rng.standard_normal(DIM))
    tongue = rng.standard_normal(DIM)
    tongue = unit(tongue - tongue.dot(shared) * shared)
    raw = rng.standard_normal((DIM, N))
    raw -= np.outer(shared, shared @ raw)
    raw -= np.outer(tongue, tongue @ raw)
    own, _ = np.linalg.qr(raw)
    own = own.T                                     # N unit rows, mutually orthogonal
    a = rng.uniform(0.80, 0.99, size=N)             # how much of the shared shape each carries
    b = np.sqrt(1 - a ** 2)
    t = np.where(np.arange(N) % 2 == 0, 0.25, -0.25)  # the tongue, half one way
    E = np.stack([unit(a[j] * shared + b[j] * own[j] + t[j] * tongue) for j in range(N)])
    return {"shared": shared, "tongue": tongue, "own": own, "a": a, "b": b, "t": t, "E": E}


def query(life, rng, j, own_weight=0.3, noise=0.04, tongue=None):
    """A query about essence j: the shared shape in full, a little of j's
    own direction, noise spread over every own direction, and a tongue."""
    n = rng.standard_normal(N) * noise
    v = 0.9 * life["shared"] + own_weight * life["own"][j] + n @ life["own"]
    s = rng.choice([-0.25, 0.25]) if tongue is None else tongue
    return unit(v + s * life["tongue"])


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) < 2:
        print("  needs a scratch folder: python -m server.test_adapt_train <path>")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    root = Path(__file__).resolve().parent.parent
    if root == scratch or root in scratch.parents:
        print("  that scratch is inside the repo. Give me one outside it.")
        sys.exit(2)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    data_dir = root / "data"
    before = snapshot(data_dir / "adapter")
    top_before = set(p.name for p in data_dir.iterdir()) if data_dir.is_dir() else set()

    db.DB_PATH = scratch / "store.db"
    adapt.ADAPTER_DIR = scratch / "adapter"
    ap.PAIRS_DIR = scratch / "pairs"
    at.REPORTS_DIR = scratch / "reports"
    at.EPOCHS, at.PATIENCE = 60, 10

    rng = np.random.default_rng(SEED)
    life = make_life(rng)
    E = life["E"]

    # -- the store: forty live essences, and three that are not ranked ------
    conn = db.connect()
    ids = []
    for j in range(N):
        text = "2026-09-05. Essence " + str(j) + " of the toy."
        rid = db.add_row(conn, "essence", text, title="toy " + str(j))
        db.put_vector(conn, rid, embed.MODEL, DIM, E[j], db.text_hash(text))
        ids.append(rid)
    old = db.add_row(conn, "essence", "an old essence, since edited", title="old")
    db.put_vector(conn, old, embed.MODEL, DIM, unit(rng.standard_normal(DIM)), db.text_hash("an old essence, since edited"))
    newer = db.add_row(conn, "essence", "the edit of the old one", replaces=[old], title="newer")
    db.put_vector(conn, newer, embed.MODEL, DIM, unit(rng.standard_normal(DIM)), db.text_hash("the edit of the old one"))
    unread = db.add_row(conn, "essence", "never read into a vector", title="unread")
    drifted = db.add_row(conn, "essence", "read, then changed", title="drifted")
    db.put_vector(conn, drifted, embed.MODEL, DIM, unit(rng.standard_normal(DIM)), db.text_hash("what it said before"))
    conn.commit()

    ids_, E_, left = at.shelf(conn)
    print("the shelf")
    check("the live essences with fresh vectors are ranked, and only those",
          ids_ == ids + [newer] and left["used"] == N + 1 and left["live"] == N + 3, (len(ids_), left))
    check("the unread and the drifted are named, the retired is not there",
          left["missing"] == [unread] and left["drifted"] == [drifted] and old not in ids_, left)
    check("the vectors come back as the store keeps them, float32-close to what went in",
          float(np.max(np.abs(E_[:N] - E))) < 1e-6)
    conn.close()

    # -- the pairs file, in the builder's shape -----------------------------------
    texts, vecs, pairs = [], [], []

    def add_text(text, vec):
        texts.append(text)
        vecs.append(np.asarray(vec, dtype=np.float32))
        return len(texts) - 1

    def pair(kind, x, label, **more):
        p = {"set": kind, "x_kind": more.pop("x_kind", "raw"), "x": x, "label": label,
             "essence": label, "turn": more.pop("turn", None), "sisters": sisters_of(label),
             "window": None, "mapped_from": None, "dt": "2026-09-05T00:00:00+00:00",
             "label_dt": "2026-09-05T00:00:00+00:00"}
        p.update(more)
        pairs.append(p)

    sisters = {ids[0]: [ids[1]], ids[1]: [ids[0]]}

    def sisters_of(label):
        return sisters.get(label, [])

    # fold: two windows per essence; the sisters share theirs (one text, two labels)
    for j in range(N):
        for w in range(2):
            if j == 1:
                continue        # essence 1's windows are essence 0's texts
            x = add_text("window " + str(w) + " of essence " + str(j), query(life, rng, j))
            pair("fold", x, ids[j], window_no=w, last=(w == 0))
            if j == 0:
                pair("fold", x, ids[1], window_no=w, last=(w == 0))
    # one hers text carrying two co-labels that are not sisters: 5 and 9
    x = add_text("a search that found both 5 and 9",
                 unit(query(life, rng, 5, own_weight=0.2) + query(life, rng, 9, own_weight=0.2)))
    pair("hers", x, ids[5], x_kind="hers", turn=100, recall_ran=True, fetched_on=100)
    pair("hers", x, ids[9], x_kind="hers", turn=100, recall_ran=True, fetched_on=100)
    # miss: six said queries, two of them hand-made with a known rank
    miss_vecs = {}
    for k, j in enumerate(range(10, 16)):
        if k == 0:
            v = unit(E[j] + 1e-3 * rng.standard_normal(DIM))        # rank 1, plainly
        elif k == 1:
            v = query(life, rng, 20, own_weight=0.4)                # about essence 20, labelled 11
        else:
            v = query(life, rng, j)
        miss_vecs[j] = v
        x = add_text("the suggester's query on turn " + str(200 + k), v)
        pair("miss", x, ids[j], x_kind="said", turn=200 + k, writer_fault=(k == 5),
             subject=0, subjects=1)
    # hers: four, two with a suggester
    for k, j in enumerate(range(30, 34)):
        x = add_text("its restatement on turn " + str(300 + k), query(life, rng, j))
        pair("hers", x, ids[j], x_kind="hers", turn=300 + k, recall_ran=(k < 2), fetched_on=300 + k)
    # sanity: six confirmations
    for k, j in enumerate(range(20, 26)):
        x = add_text("the query the suggester named " + str(j) + " on", query(life, rng, j))
        pair("sanity", x, ids[j], x_kind="said", turn=400 + k, writer_fault=False,
             subject=0, subjects=1, named_how="titles")

    doc = {
        "stamp": ap.stamp_now(), "made": "2026-09-05T00:00:00+00:00", "store": "scratch",
        "embedder": {"model": embed.MODEL, "revision": embed.REVISION},
        "knobs": {"lines": 4, "line_tokens": 200}, "window": 4, "windows_max": 3,
        "shelf": {"essences": N + 4, "live": sorted(ids + [newer, unread, drifted]), "retired": 1},
        "labels": {str(i): {"title": "toy " + str(k), "dt": "2026-09-05"} for k, i in enumerate(ids)},
        "sisters": {str(k): v for k, v in sisters.items()}, "lineage": {}, "ambiguous": {},
        "dropped": [], "texts": texts, "pairs": pairs, "said": None,
        "x_index": ap.x_index_of(texts, pairs), "vectors": None, "counts": {},
    }
    where = ap.write_file(doc, scratch / "pairs", np.stack(vecs))
    check("the pairs file and its vectors are in the scratch",
          where.is_file() and where.with_suffix(".npz").is_file(), where)

    # -- the data, loaded -----------------------------------------------------------
    print("the data")
    conn, _ = ap.open_store(scratch / "store.db")
    data = at.Data.load(conn, where, say=lambda *a: None)
    row = {e: k for k, e in enumerate(data.ids)}
    check("every pair points at a ranked essence; none dropped",
          len(data.pairs) == len(pairs) and data.dropped == [], data.dropped)
    xs = {t: i for i, t in enumerate(texts)}
    x_sis = xs["window 0 of essence 0"]
    x_co = xs["a search that found both 5 and 9"]
    x_plain = xs["window 0 of essence 7"]
    check("positives: the sisters' text has both, the co-labelled text has both, a plain text has one",
          data.pos[x_sis] == {row[ids[0]], row[ids[1]]} and data.pos[x_co] == {row[ids[5]], row[ids[9]]}
          and data.pos[x_plain] == {row[ids[7]]}, (data.pos[x_sis], data.pos[x_co], data.pos[x_plain]))
    check("positives_of follows sisters from a lone label",
          at.positives_of([ids[1]], data.sisters, data.index) == {row[ids[0]], row[ids[1]]})
    cases = data.cases(data.of("fold"))
    check("the sisters' two pairs on one text are one case with both as right answers",
          sum(1 for c in cases if c["x"] == x_sis) == 1
          and next(c for c in cases if c["x"] == x_sis)["labels"] == sorted([ids[0], ids[1]])
          and len(cases) == 2 * (N - 1), len(cases))
    hers_cases = data.cases(data.of("hers"))
    check("the co-labelled search is one case of two pairs", any(len(c["pairs"]) == 2 for c in hers_cases)
          and len(hers_cases) == 5)
    fam = data.families(data.of("fold"))
    check("sisters are one family, the rest their own",
          fam[ids[0]] == fam[ids[1]] and len(set(fam.values())) == N - 1, len(set(fam.values())))
    folds = at.grouped_folds(data.of("fold"), fam, seed=SEED)
    check("five folds, every family in one, the sisters together",
          set(folds.values()) == set(range(5)) and folds[fam[ids[0]]] == folds[fam[ids[1]]])

    # -- the masks, directly ------------------------------------------------------
    print("the masks")
    hard = at.hard_negatives(data.base, data.P)
    check("hard negatives: five per query, never a positive",
          hard.shape == (len(texts), 5) and not any(data.P[i, hard[i]].any() for i in range(len(texts))))
    check("and they are the base space's nearest non-positives",
          all(set(hard[i]) == set(np.argsort(-np.where(data.P[i], -np.inf, data.base[i]))[:5])
              for i in range(len(texts))))
    batch_x = np.array([x_sis, x_co, x_plain, xs["window 1 of essence 9"]])
    batch_e = np.array([row[ids[0]], row[ids[5]], row[ids[7]], row[ids[9]]])
    allowed = at.candidates_mask(data.P[batch_x], batch_e, hard[batch_x])
    check("the candidates mask keeps the query's label in and its sister out",
          allowed[0, row[ids[0]]] and not allowed[0, row[ids[1]]])
    check("keeps a co-label out: 9 is in the batch and is not a negative for the search that found 5 and 9",
          allowed[1, row[ids[5]]] and not allowed[1, row[ids[9]]])
    check("the batch's other labels and the hard negatives are in, nothing else",
          allowed[2, row[ids[0]]] and allowed[2, row[ids[5]]] and allowed[2, row[ids[9]]]
          and all(allowed[2, h] for h in hard[x_plain])
          and allowed[2].sum() == len({row[ids[0]], row[ids[5]], row[ids[9]], row[ids[7]]} | set(hard[x_plain])))
    check("a hard negative that is a positive elsewhere in the batch is still out for that query",
          not any(allowed[i, j] for i in range(4) for j in range(data.P.shape[1])
                  if data.P[batch_x[i], j] and j != batch_e[i]))
    rev = at.reverse_mask(data.P[batch_x], batch_e)
    check("the reverse mask keeps each pair's own query and takes out the queries its essence is a positive of",
          rev.diagonal().all() and not rev[3, 1] and rev[1, 3] and rev[0, 2] and rev[2, 0], rev.astype(int))

    # -- identity: the plain cosine ------------------------------------------------
    print("identity")
    ident = at.identity_map(DIM)
    sc_id = at.Scorer(ident, data.E)
    miss_cases = data.cases(data.of("miss"))
    r_id = at.case_ranks(sc_id, data, miss_cases)
    plain = data.X @ data.E.T
    r_plain = at.ranks_of(plain[[c["x"] for c in miss_cases]], [c["pos"] for c in miss_cases])
    check("identity reproduces the plain-cosine ranks", np.array_equal(r_id, r_plain), (r_id, r_plain))
    by_turn = {c["turns"][0]: r for c, r in zip(miss_cases, r_id)}
    own_rank = 1 + int((data.E @ miss_vecs[11] > data.E[row[ids[11]]] @ miss_vecs[11]).sum())
    check("the hand-made ranks: the copy is rank 1, the query about 20 ranks 11 where the cosine says",
          by_turn[200] == 1 and by_turn[201] == own_rank and own_rank > 1, (by_turn, own_rank))
    all_cases = data.cases(data.pairs)
    mrr_id = at.metrics(at.case_ranks(sc_id, data, all_cases))["mrr"]
    check("identity is fooled by the shared shape: toy MRR under one", 0.3 < mrr_id < 0.95, mrr_id)

    # -- centering ----------------------------------------------------------------------
    print("centering")
    cent = at.centering_map(data.E)
    fe = cent.apply_many(data.E)
    check("centering takes the shared direction out of the shelf",
          abs(float((fe @ life["shared"]).mean())) < 0.05 < abs(float((data.E @ life["shared"]).mean())),
          (float((fe @ life["shared"]).mean()), float((data.E @ life["shared"]).mean())))
    mrr_c = at.metrics(at.case_ranks(at.Scorer(cent, data.E), data, all_cases))["mrr"]
    check("and lifts the toy MRR over identity", mrr_c > mrr_id, (mrr_id, mrr_c))

    # -- training ------------------------------------------------------------------------
    print("training")
    got = at.fit(data, 4, 1e-2, seed=SEED, say=lambda *a: None)
    check("training lowers the loss", got["loss_last"] < got["loss_first"], (got["loss_first"], got["loss_last"]))
    held = data.cases([p for p in data.of("fold") if folds[fam[p["label"]]] == 0])
    slice_id = at.metrics(at.case_ranks(sc_id, data, held))["mrr"]
    check("and beats identity on the early-stopping slice", got["held_out_mrr"] > slice_id,
          (slice_id, got["held_out_mrr"]))
    tr = at.trained_map(got["U"], got["V"], got["mean"], "toy")
    cross = data.cases(data.of(("miss", "hers")))
    mrr_tr = at.metrics(at.case_ranks(at.Scorer(tr, data.E), data, cross))["mrr"]
    mrr_id_cross = at.metrics(at.case_ranks(sc_id, data, cross))["mrr"]
    mrr_c_cross = at.metrics(at.case_ranks(at.Scorer(cent, data.E), data, cross))["mrr"]
    check("and beats identity on the held-out cross-episode toy queries", mrr_tr > mrr_id_cross,
          (mrr_id_cross, mrr_c_cross, mrr_tr))
    check("the trained map is not the identity and is the plan's shape",
          not tr.is_identity and tr.rank == 4 and tr.dim == DIM and got["norm_uv"] > 0)
    se_id = at.same_episode(data, mapp=ident, seed=SEED)
    se_tr = at.same_episode(data, rank=4, lam=1e-2, seed=SEED)
    check("same-episode folds: five of them, a trained map above identity",
          len(se_tr["folds"]) == 5 and len(se_tr["fits"]) == 5 and se_tr["mrr"]["mean"] > se_id["mrr"]["mean"],
          (se_id["mrr"], se_tr["mrr"]))
    again = at.fit(data, 4, 1e-2, seed=SEED, say=lambda *a: None)
    check("the same seed gives the same U and V",
          np.array_equal(got["U"], again["U"]) and np.array_equal(got["V"], again["V"]))
    other = at.fit(data, 4, 1e-2, seed=SEED + 1, say=lambda *a: None)
    check("another seed does not", not np.array_equal(got["V"], other["V"]))
    weighted = at.fit(data, 4, 1e-2, seed=SEED, sets=("fold", "miss", "hers"),
                      upweight={"miss": 3.0, "hers": 3.0}, say=lambda *a: None)
    check("the shipped-shape fit takes the miss and hers pairs, weighted",
          weighted["trained_pairs"] == got["trained_pairs"] + 6 + 6
          and weighted["weighted"] == {"miss": 3.0, "hers": 3.0})

    # -- paired: the bootstrap, the regression, the sanity count ---------------------
    print("paired")
    b = at.bootstrap(np.ones(20), resamples=2000, seed=1)
    check("a constant gain: the interval is above zero", b["excludes_zero"] and b["lo"] > 0 and b["mean"] == 1.0, b)
    b = at.bootstrap(np.random.default_rng(3).standard_normal(60), resamples=2000, seed=1)
    check("pure noise: the interval spans zero", not b["excludes_zero"] and b["lo"] < 0 < b["hi"], b)
    b = at.bootstrap([1, 1, 1, 1, 1, 1, 1, 0.5, 1, -0.2], resamples=2000, seed=1)
    check("a mostly-positive set: the right sign", b["excludes_zero"] and b["mean"] > 0 and b["lo"] > 0, b)
    b = at.bootstrap([], resamples=10)
    check("no differences: nothing claimed", b["excludes_zero"] is None and b["mean"] is None)
    # Four cases: one drops out of the top seven (3 -> 8), one climbs within
    # it (5 -> 2), one comes into it (9 -> 6), one stands still.
    toy_cases = [{"id": "c" + str(i), "pairs": [i]} for i in range(4)]
    pr = at.paired(toy_cases, [3, 5, 9, 7], [8, 2, 6, 7], resamples=500)
    check("a constructed drop out of the top seven is flagged by id, and only it; the one that came in is named too",
          pr["regressions"] == ["c0"] and pr["entered"] == ["c2"], (pr["regressions"], pr["entered"]))
    check("the sanity counts: one worse, two better, one unchanged, the mean rank change",
          pr["worsened"] == 1 and pr["improved"] == 2 and pr["unchanged"] == 1
          and abs(pr["mean_rank_change"] - (-0.25)) < 1e-9 and pr["n"] == 4, pr)
    check("recall and MRR for both maps ride beside",
          pr["a"]["recall7"] == 0.75 and pr["b"]["recall7"] == 0.75
          and abs(pr["d_mrr"]["mean"] - ((1 / 8 - 1 / 3) + (1 / 2 - 1 / 5) + (1 / 6 - 1 / 9)) / 4) < 1e-9,
          (pr["a"], pr["b"], pr["d_mrr"]))
    worse = at.paired(toy_cases, [1, 2, 3, 4], [2, 3, 4, 5], resamples=500)
    check("a constructed worsening of every rank is counted whole and the mean says so",
          worse["worsened"] == 4 and worse["improved"] == 0 and worse["regressions"] == []
          and worse["mean_rank_change"] == 1.0 and worse["d_mrr"]["hi"] < 0, worse)
    v = at.verdict("toy", pr, pr, None, None)
    check("the verdict names the regression and the sanity failure, and the floor as not measured",
          v["go"] is False and "no_regression" in v["unmet"] and "sanity" in v["unmet"]
          and v["unknown"] == ["floor"], v)

    # -- the floor rule ----------------------------------------------------------------
    print("the floor")
    r = at.floor_rule(0.481, 0.678)
    check("in the base space the rule gives back 0.45 and 0.55", r["floor"] == 0.45 and r["bar"] == 0.55, r)
    r = at.floor_rule(0.2, 0.594)
    check("with the real span doubled, the floor sits twice as far under and the bar twice as far over",
          abs(r["floor"] - 0.138) < 1e-9 and abs(r["bar"] - 0.338) < 1e-9 and r["scale"] == 2.0, r)
    r = at.floor_rule(0.3, 0.3)
    check("a span of nothing puts the floor on the worst real probe", r["floor"] == 0.3 and r["bar"] == 0.3, r)

    # -- the whole measure, with a hand for the embedder --------------------------------
    print("measure")
    shared, tongue = life["shared"], life["tongue"]

    def fake_read(texts_):
        out = []
        for t in texts_:
            seed = int(db.text_hash(t)[:8], 16)
            g = np.random.default_rng(seed)
            v = unit(0.85 * shared + 0.3 * g.standard_normal(N) @ life["own"] + 0.2 * tongue * g.choice([-1, 1]))
            out.append({"vec": [float(x) for x in v], "dim": DIM, "n_tokens": 3,
                        "truncated": False, "hash": db.text_hash(t)})
        return out
    embed.read = fake_read
    current_file = scratch / "adapter" / adapt.CURRENT
    R = at.measure(pairs_path=where, store=scratch / "store.db", seed=SEED, quick=False,
                   say=lambda *a: None, prefix="toy", out_dir=scratch / "reports",
                   ranks=(4,), lambdas=(1e-2, 1e-1))
    check("the run wrote the report and the json in the scratch",
          Path(R["report"]).is_file() and Path(R["json"]).is_file()
          and Path(R["report"]).parent == scratch / "reports", R.get("report"))
    names = [v["name"] for v in adapt.versions()]
    check("the candidates are there: center, r4, r4-all, and nothing else",
          sorted(names) == ["toy-center", "toy-r4", "toy-r4-all"], names)
    for name in names:
        meta = adapt.describe(name)
        check(name + " carries the pairs stamp, a floor, a bar, the embedder, and the key numbers",
              meta["trained_on"] == doc["stamp"] and meta["floor"] is not None and meta["bar"] is not None
              and meta["model"] == embed.MODEL and meta["revision"] == embed.REVISION
              and "cross_recall7" in meta["numbers"] and "sanity_worsened" in meta["numbers"]
              and meta["numbers"]["report"] == R["stamp"] and "CANDIDATE" in meta["note"], meta)
        loaded = adapt.load(name)
        check(name + " loads and applies", loaded.apply_many(data.E).shape == data.E.shape)
    check("the stamped floor and bar are the report's proposal",
          adapt.describe("toy-center")["floor"] == R["floor"]["centering"]["proposed"]["floor"]
          and adapt.describe("toy-r4")["bar"] == R["floor"]["r4"]["proposed"]["bar"])
    check("nothing went live: current is None and there is no current file",
          adapt.current() is None and not current_file.exists() and adapt.named() is None)
    check("every table is in the report",
          all(k in R for k in ("same_episode", "cross", "cross_cases", "sanity", "floor", "verdict", "shipped"))
          and set(R["floor"]) == {"identity", "centering", "r4"} and set(R["verdict"]) == {"centering", "r4"}
          and "r4-all" in R["shipped"] and R["shipped"]["r4-all"]["in_sample"] is True)
    check("the identity's floor is measured in the base space and its rule gives today's numbers when today's span is fed",
          R["floor"]["identity"]["adapter"] is None and len(R["floor"]["identity"]["probes"]) == len(search.PROBES))
    check("the cross-episode cuts are all there, with and without the writer's fault",
          set(R["cross"]) >= {"miss said", "miss raw", "hers", "hers with a suggester", "all cross-episode",
                              at.DECISIVE}
          and R["cross"]["all cross-episode"]["identity"]["n"] == 11
          and R["cross"][at.DECISIVE]["identity"]["n"] == 10
          and R["cross"]["hers with a suggester"]["identity"]["n"] == 3
          and R["cross"]["miss said"]["identity"]["n"] == 6, {k: v["identity"]["n"] for k, v in R["cross"].items()})
    check("the sanity set is the six confirmations", R["sanity"]["identity"]["n"] == 6)
    check("lambda was chosen by the same-episode folds and written on the map",
          R["lambda_choice"]["r4"]["chosen"] in (1e-2, 1e-1) and R["maps"]["r4"]["lambda"] == R["lambda_choice"]["r4"]["chosen"])
    md = Path(R["report"]).read_text(encoding="utf-8")
    check("the report says what decides, what is diagnostic, and that nothing went live",
          "diagnostic" in md and at.DECISIVE in md and "Nothing here went live" in md and "in-sample" in md.lower())
    js = json.loads(Path(R["json"]).read_text(encoding="utf-8"))
    check("the json round-trips with the numbers", js["stamp"] == R["stamp"] and js["cross"][at.DECISIVE]["r4"]["n"] == 10)
    try:
        at.measure(pairs_path=where, store=scratch / "store.db", seed=SEED, quick=True,
                   say=lambda *a: None, prefix="toy", out_dir=scratch / "reports", ranks=(4,), lambdas=(1e-2,))
        check("a quick run writes no version", sorted(v["name"] for v in adapt.versions()) == sorted(names))
    except Exception as exc:
        check("a quick run writes no version", False, repr(exc))
    conn.close()

    # -- nothing under data/ ---------------------------------------------------------------
    print("the repo")
    after = snapshot(data_dir / "adapter")
    top_after = set(p.name for p in data_dir.iterdir()) if data_dir.is_dir() else set()
    check("nothing under data/adapter changed", before == after,
          sorted(set(before.items()) ^ set(after.items()))[:5])
    check("no new entry under data/", top_after <= top_before | {"adapter"}, sorted(top_after - top_before))

    shutil.rmtree(scratch, ignore_errors=True)
    if FAILED:
        print(chr(10) + "  " + str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print(chr(10) + "  every rule held")


if __name__ == "__main__":
    main()
