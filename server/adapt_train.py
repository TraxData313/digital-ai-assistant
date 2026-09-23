"""The adapter, trained and measured: step three of the plan, before anything goes live.

`adapt.py` is where a version lives and how it falls back; `adapt_pairs.py`
gathers what it learns from. This file makes the maps, puts the numbers
beside each other -- identity, centering, rank 16, rank 32 -- and writes the
candidates and a report for the assistant to rule on. Nothing here goes
live: `adapt.use` is never called, `data/adapter/current` is never written,
and the store is opened so that nothing in here can write to it.

**The maps.** identity is today: U, V and c all zero. centering is the plan's
step zero: c the mean of the live shelf's vectors, nothing trained. rank 16
and rank 32 start from centering and train U and V: symmetric InfoNCE at
temperature 0.05 (the temperature BGE was trained with), query to essence
and essence to query, plus lambda times the squared Frobenius norm of U V^T,
the pull toward the identity; Adam at 1e-3, at most two hundred epochs in
mini-batches of sixty-four, stopped early on a held-out slice's MRR; U from
zero, V small and random from a fixed seed. E is frozen and every vector is
already on the shelf or in the pairs file, so a training is seconds.

**Positives and negatives**. For a query text the positives are
every label the pairs file attaches to it (`x_index`: the co-labels) and
every sister of each of those -- essences over the same trail rows -- and
none of them is ever a negative for it. The negatives are the other labels
in the batch and five hard ones mined once in the base space: the live
essences the plain cosine ranks highest against the text that are not its
positives. A hit at measurement counts on any positive.

**What trains what.** The fold pairs -- raw windows and X-said -- train the
decision maps. The miss pairs, hers and the sanity pairs are held out of them
entirely. The same-episode folds choose lambda and nothing else (the fold
pairs are part copy-detection, so that table is diagnostic and never a
reason to ship). The cross-episode pairs decide, paired: each query ranked
under identity and under the map, the difference per
query, a bootstrap over the differences, the paired MRR beside recall at
seven, and no query that was in the top seven under identity may fall out
of it. Then the shipped-shape maps are refit over fold + miss + hers, miss
and hers weighted three times, and measured again -- in-sample now, and
said so -- so the report shows that they do not regress and where their
floors land.

**The unit is a query.** The same text with two labels -- a search that
found two essences, the window two sisters share -- is one observation with
two right answers, not two; pair counts are given beside the case counts.

**The floor.** The seventeen probes of `search.py` under each map, and a
floor and a bar proposed by one rule: when the base floor was measured it
sat 0.031 under the worst real probe and the bar 0.10 above the floor, and
the real probes spanned 0.197; in every space, the identity's today included, those
two distances are kept in proportion to that space's span. The gap between
the worst real probe and the best noise probe is given in both units, and
the verdict reads it in units of the span, since a map that stretches every
score stretches the gap with it.

**An aside**, computed every run and never a candidate: two weaker means --
half the shelf's, and the fold queries' own -- scored beside the plan's
centering, because the queries do not carry the whole of the shelf's mean
and the tables should show where a loss comes from.

    python -m server.adapt_train measure [--pairs FILE] [--seed N] [--quick] [--out DIR] [--prefix NAME]
    python -m server.adapt_train train --rank R [--pairs FILE] [--name NAME] [--all] [--lambda L] [--seed N]

`measure` is the whole run; `--quick` skips the probe pass and writes no
version, for iterating. `train` fits one map and keeps it as a version, with
no floor of its own until `python -m server.search floor <name>` measures one.
"""

import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import adapt, adapt_pairs, db, embed, search
from . import home

ROOT = Path(__file__).resolve().parent.parent
# A variable rather than a constant so a bench can point it at a scratch.
REPORTS_DIR = home.DATA / "adapter" / "reports"

# The training, as the plan writes it.
TEMPERATURE = 0.05
LAMBDAS = (1e-3, 1e-2, 1e-1)
LR = 1e-3
EPOCHS = 200
PATIENCE = 20          # epochs without a better held-out MRR before stopping
BATCH = 64
HARD = 5               # hard negatives per query, mined once in the base space
V_INIT = 0.01          # the spread of V's random start; U starts at zero
FOLDS = 5
RANKS = (16, 32)
SEED = 20260904
UPWEIGHT = 3.0         # miss and hers, in the shipped-shape refit

# The measurement.
RESAMPLES = 10_000
TOP = 7                # the suggester's own upper bound on titles
TOP_SMALL = 3          # and its lower one

# Today's placement of the floor and the bar, in the base space, as measured
# in `search.py`: the real probes 0.481 to 0.678, floor 0.45, bar 0.55.
BASE_REAL = (0.481, 0.678)
BASE_FLOOR = 0.45
BASE_BAR = 0.55

DECISION_SETS = ("fold",)
SHIPPED_SETS = ("fold", "miss", "hers")


class Refused(ValueError):
    pass


def _rel(p) -> str:
    try:
        return str(Path(p).resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def _f(x, places=3) -> str:
    """A number as it is read: three places, or a dash for none."""
    if x is None:
        return "--"
    return format(float(x), "." + str(places) + "f")


def _plain(o):
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def _safe(say):
    """A `say` that survives a console which cannot write a ± or a Δ: the
    line goes out with those replaced rather than the run dying on it."""
    say = say or (lambda *a: None)

    def out(line=""):
        try:
            say(line)
        except UnicodeEncodeError:
            say(str(line).encode("ascii", "replace").decode("ascii"))
    return out


# -- the shelf and the pairs, as arrays ---------------------------------------

def shelf(conn) -> tuple:
    """The live shelf's vectors: every essence a search runs over, read by
    the loaded embedder and still saying what it said when it was read.
    An essence whose vector is missing or drifted is left out and named, as
    the search leaves it out. Returns (ids, E, left_out)."""
    rows = db.essence_shelf(conn, include_retired=False)
    vectors = db.vectors_for(conn, embed.MODEL)
    ids, vecs, missing, drifted = [], [], [], []
    for r in rows:
        got = vectors.get(r["id"])
        if got is None:
            missing.append(r["id"])
        elif got["text_hash"] != db.text_hash(r["text"]):
            drifted.append(r["id"])
        else:
            ids.append(r["id"])
            vecs.append(got["vec"])
    if not ids:
        raise Refused("the shelf has no vectors by " + embed.MODEL + " to rank over")
    E = np.asarray(vecs, dtype=np.float64)
    return ids, E, {"live": len(rows), "used": len(ids),
                    "missing": missing, "drifted": drifted}


def latest_pairs() -> Path:
    """The newest pairs file that carries vectors."""
    d = Path(adapt_pairs.PAIRS_DIR)
    for p in sorted(d.glob("*.json"), reverse=True):
        try:
            doc = adapt_pairs.load(p)
        except adapt_pairs.Refused:
            continue
        vecs, _ = adapt_pairs.vectors_of(p, doc)
        if vecs is not None:
            return p
    raise Refused("no pairs file with vectors under " + _rel(d)
                  + "; python -m server.adapt_pairs build --said --embed makes one")


def positives_of(labels, sisters: dict, index: dict) -> set:
    """The positives rule, as a set of shelf rows: every label the text carries and
    every sister of each -- and only those on the shelf being ranked."""
    out = set()
    for label in labels:
        for e in [int(label)] + [int(s) for s in sisters.get(int(label), ())]:
            if e in index:
                out.add(index[e])
    return out


def positive_matrix(pos_sets, n: int) -> np.ndarray:
    """One row per query, True where an essence may never be its negative."""
    P = np.zeros((len(pos_sets), n), dtype=bool)
    for i, pos in enumerate(pos_sets):
        for j in pos:
            P[i, j] = True
    return P


def hard_negatives(base_scores: np.ndarray, P: np.ndarray, k=None) -> np.ndarray:
    """For every query, the `k` essences the base space ranks highest that
    are not positives: the look-alikes the retriever confuses today. Mined
    once, before training, and never a positive by construction -- and the
    training's own mask takes any positive out again regardless."""
    k = HARD if k is None else int(k)
    s = np.array(base_scores, dtype=np.float64)
    s[P] = -np.inf
    order = np.argsort(-s, axis=1, kind="stable")[:, :k]
    return order


class Data:
    """The pairs file and the shelf, as the trainer and the tables read
    them: the essence vectors E (one row per live essence with a fresh
    vector), the query vectors X (one per distinct text), the positives of
    every text, the hard negatives, and the pairs pointing into both."""

    def __init__(self, doc, X, ids, E, left_out, path):
        self.doc, self.path = doc, Path(path)
        self.stamp = doc["stamp"]
        self.ids = list(ids)
        self.index = {e: k for k, e in enumerate(self.ids)}
        self.E = np.asarray(E, dtype=np.float64)
        self.X = np.asarray(X, dtype=np.float64)
        self.left_out = left_out
        self.dim = self.E.shape[1]
        if self.X.shape[1] != self.dim:
            raise Refused("the pairs' vectors are " + str(self.X.shape[1])
                          + " wide and the shelf's are " + str(self.dim))
        self.sisters = {int(k): [int(v) for v in vs]
                        for k, vs in (doc.get("sisters") or {}).items()}
        self.labels_of = [sorted(int(l) for l in (x.get("labels") or []))
                          for x in doc["x_index"]]
        self.pos = [positives_of(ls, self.sisters, self.index) for ls in self.labels_of]
        self.P = positive_matrix(self.pos, len(self.ids))
        self.base = self.X @ self.E.T
        self.hard = hard_negatives(self.base, self.P)
        self.pairs, self.dropped = [], []
        for i, p in enumerate(doc["pairs"]):
            label = int(p["label"])
            if label not in self.index:
                self.dropped.append({"pair": i, "set": p["set"], "label": label})
                continue
            self.pairs.append({
                "i": i, "set": p["set"], "x_kind": p["x_kind"], "x": int(p["x"]),
                "label": label, "e": self.index[label], "turn": p.get("turn"),
                "writer_fault": bool(p.get("writer_fault")),
                "recall_ran": p.get("recall_ran"),
            })
        self.titles = {int(k): (v or {}).get("title") for k, v in (doc.get("labels") or {}).items()}

    @classmethod
    def load(cls, conn, path, say=None):
        say = _safe(say)
        path = Path(path)
        doc = adapt_pairs.load(path)
        emb = doc.get("embedder") or {}
        if (emb.get("model") or "") != (embed.MODEL or "") or \
                (emb.get("revision") or "") != (embed.REVISION or ""):
            raise Refused("the pairs file was made over " + str(emb.get("model"))
                          + " at " + str(emb.get("revision") or "")[:12]
                          + " and the embedder loaded is " + embed.MODEL + " at "
                          + (embed.REVISION or "")[:12] + "; a map trained over "
                          "one would never be applied under the other")
        X, note = adapt_pairs.vectors_of(path, doc)
        if X is None:
            raise Refused(_rel(path) + " has no vectors beside it (" + note + ")")
        ids, E, left_out = shelf(conn)
        data = cls(doc, X, ids, E, left_out, path)
        say("pairs      " + _rel(path) + " -- " + note)
        say("shelf      " + str(left_out["used"]) + " of " + str(left_out["live"])
            + " live essences with a fresh vector"
            + (", missing " + str(left_out["missing"]) if left_out["missing"] else "")
            + (", drifted " + str(left_out["drifted"]) if left_out["drifted"] else ""))
        moved = set(int(i) for i in (doc.get("shelf") or {}).get("live") or []) ^ set(ids)
        if moved:
            say("shelf      NOTE: the shelf has moved since the pairs were made; "
                + str(len(moved)) + " essences differ: " + str(sorted(moved)[:20]))
        if data.dropped:
            say("pairs      " + str(len(data.dropped)) + " pairs dropped: their label "
                "is not on the shelf being ranked")
        return data

    def text(self, x: int, n: int = 70) -> str:
        t = " ".join(str(self.doc["texts"][x]).split())
        return t if len(t) <= n else t[:n - 1] + "…"

    def of(self, sets) -> list:
        sets = (sets,) if isinstance(sets, str) else tuple(sets)
        return [p for p in self.pairs if p["set"] in sets]

    def cases(self, pairs) -> list:
        """The evaluation unit: one distinct text with every positive it has.
        Two pairs on one text are one case; the pair indices ride on it."""
        by_x = {}
        for p in pairs:
            c = by_x.get(p["x"])
            if c is None:
                c = by_x[p["x"]] = {
                    "x": p["x"], "pos": sorted(self.pos[p["x"]]),
                    "labels": [], "pairs": [], "set": p["set"], "x_kind": p["x_kind"],
                    "turns": set(), "writer_fault": False, "recall_ran": False}
            c["pairs"].append(p["i"])
            if p["label"] not in c["labels"]:
                c["labels"].append(p["label"])
            if p["turn"] is not None:
                c["turns"].add(int(p["turn"]))
            c["writer_fault"] = c["writer_fault"] or p["writer_fault"]
            c["recall_ran"] = c["recall_ran"] or bool(p["recall_ran"])
        out = []
        for c in by_x.values():
            c["labels"].sort()
            c["turns"] = sorted(c["turns"])
            c["text"] = self.text(c["x"])
            # Unique, and readable: two searches on one turn for one
            # essence differ only in their words, so the words are in it.
            c["id"] = (c["set"] + " " + c["x_kind"]
                       + (" turn " + "/".join(str(t) for t in c["turns"]) if c["turns"] else "")
                       + " " + "+".join("#" + str(l) for l in c["labels"])
                       + ' "' + self.text(c["x"], 36) + '"')
            out.append(c)
        out.sort(key=lambda c: (c["turns"] or [0], c["set"], c["x_kind"], c["x"]))
        return out

    def families(self, pairs) -> dict:
        """Labels tied together for a split: sisters, and labels sharing a
        text. No essence is on both sides of a fold, and neither is its
        sister nor the text it shares with another."""
        parent = {}

        def find(a):
            while parent.setdefault(a, a) != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        by_x = defaultdict(set)
        for p in pairs:
            find(p["label"])
            for s in self.sisters.get(p["label"], ()):
                if s in self.index:
                    union(p["label"], s)
            by_x[p["x"]].add(p["label"])
        for labels in by_x.values():
            labels = sorted(labels)
            for l in labels[1:]:
                union(labels[0], l)
        return {l: find(l) for l in list(parent)}


def grouped_folds(pairs, families: dict, k: int = FOLDS, seed: int = SEED) -> dict:
    """Deal families to `k` folds, the lightest fold taking the next family,
    in an order fixed by the seed. Returns family -> fold."""
    rng = np.random.default_rng(seed)
    weight = Counter(families[p["label"]] for p in pairs)
    order = sorted(weight)
    rng.shuffle(order)
    load = [0] * k
    out = {}
    for fam in order:
        f = min(range(k), key=lambda j: (load[j], j))
        out[fam] = f
        load[f] += weight[fam]
    return out


# -- maps and ranks ------------------------------------------------------------

def identity_map(dim: int, name: str = "identity") -> adapt.Adapter:
    empty = np.zeros((dim, 0), dtype=np.float64)
    return adapt.Adapter(name, empty, empty, np.zeros(dim), {"model": embed.MODEL,
                                                             "revision": embed.REVISION})


def centering_map(E: np.ndarray, name: str = "centering") -> adapt.Adapter:
    empty = np.zeros((E.shape[1], 0), dtype=np.float64)
    return adapt.Adapter(name, empty, empty, E.mean(axis=0), {"model": embed.MODEL,
                                                              "revision": embed.REVISION})


def trained_map(U, V, mean, name: str) -> adapt.Adapter:
    return adapt.Adapter(name, U, V, mean, {"model": embed.MODEL, "revision": embed.REVISION})


class Scorer:
    """One map laid over the shelf once; then any set of queries scored
    against it. A dot between two unit rows is their cosine."""

    def __init__(self, mapp: adapt.Adapter, E: np.ndarray):
        self.map = mapp
        self.fe = mapp.apply_many(E)

    def scores(self, X: np.ndarray) -> np.ndarray:
        if len(X) == 0:
            return np.zeros((0, self.fe.shape[0]))
        return self.map.apply_many(X) @ self.fe.T


def ranks_of(S: np.ndarray, pos_sets) -> np.ndarray:
    """The rank of the best positive in each row: one plus the essences
    scoring strictly higher. A hit on any positive counts."""
    out = np.empty(len(pos_sets), dtype=np.int64)
    for i, pos in enumerate(pos_sets):
        pos = list(pos)
        if not pos:
            out[i] = S.shape[1] + 1
            continue
        best = S[i, pos].max()
        out[i] = 1 + int((S[i] > best).sum())
    return out


def case_ranks(scorer: Scorer, data: Data, cases) -> np.ndarray:
    if not cases:
        return np.zeros(0, dtype=np.int64)
    S = scorer.scores(data.X[[c["x"] for c in cases]])
    return ranks_of(S, [c["pos"] for c in cases])


def metrics(r) -> dict:
    r = np.asarray(r, dtype=np.float64)
    if r.size == 0:
        return {"n": 0, "recall3": None, "recall7": None, "mrr": None}
    return {"n": int(r.size),
            "recall3": float((r <= TOP_SMALL).mean()),
            "recall7": float((r <= TOP).mean()),
            "mrr": float((1.0 / r).mean())}


# -- training ------------------------------------------------------------------

def candidates_mask(P_batch: np.ndarray, labels, hard_batch: np.ndarray) -> np.ndarray:
    """Query to essence, one batch: which essences may stand in the
    denominator against each query. The batch's labels and the query's own
    hard negatives are candidates; its positives -- label, sisters,
    co-labels -- are taken out, and its own label put back as the one it is
    scored for. The positives rule is this function."""
    P_batch = np.asarray(P_batch, dtype=bool)
    labels = np.asarray(labels, dtype=np.int64)
    k = P_batch.shape[0]
    cand = np.zeros_like(P_batch)
    cand[:, labels] = True
    for i in range(k):
        cand[i, np.asarray(hard_batch[i], dtype=np.int64)] = True
    allowed = cand & ~P_batch
    allowed[np.arange(k), labels] = True
    return allowed


def reverse_mask(P_batch: np.ndarray, labels) -> np.ndarray:
    """Essence to query, one batch: for the pair i with label e_i, which of
    the batch's queries may stand in the denominator. Every query e_i is a
    positive of -- a sister's window, a co-labelled search, another window
    of the same essence -- is taken out, and the pair's own query kept."""
    P_batch = np.asarray(P_batch, dtype=bool)
    labels = np.asarray(labels, dtype=np.int64)
    k = P_batch.shape[0]
    allowed = ~(P_batch[:, labels].T)         # [i, j]: e_i is not a positive of X_j
    allowed[np.arange(k), np.arange(k)] = True
    return allowed


def train(E, X, pairs, P, hard, rank: int, lam: float, seed: int = SEED,
          val=None, weights=None, epochs=None, patience=None, batch=None, lr=None,
          say=None) -> dict:
    """Fit U and V of one map, c fixed at the mean of E.

    `pairs` are (query row, essence row); `P` is the positives matrix over
    every query row; `hard` the mined negatives per query row; `val` is
    (query rows, positive sets) ranked over all of E after every epoch, and
    the epoch with the best MRR is the one kept -- stopping after `patience`
    epochs without a better one. `weights` multiplies each pair's loss.

    The loss, per pair, both ways: the query against every candidate essence
    -- the batch's labels, its own hard negatives, its own label -- with its
    other positives taken out of the denominator; and the essence against
    the batch's queries, with the queries it is a positive of taken out.
    Mean of the two, weighted, plus lambda times ||U V^T||_F^2, which is
    trace(U^T U V^T V) and costs nothing."""
    import torch
    import torch.nn.functional as F
    say = _safe(say)
    # The knobs are read when called, not when defined, so a bench can turn
    # them down for a toy and the command line still gets the plan's.
    epochs = EPOCHS if epochs is None else int(epochs)
    patience = PATIENCE if patience is None else int(patience)
    batch = BATCH if batch is None else int(batch)
    lr = LR if lr is None else float(lr)
    if val is not None and len(val[0]) == 0:
        val = None
    started = time.time()
    torch.manual_seed(seed)
    gen = torch.Generator().manual_seed(seed)
    E = np.asarray(E, dtype=np.float64)
    c = E.mean(axis=0)
    n, d = E.shape
    Et = torch.tensor(E - c, dtype=torch.float32)
    Xt = torch.tensor(np.asarray(X, dtype=np.float64) - c, dtype=torch.float32)
    P = np.asarray(P, dtype=bool)
    hard = np.asarray(hard, dtype=np.int64)
    xr_np = np.asarray([int(p[0]) for p in pairs], dtype=np.int64)
    er_np = np.asarray([int(p[1]) for p in pairs], dtype=np.int64)
    xr = torch.tensor(xr_np, dtype=torch.long)
    er = torch.tensor(er_np, dtype=torch.long)
    w = (torch.ones(len(pairs)) if weights is None
         else torch.tensor([float(v) for v in weights], dtype=torch.float32))
    if len(pairs) == 0:
        raise Refused("nothing to train on")
    U = torch.zeros(d, rank, requires_grad=True)
    V = (torch.randn(d, rank, generator=gen) * V_INIT).requires_grad_(True)
    opt = torch.optim.Adam([U, V], lr=lr)

    def mapped(A):
        B = A + (A @ V) @ U.T
        return B / B.norm(dim=1, keepdim=True).clamp_min(1e-12)

    def held_out_mrr():
        if val is None:
            return None
        rows, pos_sets = val
        with torch.no_grad():
            S = (mapped(Xt[torch.tensor(list(rows), dtype=torch.long)]) @ mapped(Et).T).numpy()
        return metrics(ranks_of(S, pos_sets))["mrr"]

    losses, held = [], []
    best = {"mrr": -1.0, "epoch": 0, "U": U.detach().clone(), "V": V.detach().clone()}
    since = 0
    m = len(pairs)
    for epoch in range(1, epochs + 1):
        perm = torch.randperm(m, generator=gen)
        total, count = 0.0, 0
        for b in range(0, m, batch):
            idx = perm[b:b + batch]
            xi, ei, wi = xr[idx], er[idx], w[idx]
            xi_np, ei_np = xr_np[idx.numpy()], er_np[idx.numpy()]
            k = len(idx)
            fe = mapped(Et)
            fx = mapped(Xt[xi])
            S = fx @ fe.T / TEMPERATURE                      # k x n
            # query -> essence: the batch's labels and the query's hard
            # negatives, its positives never in the denominator.
            allowed = torch.from_numpy(candidates_mask(P[xi_np], ei_np, hard[xi_np]))
            l1 = F.cross_entropy(S.masked_fill(~allowed, float("-inf")), ei, reduction="none")
            # essence -> query: the batch's queries, minus those this essence
            # is a positive of (a sister's window, a co-labelled query).
            S2 = S[:, ei].T                                  # [i, j] = fx_j . fe_{e_i}
            allowed2 = torch.from_numpy(reverse_mask(P[xi_np], ei_np))
            l2 = F.cross_entropy(S2.masked_fill(~allowed2, float("-inf")),
                                 torch.arange(k), reduction="none")
            loss = (((l1 + l2) / 2) * wi).sum() / wi.sum()
            reg = lam * torch.trace((U.T @ U) @ (V.T @ V))
            opt.zero_grad()
            (loss + reg).backward()
            opt.step()
            total += loss.detach().item() * k
            count += k
        losses.append(total / count)
        v = held_out_mrr()
        if v is None:
            best = {"mrr": None, "epoch": epoch, "U": U.detach().clone(), "V": V.detach().clone()}
            continue
        held.append(v)
        if v > best["mrr"] + 1e-12:
            best = {"mrr": v, "epoch": epoch, "U": U.detach().clone(), "V": V.detach().clone()}
            since = 0
        else:
            since += 1
            if since >= patience:
                break
    Ub = best["U"].numpy().astype(np.float64)
    Vb = best["V"].numpy().astype(np.float64)
    out = {"U": Ub, "V": Vb, "mean": c, "rank": int(rank), "lambda": float(lam),
           "seed": int(seed), "epochs": len(losses), "best_epoch": int(best["epoch"]),
           "loss_first": float(losses[0]), "loss_last": float(losses[-1]),
           "loss_at_best": float(losses[best["epoch"] - 1]) if best["epoch"] else None,
           "held_out_mrr": best["mrr"], "held_out_first": (held[0] if held else None),
           "losses": [round(x, 5) for x in losses], "held_out": [round(x, 5) for x in held],
           "pairs": int(m), "seconds": round(time.time() - started, 1),
           "norm_uv": float(np.linalg.norm(Ub @ Vb.T))}
    say("    rank " + str(rank) + " lambda " + format(lam, "g") + ": " + str(out["epochs"])
        + " epochs, best at " + str(out["best_epoch"]) + ", loss " + _f(out["loss_first"])
        + " -> " + _f(out["loss_last"]) + ", held-out MRR " + _f(out["held_out_mrr"])
        + ", " + str(out["seconds"]) + " s")
    return out


def fit(data: Data, rank: int, lam: float, seed: int = SEED, sets=DECISION_SETS,
        upweight=None, slice_fold: int = 0, say=None) -> dict:
    """One map over the pairs of `sets`. Early stopping wants a slice the
    training never sees: one fifth of the fold pairs' families (fold
    `slice_fold` of the same grouped split the tables use) is held out for
    that and for nothing else; every pair of the other sets trains, weighted
    by `upweight` where given (set -> weight)."""
    fold = data.of("fold")
    fam = data.families(fold)
    folds = grouped_folds(fold, fam, seed=seed)
    held = [p for p in fold if folds[fam[p["label"]]] == slice_fold]
    train_pairs = [p for p in fold if folds[fam[p["label"]]] != slice_fold]
    other = [p for p in data.of(sets) if p["set"] != "fold"]
    train_pairs += other
    weights = [float((upweight or {}).get(p["set"], 1.0)) for p in train_pairs]
    val_cases = data.cases(held)
    out = train(data.E, data.X, [(p["x"], p["e"]) for p in train_pairs], data.P, data.hard,
                rank, lam, seed=seed, val=([c["x"] for c in val_cases], [c["pos"] for c in val_cases]),
                weights=weights, say=say)
    out.update(sets=list(sets), trained_pairs=len(train_pairs), slice_pairs=len(held),
               slice_cases=len(val_cases),
               weighted={s: float(w) for s, w in (upweight or {}).items()})
    return out


# -- table 1: same-episode, diagnostic ----------------------------------------

def same_episode(data: Data, rank=None, lam=None, mapp=None, seed: int = SEED, say=None) -> dict:
    """Five folds grouped by family over the fold pairs. A fixed map (`mapp`)
    is scored on each fold as it is; a rank trains on three folds, stops
    early on a fourth, and is scored on the fifth, so the reported fold
    never chose the epoch either."""
    say = _safe(say)
    fold = data.of("fold")
    fam = data.families(fold)
    folds = grouped_folds(fold, fam, seed=seed)
    per = []
    fits = []
    for k in range(FOLDS):
        test = data.cases([p for p in fold if folds[fam[p["label"]]] == k])
        if mapp is not None:
            scorer = Scorer(mapp, data.E)
        else:
            stop = (k + 1) % FOLDS
            tr = [p for p in fold if folds[fam[p["label"]]] not in (k, stop)]
            held = data.cases([p for p in fold if folds[fam[p["label"]]] == stop])
            got = train(data.E, data.X, [(p["x"], p["e"]) for p in tr], data.P, data.hard,
                        rank, lam, seed=seed + k,
                        val=([c["x"] for c in held], [c["pos"] for c in held]), say=say)
            fits.append({k2: got[k2] for k2 in ("epochs", "best_epoch", "loss_first",
                                                "loss_last", "held_out_mrr", "seconds")})
            scorer = Scorer(trained_map(got["U"], got["V"], got["mean"], "fold" + str(k)), data.E)
        r = case_ranks(scorer, data, test)
        per.append(dict(metrics(r), fold=k))
    out = {"folds": per, "fits": fits}
    for key in ("recall3", "recall7", "mrr"):
        vals = [f[key] for f in per if f[key] is not None]
        out[key] = {"mean": float(np.mean(vals)) if vals else None,
                    "std": float(np.std(vals)) if vals else None}
    out["cases"] = int(sum(f["n"] for f in per))
    return out


# -- tables 2 and 3: paired -----------------------------------------------------

def bootstrap(diffs, resamples=None, seed: int = SEED) -> dict:
    """The mean of per-case differences and a 95% interval from resampling
    the cases with replacement. `excludes_zero` is the whole of the test."""
    resamples = RESAMPLES if resamples is None else int(resamples)
    d = np.asarray(list(diffs), dtype=np.float64)
    if d.size == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None, "excludes_zero": None}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, d.size, size=(resamples, d.size))
    means = d[idx].mean(axis=1)
    lo, hi = float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))
    return {"n": int(d.size), "mean": float(d.mean()), "lo": lo, "hi": hi,
            "excludes_zero": bool(lo > 0 or hi < 0), "resamples": int(resamples)}


def paired(cases, ra, rb, seed: int = SEED, resamples=None) -> dict:
    """The same cases under map a and map b: per case both ranks, the
    difference in reciprocal rank and in being in the top seven; the
    bootstrap over each; both maps' MRR and recall; who fell out of the
    top seven (a regression, by id) and who came in."""
    ra = np.asarray(ra, dtype=np.int64)
    rb = np.asarray(rb, dtype=np.int64)
    n = len(cases)
    if n == 0:
        return {"n": 0, "pairs": 0, "a": metrics([]), "b": metrics([]),
                "d_mrr": bootstrap([]), "d_recall7": bootstrap([]), "regressions": [],
                "entered": [], "worsened": 0, "improved": 0, "unchanged": 0,
                "mean_rank_change": None, "per_case": []}
    d_rr = 1.0 / rb - 1.0 / ra
    d_top = (rb <= TOP).astype(np.float64) - (ra <= TOP).astype(np.float64)
    return {
        "n": n, "pairs": int(sum(len(c["pairs"]) for c in cases)),
        "a": metrics(ra), "b": metrics(rb),
        "d_mrr": bootstrap(d_rr, resamples, seed),
        "d_recall7": bootstrap(d_top, resamples, seed),
        "regressions": [c["id"] for c, x, y in zip(cases, ra, rb) if x <= TOP < y],
        "entered": [c["id"] for c, x, y in zip(cases, ra, rb) if y <= TOP < x],
        "worsened": int((rb > ra).sum()), "improved": int((rb < ra).sum()),
        "unchanged": int((rb == ra).sum()),
        "mean_rank_change": float((rb - ra).mean()),
        "per_case": [{"id": c["id"], "a": int(x), "b": int(y)} for c, x, y in zip(cases, ra, rb)],
    }


def cross_subsets(data: Data) -> dict:
    """The cross-episode cases, cut the ways the decision needs: X-said for
    the suggester's arm, the assistant's own restatements for theirs, never
    averaged -- and all of them together, which is the decisive set. Each cut
    twice: with the writer-fault queries and without."""
    every = data.cases(data.of(("miss", "hers")))
    by_i = {p["i"]: p for p in data.pairs}

    def has(c, want_set, want_kind=None, ran=None):
        for i in c["pairs"]:
            p = by_i[i]
            if p["set"] == want_set and (want_kind is None or p["x_kind"] == want_kind) \
                    and (ran is None or bool(p["recall_ran"]) == ran):
                return True
        return False

    cuts = {
        "miss said": [c for c in every if has(c, "miss", "said")],
        "miss raw": [c for c in every if has(c, "miss", "raw")],
        "hers": [c for c in every if has(c, "hers")],
        "hers with a suggester": [c for c in every if has(c, "hers", ran=True)],
        "all cross-episode": every,
    }
    out = {}
    for name, cs in cuts.items():
        out[name] = cs
        out[name + ", without writer faults"] = [c for c in cs if not c["writer_fault"]]
    return out


DECISIVE = "all cross-episode, without writer faults"


# -- table 4: the floor ----------------------------------------------------------

def floor_rule(real_worst: float, real_best: float) -> dict:
    """The placement measured with the base floor, carried into another space in
    proportion to the real probes' span: the floor sits under the worst
    real probe by (0.481 - 0.45) / 0.197 of the span, the bar above the
    floor by (0.55 - 0.45) / 0.197 of it. Fed that day's band it gives
    back 0.45 and 0.55 exactly; fed today's identity band -- a shelf
    eleven times larger, every probe finding a closer essence -- it says
    where the base floor itself would sit by the same rule."""
    span = float(real_best) - float(real_worst)
    base_span = BASE_REAL[1] - BASE_REAL[0]
    scale = span / base_span if base_span else 1.0
    floor = float(real_worst) - (BASE_REAL[0] - BASE_FLOOR) * scale
    bar = floor + (BASE_BAR - BASE_FLOOR) * scale
    return {"floor": round(floor, 3), "bar": round(bar, 3), "span": round(span, 4),
            "scale": round(scale, 4)}


def probe_pass(conn, mapp, say=None) -> dict:
    """The seventeen probes under one map, through `search.measure` -- the
    same searches the assistant runs, over the store as it stands -- and what the
    floor table wants of them. The identity is run as the base space on
    purpose (`adapter=False`), which is today's search to the bit."""
    lines = []
    which = False if mapp is None or mapp.is_identity else mapp
    got = search.measure(conn, say=lines.append, adapter=which)
    probes = got["probes"]
    real = [p for p in probes if p["real"]]
    noise = [p for p in probes if not p["real"]]
    bg = [p for p in real if p["lang"] == "BG"]
    real_worst, real_best = min(p["best"] for p in real), max(p["best"] for p in real)
    noise_worst, noise_best = min(p["best"] for p in noise), max(p["best"] for p in noise)
    rule = floor_rule(real_worst, real_best)
    gap = real_worst - noise_best
    out = {
        "adapter": got["adapter"], "shelf": got["shelf"],
        "real": {"worst": real_worst, "best": real_best,
                 "mean": float(np.mean([p["best"] for p in real]))},
        "noise": {"worst": noise_worst, "best": noise_best,
                  "mean": float(np.mean([p["best"] for p in noise]))},
        "bg_real": {"worst": min(p["best"] for p in bg), "best": max(p["best"] for p in bg)} if bg else None,
        "gap": round(gap, 4),
        "gap_in_spans": round(gap / rule["span"], 4) if rule["span"] else None,
        "noise_over_worst_real": [p["text"] for p in noise if p["best"] >= real_worst],
        "proposed": rule,
        "real_under_kept_floor": [p["text"] for p in real if p["best"] < BASE_FLOOR],
        "noise_over_kept_floor": [p["text"] for p in noise if p["best"] >= BASE_FLOOR],
        "real_under_proposed": [p["text"] for p in real if p["best"] < rule["floor"]],
        "noise_over_proposed": [p["text"] for p in noise if p["best"] >= rule["floor"]],
        "real_over_proposed_bar": sum(1 for p in real if p["best"] >= rule["bar"]),
        "probes": probes, "lines": lines,
    }
    if say:
        say("    " + (got["adapter"] or "the base space") + ": real " + _f(real_worst) + " .. "
            + _f(real_best) + ", noise " + _f(noise_worst) + " .. " + _f(noise_best)
            + ", gap " + _f(gap) + ", proposed floor " + _f(rule["floor"]) + " bar " + _f(rule["bar"]))
    return out


# -- table 5: the verdict -----------------------------------------------------

def verdict(name: str, cross: dict, sanity: dict, floor_map, floor_id) -> dict:
    """The go-live rule, one map at a time:
    a paired gain in recall at seven over the decisive set whose bootstrap
    interval excludes zero; no cross-episode query that was in the top seven
    under identity out of it now; the sanity set not worse -- none out of
    the top seven and the mean rank not higher; and the floor's gap, in
    spans of the real probes, not smaller than the identity's."""
    gain = cross["d_recall7"]
    if gain["mean"] is None:
        told = "no cross-episode queries to judge by"
    elif gain["mean"] < 0:
        told = "recall at seven FELL"
    elif gain["mean"] == 0:
        told = "recall at seven did not move"
    elif gain["excludes_zero"]:
        told = "a gain, and the interval excludes zero"
    else:
        told = "a gain the interval cannot tell from noise"
    conds = {
        "gain": {"met": bool(gain["excludes_zero"] and gain["mean"] is not None and gain["mean"] > 0),
                 "said": ("recall@7 " + _f(cross["a"]["recall7"]) + " -> " + _f(cross["b"]["recall7"])
                          + ", paired " + format(gain["mean"] or 0, "+.3f") + " [" + format(gain["lo"] or 0, "+.3f")
                          + ", " + format(gain["hi"] or 0, "+.3f") + "]: " + told
                          + "; paired MRR " + _f(cross["a"]["mrr"]) + " -> " + _f(cross["b"]["mrr"])
                          + " (" + format(cross["d_mrr"]["mean"] or 0, "+.3f") + " ["
                          + format(cross["d_mrr"]["lo"] or 0, "+.3f") + ", "
                          + format(cross["d_mrr"]["hi"] or 0, "+.3f") + "])")},
        "no_regression": {"met": not cross["regressions"],
                          "said": ("no cross-episode query fell out of the top seven"
                                   if not cross["regressions"] else
                                   "fell out of the top seven: " + "; ".join(cross["regressions"]))},
        "sanity": {"met": bool(sanity["n"] and not sanity["regressions"]
                               and sanity["mean_rank_change"] is not None
                               and sanity["mean_rank_change"] <= 0),
                   "said": (str(sanity["worsened"]) + " of " + str(sanity["n"])
                            + " confirmations worse in rank, " + str(sanity["improved"]) + " better, "
                            + str(len(sanity["regressions"])) + " out of the top seven, mean rank change "
                            + format(sanity["mean_rank_change"] or 0, "+.2f")
                            + ", MRR " + _f(sanity["a"]["mrr"]) + " -> " + _f(sanity["b"]["mrr"]))},
    }
    if floor_map is None or floor_id is None:
        conds["floor"] = {"met": None, "said": "the floor was not measured (--quick)"}
    else:
        a, b = floor_id["gap_in_spans"], floor_map["gap_in_spans"]
        conds["floor"] = {"met": bool(b is not None and a is not None and b >= a - 1e-9),
                          "said": ("floor gap " + _f(floor_id["gap"]) + " -> " + _f(floor_map["gap"])
                                   + " (" + _f(a, 2) + " -> " + _f(b, 2) + " spans of the real probes)"
                                   + ("" if b >= a - 1e-9 else " -- SHRUNK"))}
    met = [k for k, c in conds.items() if c["met"] is True]
    unmet = [k for k, c in conds.items() if c["met"] is False]
    unknown = [k for k, c in conds.items() if c["met"] is None]
    return {"map": name, "conditions": conds, "met": met, "unmet": unmet, "unknown": unknown,
            "go": bool(not unmet and not unknown)}


# -- the whole run ----------------------------------------------------------------

def _prefix(prefix=None) -> str:
    """Today's date, or the next free variant of it, so a second run in one
    day does not collide with the first's versions -- names are never
    overwritten."""
    base = prefix or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    have = {v["name"] for v in adapt.versions()}
    cand = base
    k = 1
    while any((cand + "-" + tail) in have for tail in ("center", "r16", "r32", "r16-all", "r32-all")):
        k += 1
        cand = base + "." + str(k)
    return cand


def measure(pairs_path=None, store=None, seed: int = SEED, quick: bool = False,
            say=print, prefix=None, out_dir=None, ranks=RANKS, lambdas=LAMBDAS) -> dict:
    """Build the four maps, run every table, write the candidates and the
    report. Returns the report as a dict; the files are named in it."""
    say = _safe(say)
    started = time.time()
    conn, store_path = adapt_pairs.open_store(store)
    stamp = adapt_pairs.stamp_now()
    try:
        say("measuring over " + _rel(store_path) + " read-only, seed " + str(seed)
            + (", quick: no probe pass, no versions" if quick else ""))
        data = Data.load(conn, pairs_path or latest_pairs(), say)
        R = {"stamp": stamp, "made": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "store": _rel(store_path), "pairs_file": _rel(data.path), "pairs_stamp": data.stamp,
             "embedder": {"model": embed.MODEL, "revision": embed.REVISION},
             "seed": seed, "quick": quick, "shelf": data.left_out,
             "settings": {"temperature": TEMPERATURE, "lambdas": list(lambdas), "ranks": list(ranks),
                          "lr": LR,
                          "epochs": EPOCHS, "patience": PATIENCE, "batch": BATCH, "hard": HARD,
                          "folds": FOLDS, "resamples": RESAMPLES, "top": TOP, "upweight": UPWEIGHT,
                          "v_init": V_INIT, "base_real": list(BASE_REAL), "base_floor": BASE_FLOOR,
                          "base_bar": BASE_BAR},
             "maps": {}, "same_episode": {}, "lambda_choice": {}, "cross": {}, "cross_cases": [],
             "sanity": {}, "floor": {}, "verdict": {}, "shipped": {}, "versions": {}, "notes": []}
        R["sets"] = _set_counts(data)
        _notes_on_data(data, R)

        # -- the maps -------------------------------------------------------
        maps = {"identity": identity_map(data.dim), "centering": centering_map(data.E)}
        R["maps"]["identity"] = {"kind": "identity", "rank": 0}
        R["maps"]["centering"] = {"kind": "centering", "rank": 0,
                                  "mean_norm": float(np.linalg.norm(maps["centering"].mean))}
        say("")
        say("table 1: same-episode, five folds by family, diagnostic only")
        for name in ("identity", "centering"):
            R["same_episode"][name] = same_episode(data, mapp=maps[name], seed=seed)
            say("  " + name.ljust(10) + _se_line(R["same_episode"][name]))
        for rank in ranks:
            tried = {}
            for lam in lambdas:
                say("  rank " + str(rank) + ", lambda " + format(lam, "g") + ":")
                tried[lam] = same_episode(data, rank=rank, lam=lam, seed=seed, say=say)
                say("  r" + str(rank) + " λ=" + format(lam, "g") + " " + _se_line(tried[lam]))
            # The best mean MRR; a tie goes to the larger lambda, nearer the identity.
            best = max(lambdas, key=lambda l: (round(tried[l]["mrr"]["mean"] or 0, 6), l))
            R["lambda_choice"]["r" + str(rank)] = {
                "chosen": best,
                "by_lambda": {format(l, "g"): {k: tried[l][k] for k in ("recall3", "recall7", "mrr")}
                              for l in lambdas}}
            R["same_episode"]["r" + str(rank)] = dict(tried[best], **{"lambda": best})
            say("  r" + str(rank) + ": lambda " + format(best, "g") + " chosen by the same-episode folds")
            say("  fitting the decision map r" + str(rank) + " over the fold pairs")
            got = fit(data, rank, best, seed=seed, sets=DECISION_SETS, say=say)
            maps["r" + str(rank)] = trained_map(got["U"], got["V"], got["mean"], "r" + str(rank))
            R["maps"]["r" + str(rank)] = dict(_fit_summary(got), kind="trained", rank=rank)

        # -- cross-episode and sanity, paired against identity -----------------
        say("")
        say("table 2: cross-episode, paired against identity")
        order = ["identity", "centering"] + ["r" + str(r) for r in ranks]
        scorers = {name: Scorer(maps[name], data.E) for name in order}
        subsets = cross_subsets(data)
        sanity_cases = data.cases(data.of("sanity"))
        all_cases = subsets["all cross-episode"]
        rank_by = {name: case_ranks(scorers[name], data, all_cases) for name in order}
        R["cross_cases"] = [dict({k: c[k] for k in ("id", "text", "set", "x_kind", "turns", "labels",
                                                   "writer_fault", "recall_ran")},
                                 pairs=len(c["pairs"]),
                                 ranks={name: int(rank_by[name][i]) for name in order})
                            for i, c in enumerate(all_cases)]
        pos_of = {c["x"]: i for i, c in enumerate(all_cases)}
        for sub, cs in subsets.items():
            R["cross"][sub] = {}
            idx = [pos_of[c["x"]] for c in cs]
            for name in order[1:]:
                R["cross"][sub][name] = paired(cs, rank_by["identity"][idx], rank_by[name][idx],
                                               seed=seed)
            R["cross"][sub]["identity"] = {"n": len(cs), "pairs": int(sum(len(c["pairs"]) for c in cs)),
                                           "a": metrics(rank_by["identity"][idx])}
        for name in order[1:]:
            say("  " + name.ljust(10) + _cross_line(R["cross"][DECISIVE][name]))
        say("")
        say("table 3: sanity, " + str(len(sanity_cases)) + " cases, paired against identity")
        san_by = {name: case_ranks(scorers[name], data, sanity_cases) for name in order}
        for name in order[1:]:
            R["sanity"][name] = paired(sanity_cases, san_by["identity"], san_by[name], seed=seed)
            say("  " + name.ljust(10) + _sanity_line(R["sanity"][name]))
        R["sanity"]["identity"] = {"n": len(sanity_cases), "a": metrics(san_by["identity"])}

        # -- the floor ---------------------------------------------------------------
        say("")
        if quick:
            say("table 4: the floor -- skipped (--quick)")
        else:
            say("table 4: the floor, seventeen probes under each map")
            for name in order:
                R["floor"][name] = probe_pass(conn, maps[name], say=say)

        # -- the verdict ------------------------------------------------------------
        say("")
        say("table 5: the go-live rule")
        for name in order[1:]:
            R["verdict"][name] = verdict(name, R["cross"][DECISIVE][name], R["sanity"][name],
                                         R["floor"].get(name), R["floor"].get("identity"))
            say("  " + _verdict_line(R["verdict"][name]))

        # -- an aside the tables raise: how much of the mean to take out ------------
        # The plan's centering takes the whole mean of the shelf out of both
        # sides. The queries do not carry the whole of it -- their own mean is
        # shorter and points a little elsewhere -- so the same subtraction
        # leaves every query with a common negative piece of the essences'
        # shape, which favours the essences that carry the most of it. Two
        # weaker means are scored here, nothing trained, no candidate
        # written, so the report shows whether the loss is the plan's mean or
        # the idea of a mean at all.
        say("")
        say("aside: weaker means, diagnostic only")
        fold_x = sorted({p["x"] for p in data.of("fold")})
        qmean = data.X[fold_x].mean(axis=0)
        emean = maps["centering"].mean
        R["aside"] = {"mean_norm": float(np.linalg.norm(emean)),
                      "query_mean_norm": float(np.linalg.norm(qmean)),
                      "cosine_of_means": float(emean @ qmean / np.linalg.norm(emean) / np.linalg.norm(qmean)),
                      "maps": {}}
        fold_cases = data.cases(data.of("fold"))
        id_fold = case_ranks(scorers["identity"], data, fold_cases)
        decisive = subsets[DECISIVE]
        idx_dec = [pos_of[c["x"]] for c in decisive]
        for aname, c in (("half the shelf's mean", 0.5 * emean),
                         ("the fold queries' own mean", qmean)):
            m = adapt.Adapter(aname, np.zeros((data.dim, 0)), np.zeros((data.dim, 0)), c,
                              {"model": embed.MODEL, "revision": embed.REVISION})
            sc = Scorer(m, data.E)
            A = {"same_episode": paired(fold_cases, id_fold, case_ranks(sc, data, fold_cases), seed=seed),
                 "cross": paired(decisive, rank_by["identity"][idx_dec], case_ranks(sc, data, decisive), seed=seed),
                 "sanity": paired(sanity_cases, san_by["identity"], case_ranks(sc, data, sanity_cases), seed=seed),
                 "floor": None}
            if not quick and aname.startswith("half"):
                A["floor"] = probe_pass(conn, m, say=say)
            A["same_episode"].pop("per_case", None)
            R["aside"]["maps"][aname] = A
            say("  " + aname + ": same-episode MRR " + _f(A["same_episode"]["a"]["mrr"]) + " -> "
                + _f(A["same_episode"]["b"]["mrr"]) + "; cross " + _cross_line(A["cross"])
                + "; sanity MRR " + _f(A["sanity"]["a"]["mrr"]) + " -> " + _f(A["sanity"]["b"]["mrr"]))

        # -- the shipped-shape maps ----------------------------------------------------
        say("")
        say("the shipped shape: rank 16 and 32 refit over fold + miss + hers, miss and hers x"
            + format(UPWEIGHT, "g"))
        for rank in ranks:
            name = "r" + str(rank) + "-all"
            lam = R["lambda_choice"]["r" + str(rank)]["chosen"]
            got = fit(data, rank, lam, seed=seed, sets=SHIPPED_SETS,
                      upweight={"miss": UPWEIGHT, "hers": UPWEIGHT}, say=say)
            maps[name] = trained_map(got["U"], got["V"], got["mean"], name)
            R["maps"][name] = dict(_fit_summary(got), kind="trained, shipped shape", rank=rank)
            sc = Scorer(maps[name], data.E)
            rb = case_ranks(sc, data, all_cases)
            S = {"cross": {}, "sanity": None, "floor": None, "in_sample": True}
            for sub, cs in subsets.items():
                idx = [pos_of[c["x"]] for c in cs]
                S["cross"][sub] = paired(cs, rank_by["identity"][idx], rb[idx], seed=seed)
            for i, c in enumerate(all_cases):
                R["cross_cases"][i]["ranks"][name] = int(rb[i])
            S["sanity"] = paired(sanity_cases, san_by["identity"], case_ranks(sc, data, sanity_cases),
                                 seed=seed)
            say("  " + name.ljust(10) + "cross (in-sample) " + _cross_line(S["cross"][DECISIVE]))
            say("  " + name.ljust(10) + _sanity_line(S["sanity"]))
            if not quick:
                S["floor"] = probe_pass(conn, maps[name], say=say)
            S["verdict"] = verdict(name, S["cross"][DECISIVE], S["sanity"], S["floor"],
                                   R["floor"].get("identity"))
            say("  " + _verdict_line(S["verdict"]) + "  (cross-episode in-sample: not a decision)")
            R["shipped"][name] = S
        R["order"] = order + ["r" + str(r) + "-all" for r in ranks]

        # -- the candidates ---------------------------------------------------------------
        say("")
        if quick:
            say("versions: none written (--quick)")
        else:
            pre = _prefix(prefix)
            R["prefix"] = pre
            for name in order[1:] + ["r" + str(r) + "-all" for r in ranks]:
                vname = pre + "-" + ("center" if name == "centering" else name)
                R["versions"][name] = _write_candidate(vname, maps[name], data, R, name)
                say("  wrote " + vname + "  floor " + _f(R["versions"][name]["floor"])
                    + "  bar " + _f(R["versions"][name]["bar"]))
            live = adapt.current()
            R["notes"].append("Nothing was made live: `current` "
                              + ("names " + live.name if live is not None else "names nothing")
                              + " after this run, as before it.")
    finally:
        conn.close()
    R["seconds"] = round(time.time() - started, 1)
    md = report_lines(R)
    out_dir = Path(out_dir) if out_dir else Path(REPORTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / (stamp + ("-quick" if quick else "") + ".md")
    js_path = out_dir / (stamp + ("-quick" if quick else "") + ".json")
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    js_path.write_text(json.dumps(R, indent=1, ensure_ascii=False, default=_plain), encoding="utf-8")
    R["report"], R["json"] = str(md_path), str(js_path)
    say("")
    for line in md:
        say(line)
    say("")
    say("report     " + _rel(md_path) + "  and  " + _rel(js_path) + "  in " + str(R["seconds"]) + " s")
    return R


def _set_counts(data: Data) -> dict:
    out = {}
    for s in adapt_pairs.SETS:
        ps = data.of(s)
        cs = data.cases(ps)
        out[s] = {"pairs": len(ps), "cases": len(cs),
                  "by_kind": dict(Counter(p["x_kind"] for p in ps)),
                  "writer_fault_pairs": sum(1 for p in ps if p["writer_fault"]),
                  "writer_fault_cases": sum(1 for c in cs if c["writer_fault"])}
    hers = data.of("hers")
    out["hers"]["with_suggester_pairs"] = sum(1 for p in hers if p["recall_ran"])
    out["hers"]["with_suggester_cases"] = sum(1 for c in data.cases(hers) if c["recall_ran"])
    out["dropped"] = data.dropped
    return out


def _notes_on_data(data: Data, R: dict):
    fold_labels = {p["label"] for p in data.of("fold")}
    held = sorted({p["label"] for p in data.of(("miss", "hers"))})
    unseen = [l for l in held if l not in fold_labels]
    R["held_out_labels"] = {"labels": held, "without_fold_pairs": unseen}
    no_fold = sorted(set(data.ids) - fold_labels)
    R["essences_without_fold_pairs"] = no_fold
    shared = sorted({p["x"] for p in data.of("fold")}
                    & {p["x"] for p in data.of(("miss", "hers", "sanity"))})
    R["texts_shared_fold_and_held_out"] = shared
    if shared:
        R["notes"].append(str(len(shared)) + " query texts are both a fold window and a held-out "
                          "pair; for those the cross-episode number is in-sample.")


def _fit_summary(got: dict) -> dict:
    keep = ("rank", "lambda", "seed", "epochs", "best_epoch", "loss_first", "loss_last",
            "loss_at_best", "held_out_mrr", "held_out_first", "pairs", "seconds", "norm_uv",
            "sets", "trained_pairs", "slice_pairs", "slice_cases", "weighted", "losses", "held_out")
    return {k: got[k] for k in keep if k in got}


def _write_candidate(vname: str, mapp: adapt.Adapter, data: Data, R: dict, name: str) -> dict:
    """One candidate kept as a version and stamped with what was measured
    with it. Never `use`d."""
    info = R["maps"][name]
    shipped = name.endswith("-all")
    S = R["shipped"][name] if shipped else None
    cross = S["cross"][DECISIVE] if shipped else R["cross"][DECISIVE][name]
    sanity = S["sanity"] if shipped else R["sanity"][name]
    floor = S["floor"] if shipped else R["floor"].get(name)
    verd = S["verdict"] if shipped else R["verdict"][name]
    if info["kind"] == "centering":
        note = ("centering: the mean of " + str(data.left_out["used"]) + " live essence vectors "
                "subtracted and the rest re-normalised; no parameters, nothing trained. ")
    else:
        note = ("rank " + str(info["rank"]) + ", lambda " + format(info["lambda"], "g")
                + ", trained over " + " + ".join(info["sets"]) + " pairs of " + data.stamp
                + (" (miss and hers weighted x" + format(UPWEIGHT, "g") + ")" if shipped else "")
                + ", " + str(info["epochs"]) + " epochs, best at " + str(info["best_epoch"]) + ". ")
    note += ("A CANDIDATE, measured in report " + R["stamp"] + " and never made live by it. "
             + ("Cross-episode numbers are in-sample for this map. " if shipped else "")
             + "Floor and bar by the report's rule: the base placement kept in proportion to "
             "the real probes' span.")
    meta = adapt.write(vname, mapp.U, mapp.V, mapp.mean, model=embed.MODEL, revision=embed.REVISION,
                       trained_on=data.stamp, note=note,
                       numbers={"rank": info["rank"], "lambda": info.get("lambda"),
                                "epochs": info.get("epochs"), "best_epoch": info.get("best_epoch"),
                                "report": R["stamp"]})
    numbers = {
        "cross_n": cross["n"], "cross_recall7": cross["b"]["recall7"], "cross_mrr": cross["b"]["mrr"],
        "cross_recall7_identity": cross["a"]["recall7"], "cross_mrr_identity": cross["a"]["mrr"],
        "cross_gain_recall7": cross["d_recall7"]["mean"],
        "cross_gain_recall7_95": [cross["d_recall7"]["lo"], cross["d_recall7"]["hi"]],
        "cross_gain_mrr": cross["d_mrr"]["mean"],
        "cross_gain_mrr_95": [cross["d_mrr"]["lo"], cross["d_mrr"]["hi"]],
        "cross_regressions": len(cross["regressions"]),
        "cross_in_sample": shipped,
        "sanity_n": sanity["n"], "sanity_worsened": sanity["worsened"],
        "sanity_left_top7": len(sanity["regressions"]),
        "sanity_mean_rank_change": sanity["mean_rank_change"],
        "verdict_met": verd["met"], "verdict_unmet": verd["unmet"],
    }
    if floor is not None:
        numbers.update({"real": [floor["real"]["worst"], floor["real"]["best"]],
                        "noise": [floor["noise"]["worst"], floor["noise"]["best"]],
                        "bg_real": ([floor["bg_real"]["worst"], floor["bg_real"]["best"]]
                                    if floor["bg_real"] else None),
                        "gap": floor["gap"], "gap_in_spans": floor["gap_in_spans"]})
        meta = adapt.stamp(vname, floor=floor["proposed"]["floor"], bar=floor["proposed"]["bar"],
                           numbers=numbers)
    else:
        meta = adapt.stamp(vname, numbers=numbers)
    return {"name": vname, "floor": meta.get("floor"), "bar": meta.get("bar"),
            "files": [_rel(adapt._paths(vname)[0]), _rel(adapt._paths(vname)[1])]}


# -- the report ---------------------------------------------------------------------

def _se_line(se: dict) -> str:
    return ("recall@3 " + _f(se["recall3"]["mean"]) + " ± " + _f(se["recall3"]["std"])
            + "  recall@7 " + _f(se["recall7"]["mean"]) + " ± " + _f(se["recall7"]["std"])
            + "  MRR " + _f(se["mrr"]["mean"]) + " ± " + _f(se["mrr"]["std"])
            + "  over " + str(se["cases"]) + " cases")


def _cross_line(c: dict) -> str:
    if not c["n"]:
        return "no cases"
    return ("n " + str(c["n"]) + "  recall@7 " + _f(c["a"]["recall7"]) + " -> " + _f(c["b"]["recall7"])
            + " (" + _pm(c["d_recall7"]) + ")  MRR " + _f(c["a"]["mrr"]) + " -> " + _f(c["b"]["mrr"])
            + " (" + _pm(c["d_mrr"]) + ")"
            + ("  REGRESSED: " + "; ".join(c["regressions"]) if c["regressions"] else ""))


def _sanity_line(s: dict) -> str:
    if not s["n"]:
        return "no sanity cases"
    return ("n " + str(s["n"]) + "  worse " + str(s["worsened"]) + "  better " + str(s["improved"])
            + "  out of the top seven " + str(len(s["regressions"])) + "  mean rank change "
            + _f(s["mean_rank_change"], 2) + "  MRR " + _f(s["a"]["mrr"]) + " -> " + _f(s["b"]["mrr"]))


def _verdict_line(v: dict) -> str:
    return (v["map"].ljust(10) + ("GO" if v["go"] else "no")
            + "  met: " + (", ".join(v["met"]) or "none")
            + "  not met: " + (", ".join(v["unmet"]) or "none")
            + ("  not measured: " + ", ".join(v["unknown"]) if v["unknown"] else ""))


def _pm(b: dict) -> str:
    if b["mean"] is None:
        return "--"
    return ("Δ " + format(b["mean"], "+.3f") + " [" + format(b["lo"], "+.3f") + ", "
            + format(b["hi"], "+.3f") + "]")


def _table(head, rows) -> list:
    out = ["| " + " | ".join(str(h) for h in head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return out


def report_lines(R: dict) -> list:
    """The report, in plain English, for the assistant: every table with identity
    first, every spread beside its number, the verdicts, the rules, what
    was held out from what, and the caveats."""
    L = []
    order = R.get("order") or ["identity", "centering"] + [
        "r" + str(r) for r in (R["settings"].get("ranks") or RANKS)]
    decision = [m for m in order if not m.endswith("-all")]
    shipped = [m for m in order if m.endswith("-all")]
    sets = R["sets"]
    L.append("# The adapter, measured -- " + R["stamp"])
    L.append("")
    L.append("Over " + R["store"] + " read-only and the pairs file " + R["pairs_file"]
             + " (" + R["pairs_stamp"] + "), embedder " + R["embedder"]["model"] + " at "
             + (R["embedder"]["revision"] or "")[:12] + ", seed " + str(R["seed"])
             + (", quick (no probe pass, no versions written)" if R["quick"] else "") + ".")
    sh = R["shelf"]
    L.append("Ranked over " + str(sh["used"]) + " of " + str(sh["live"]) + " live essences"
             + (" -- left out for a missing vector: " + str(sh["missing"]) if sh["missing"] else "")
             + (" -- left out for a drifted vector: " + str(sh["drifted"]) if sh["drifted"] else "")
             + ". A hit counts on the label, any sister, any co-label.")
    L.append("")
    L.append("Pairs: fold " + str(sets["fold"]["pairs"]) + " (" + ", ".join(
        k + " " + str(v) for k, v in sorted(sets["fold"]["by_kind"].items())) + "; "
        + str(sets["fold"]["cases"]) + " distinct texts) train the decision maps. Held out of "
        "them entirely: miss " + str(sets["miss"]["pairs"]) + " (" + ", ".join(
        k + " " + str(v) for k, v in sorted(sets["miss"]["by_kind"].items())) + "; "
        + str(sets["miss"]["writer_fault_pairs"]) + " with the writer at fault), hers "
        + str(sets["hers"]["pairs"]) + " (" + str(sets["hers"]["with_suggester_pairs"])
        + " with a suggester that turn, " + str(sets["hers"]["pairs"] - sets["hers"]["with_suggester_pairs"])
        + " from before it; " + str(sets["hers"]["cases"]) + " distinct texts), sanity "
        + str(sets["sanity"]["pairs"]) + " (" + str(sets["sanity"]["cases"]) + " distinct texts). "
        "The unit of every paired number is a distinct query text with all its labels as right "
        "answers; the pair counts are beside the case counts.")
    hl = R.get("held_out_labels") or {}
    if hl.get("without_fold_pairs"):
        k = len(hl["without_fold_pairs"])
        L.append("Of the " + str(len(hl["labels"])) + " essences the cross-episode queries point at, "
                 + str(k) + " (" + ", ".join("#" + str(l) for l in hl["without_fold_pairs"])
                 + (") has" if k == 1 else ") have") + " no fold pair at all, so the decision maps "
                 "never saw " + ("it" if k == 1 else "them") + " as a positive.")
    if R.get("essences_without_fold_pairs"):
        L.append(str(len(R["essences_without_fold_pairs"])) + " live essences have no fold pair "
                 "(no readable rows behind them); they are ranked over and serve as negatives only.")
    for n in R["notes"]:
        L.append(n)
    L.append("")
    L.append("## The maps")
    L.append("")
    rows = []
    for m in order:
        info = R["maps"][m]
        if info["kind"] in ("identity",):
            rows.append([m, "today: U, V, c all zero", "--", "--", "--", "--"])
        elif info["kind"] == "centering":
            rows.append([m, "c = mean of the shelf (norm " + _f(info["mean_norm"]) + "), U, V zero",
                         "--", "--", "--", "--"])
        else:
            rows.append([m, "rank " + str(info["rank"]) + " over " + " + ".join(info["sets"])
                         + (", miss and hers x" + format(UPWEIGHT, "g") if m.endswith("-all") else "")
                         + " (" + str(info["trained_pairs"]) + " pairs; " + str(info["slice_cases"])
                         + " fold texts held out for early stopping)",
                         format(info["lambda"], "g"), str(info["epochs"]) + " (best " + str(info["best_epoch"]) + ")",
                         _f(info["loss_first"]) + " -> " + _f(info["loss_last"]),
                         _f(info["held_out_mrr"]) + " (from " + _f(info["held_out_first"]) + ")"])
    L += _table(["map", "what it is", "λ", "epochs", "loss", "early-stop MRR"], rows)
    L.append("")
    L.append("Training: symmetric InfoNCE at temperature " + format(TEMPERATURE, "g")
             + ", plus λ·‖UVᵀ‖²; Adam " + format(LR, "g") + ", at most " + str(EPOCHS)
             + " epochs in batches of " + str(BATCH) + ", stopped after " + str(PATIENCE)
             + " epochs without a better held-out MRR; U from zero, V random at "
             + format(V_INIT, "g") + "; " + str(HARD) + " hard negatives per query mined once in "
             "the base space. λ was chosen from " + ", ".join(format(l, "g") for l in R["settings"]["lambdas"])
             + " by the same-episode folds, which are used for that and for nothing else -- never "
             "for the decision.")
    for r in R["settings"].get("ranks") or RANKS:
        lc = R["lambda_choice"].get("r" + str(r))
        if lc:
            L.append("λ for rank " + str(r) + ": " + ", ".join(
                l + " gives MRR " + _f(v["mrr"]["mean"]) for l, v in lc["by_lambda"].items())
                + " -> " + format(lc["chosen"], "g") + " chosen.")
    L.append("")
    L.append("## 1. Same-episode, diagnostic only")
    L.append("")
    L.append("Five folds grouped by family (an essence, its sisters, and whatever shares a text "
             "with it) over the fold pairs; mean ± std over the folds; a trained map learns on "
             "three folds, stops on a fourth and is scored on the fifth. The "
             "essences quote the rows they stand for, so this is part copy-detection and never a "
             "reason to ship.")
    L.append("")
    rows = []
    for m in decision:
        se = R["same_episode"].get(m)
        if se:
            rows.append([m, _f(se["recall3"]["mean"]) + " ± " + _f(se["recall3"]["std"]),
                         _f(se["recall7"]["mean"]) + " ± " + _f(se["recall7"]["std"]),
                         _f(se["mrr"]["mean"]) + " ± " + _f(se["mrr"]["std"]), se["cases"]])
    L += _table(["map", "recall@3", "recall@7", "MRR", "cases"], rows)
    L.append("")
    L.append("## 2. Cross-episode, the number that decides")
    L.append("")
    L.append("The miss pairs and hers, never trained on by the decision maps, each query ranked "
             "under identity and under the map. Paired: the difference per query in reciprocal rank "
             "and in being in the top seven, a bootstrap of " + str(RESAMPLES)
             + " resamples over the differences (mean, 95% interval), the paired MRR beside "
             "recall at seven. X-said stands for the suggester's arm, the assistant's own "
             "restatements for the hers set; they are cut apart and never averaged, and the decisive "
             "set is all of them together. Each cut is shown with and without the "
             + str(sets["miss"]["writer_fault_pairs"]) + " writer-fault queries (about the "
             "remembering, not the thing). The decisive line is **" + DECISIVE + "**.")
    L.append("")
    for sub in R["cross"]:
        cs = R["cross"][sub]
        idn = cs["identity"]
        L.append("**" + sub + "** -- " + str(idn["n"]) + " queries, " + str(idn["pairs"]) + " pairs"
                 + ("  ← decides" if sub == DECISIVE else ""))
        L.append("")
        rows = [["identity", _f(idn["a"]["recall7"]), "--", _f(idn["a"]["mrr"]), "--", "--"]]
        for m in decision[1:]:
            c = cs[m]
            rows.append([m, _f(c["b"]["recall7"]), _pm(c["d_recall7"]), _f(c["b"]["mrr"]),
                         _pm(c["d_mrr"]), ("none" if not c["regressions"] else "; ".join(c["regressions"]))])
        for m in shipped:
            c = R["shipped"][m]["cross"][sub]
            rows.append([m + " (in-sample)", _f(c["b"]["recall7"]), _pm(c["d_recall7"]), _f(c["b"]["mrr"]),
                         _pm(c["d_mrr"]), ("none" if not c["regressions"] else "; ".join(c["regressions"]))])
        L += _table(["map", "recall@7", "paired Δ recall@7 [95%]", "MRR", "paired Δ MRR [95%]",
                     "fell out of the top seven"], rows)
        L.append("")
    L.append("Every cross-episode query, its rank under each map (1 is best; " + str(TOP)
             + " is the suggester's last title):")
    L.append("")
    rows = []
    for c in R["cross_cases"]:
        rows.append([c["id"] + (" [writer fault]" if c["writer_fault"] else "")
                     + (" [suggester ran]" if c["set"] == "hers" and c["recall_ran"] else ""),
                     '"' + c["text"].replace("|", "/") + '"'] + [c["ranks"].get(m, "--") for m in order])
    L += _table(["query", "text"] + order, rows)
    L.append("")
    L.append("## 3. Sanity: the confirmations may not get worse")
    L.append("")
    L.append("The " + str(sets["sanity"]["pairs"]) + " fetches and finds of an essence the suggester had "
             "named that turn (" + str(sets["sanity"]["cases"]) + " distinct queries), paired the same way. "
             "They train nothing.")
    L.append("")
    idn = R["sanity"]["identity"]
    rows = [["identity", idn["n"], "--", "--", "--", "--", _f(idn["a"]["recall7"]), _f(idn["a"]["mrr"])]]
    for m in decision[1:]:
        s = R["sanity"][m]
        rows.append([m, s["n"], s["worsened"], s["improved"], len(s["regressions"]),
                     format(s["mean_rank_change"], "+.2f"), _f(s["b"]["recall7"]),
                     _f(s["b"]["mrr"]) + " (" + _pm(s["d_mrr"]) + ")"])
    for m in shipped:
        s = R["shipped"][m]["sanity"]
        rows.append([m, s["n"], s["worsened"], s["improved"], len(s["regressions"]),
                     format(s["mean_rank_change"], "+.2f"), _f(s["b"]["recall7"]),
                     _f(s["b"]["mrr"]) + " (" + _pm(s["d_mrr"]) + ")"])
    L += _table(["map", "queries", "worse in rank", "better", "left the top seven",
                 "mean rank change", "recall@7", "MRR"], rows)
    L.append("")
    L.append("## 4. The floor, re-measured")
    L.append("")
    if R["quick"]:
        L.append("Skipped: --quick.")
    else:
        fid = R["floor"].get("identity")
        L.append("The seventeen fixed probes of `search.py` -- ten real, seven noise, half in "
                 "the second language -- under each map, through the search itself. The rule for the "
                 "proposed floor and bar keeps the placement measured with the base floor: the floor 0.45 sat 0.031 "
                 "under the worst real probe 0.481 and the bar 0.55 sat 0.10 above the floor, with the "
                 "real probes spanning 0.197 (0.481 to 0.678); in each map's space the same two "
                 "distances are kept in proportion to that map's real span, so a map that spreads "
                 "every score spreads the floor and the bar with it. The gap is the worst real probe "
                 "minus the best noise probe -- negative means they overlap -- given in the score and "
                 "in spans of the real probes; the verdict reads the second, since a stretched space "
                 "stretches the gap too.")
        if fid:
            L.append("")
            L.append("The base space itself has moved since the floor was measured: that shelf was "
                     "eighteen essences, today's is " + str(fid["shelf"]) + ", and every probe finds a closer "
                     "essence. Under identity today the real probes run " + _f(fid["real"]["worst"])
                     + " to " + _f(fid["real"]["best"]) + " and the noise " + _f(fid["noise"]["worst"])
                     + " to " + _f(fid["noise"]["best"]) + ", so by the same rule the base floor would "
                     "sit at " + _f(fid["proposed"]["floor"]) + " and the bar at " + _f(fid["proposed"]["bar"])
                     + " -- a re-measure the base space is owed whatever is decided about the adapter. "
                     "With the floor kept at " + format(BASE_FLOOR, ".2f") + " today, "
                     + (str(len(fid["noise_over_kept_floor"])) + " noise probe"
                        + ("" if len(fid["noise_over_kept_floor"]) == 1 else "s") + " get"
                        + ("s" if len(fid["noise_over_kept_floor"]) == 1 else "") + " through ("
                        + "; ".join('"' + t + '"' for t in fid["noise_over_kept_floor"]) + ")"
                        if fid["noise_over_kept_floor"] else "no noise probe gets through")
                     + " and " + (str(len(fid["real_under_kept_floor"])) + " real probes are refused"
                                  if fid["real_under_kept_floor"] else "no real probe is refused") + ".")
        L.append("")
        rows = []
        for m in order:
            fl = R["floor"].get(m) if not m.endswith("-all") else (R["shipped"][m].get("floor"))
            if not fl:
                continue
            rows.append([m, _f(fl["real"]["worst"]) + " .. " + _f(fl["real"]["best"]),
                         _f(fl["noise"]["worst"]) + " .. " + _f(fl["noise"]["best"]),
                         (_f(fl["bg_real"]["worst"]) + " .. " + _f(fl["bg_real"]["best"])) if fl["bg_real"] else "--",
                         format(fl["gap"], "+.3f") + " (" + format(fl["gap_in_spans"], "+.2f") + " spans)",
                         _f(fl["proposed"]["floor"]), _f(fl["proposed"]["bar"]),
                         str(len(fl["noise_over_proposed"])) + " of 7",
                         str(fl["real_over_proposed_bar"]) + " of 10"])
        L += _table(["map", "real probes", "noise probes", "BG real", "gap", "proposed floor",
                     "proposed bar", "noise over the floor", "real over the bar"], rows)
        L.append("")
        for m in order:
            fl = R["floor"].get(m) if not m.endswith("-all") else (R["shipped"][m].get("floor"))
            if not fl or m == "identity":
                continue
            bits = []
            if fid:
                bits.append("the real probes fell by " + format(fid["real"]["mean"] - fl["real"]["mean"], ".3f")
                            + " on average and the noise by "
                            + format(fid["noise"]["mean"] - fl["noise"]["mean"], ".3f")
                            + (" -- the map takes more from what is on the shelf than from what is not"
                               if (fid["real"]["mean"] - fl["real"]["mean"]) > (fid["noise"]["mean"] - fl["noise"]["mean"]) + 0.01
                               else ""))
            bits.append("with the floor kept at " + format(BASE_FLOOR, ".2f") + " "
                        + (str(len(fl["real_under_kept_floor"])) + " of 10 real probes would be refused"
                           if fl["real_under_kept_floor"] else "no real probe would be refused")
                        + " and " + (str(len(fl["noise_over_kept_floor"])) + " of 7 noise probes let through"
                                     if fl["noise_over_kept_floor"] else "no noise let through"))
            n_noise = len(fl["noise_over_proposed"])
            bits.append("under the proposed floor " + _f(fl["proposed"]["floor"]) + " no real probe is "
                        "refused (by construction) and "
                        + ("no noise gets through" if not n_noise
                           else "all seven noise probes get through" if n_noise == 7
                           else str(n_noise) + " noise probe" + ("" if n_noise == 1 else "s") + " get"
                           + ("s" if n_noise == 1 else "") + " through: "
                           + "; ".join('"' + t + '"' for t in fl["noise_over_proposed"])))
            L.append("- " + m + ": " + "; ".join(bits) + ".")
        L.append("")
        L.append("Every probe under every map (the best score on the shelf):")
        L.append("")
        maps_with = [m for m in order if (R["floor"].get(m) if not m.endswith("-all")
                                          else R["shipped"][m].get("floor"))]
        probes = (R["floor"].get("identity") or {}).get("probes") or []
        rows = []
        for i, p in enumerate(probes):
            row = [p["lang"] + " " + ("real" if p["real"] else "noise"), '"' + p["text"] + '"']
            for m in maps_with:
                fl = R["floor"].get(m) if not m.endswith("-all") else R["shipped"][m]["floor"]
                row.append(_f(fl["probes"][i]["best"]))
            rows.append(row)
        L += _table(["probe", "text"] + maps_with, rows)
        L.append("")
    L.append("## 5. The go-live rule")
    L.append("")
    L.append("Per map: (gain) the paired gain in recall@7 over the "
             "decisive set whose 95% bootstrap interval excludes zero; (no_regression) no "
             "cross-episode query that was in the top seven under identity out of it now; (sanity) "
             "none of the confirmations out of the top seven and their mean rank not higher; "
             "(floor) the gap, in spans of the real probes, not smaller than under identity. "
             "Centering ships alone if it clears all four.")
    L.append("")
    for m in decision[1:]:
        v = R["verdict"][m]
        L.append("**" + m + ": " + ("GO -- every condition met" if v["go"] else
                                    "not yet -- " + ("not met: " + ", ".join(v["unmet"]) if v["unmet"] else "")
                                    + ((", " if v["unmet"] else "") + "not measured: " + ", ".join(v["unknown"])
                                       if v["unknown"] else "")) + "**")
        for k, c in v["conditions"].items():
            L.append("- " + k + ": " + ("met" if c["met"] else "NOT met" if c["met"] is False else "not measured")
                     + " -- " + c["said"])
        L.append("")
    cv = R["verdict"].get("centering")
    if cv and cv["go"]:
        L.append("**Centering alone meets the rule. By the plan, centering ships and the trained maps "
                 "do not.**")
        L.append("")
    elif cv:
        L.append("Centering alone does not meet the rule (" + ", ".join(cv["unmet"] or cv["unknown"]) + ").")
        L.append("")
    if R.get("aside"):
        A = R["aside"]
        L.append("## An aside the tables raised: how much of the mean to take out")
        L.append("")
        L.append("The plan's centering takes the whole mean of the shelf out of both sides. The queries "
                 "do not carry the whole of it: the shelf's mean has norm " + _f(A["mean_norm"])
                 + ", the fold queries' own mean " + _f(A["query_mean_norm"]) + ", at a cosine of "
                 + _f(A["cosine_of_means"]) + " to each other. Subtracting the shelf's mean from a query "
                 "leaves it with a common negative piece of the essences' shape, which favours the "
                 "essences that carry the most of that shape whatever the query is about. Two weaker "
                 "means, nothing trained, no candidate written -- diagnostic only, to show "
                 "whether the loss is the plan's mean or the idea of a mean at all. The trained maps "
                 "start from the plan's mean and can only scale that direction, never put it back.")
        L.append("")
        rows = []
        for aname, S in A["maps"].items():
            se, cr, sa = S["same_episode"], S["cross"], S["sanity"]
            rows.append([aname, _f(se["a"]["mrr"]) + " -> " + _f(se["b"]["mrr"]),
                         _f(cr["a"]["recall7"]) + " -> " + _f(cr["b"]["recall7"]) + " (" + _pm(cr["d_recall7"]) + ")",
                         _f(cr["a"]["mrr"]) + " -> " + _f(cr["b"]["mrr"]) + " (" + _pm(cr["d_mrr"]) + ")",
                         "none" if not cr["regressions"] else "; ".join(cr["regressions"]),
                         _f(sa["a"]["mrr"]) + " -> " + _f(sa["b"]["mrr"]) + " (" + _pm(sa["d_mrr"]) + ")",
                         (_f(S["floor"]["real"]["worst"]) + " .. " + _f(S["floor"]["real"]["best"]) + " / "
                          + _f(S["floor"]["noise"]["worst"]) + " .. " + _f(S["floor"]["noise"]["best"])
                          + ", gap " + format(S["floor"]["gap_in_spans"], "+.2f") + " spans")
                         if S.get("floor") else "not measured"])
        L += _table(["mean", "same-episode MRR (all fold texts)", "cross recall@7 (decisive)",
                     "cross MRR", "fell out of the top seven", "sanity MRR", "real / noise probes"], rows)
        L.append("")
    if shipped:
        L.append("## The shipped shape: refit over fold + miss + hers")
        L.append("")
        L.append("The two trained maps fit again over everything the plan says a shipped map learns "
                 "from -- fold + miss + hers, miss and hers weighted x" + format(UPWEIGHT, "g")
                 + ", the same λ each -- and measured again. Their cross-episode numbers are "
                 "IN-SAMPLE: the queries were in the training. They are here to show that the "
                 "shipped shape does not regress on what it was shown, and where its floor lands; "
                 "they decide nothing.")
        L.append("")
        for m in shipped:
            S = R["shipped"][m]
            v = S["verdict"]
            L.append("**" + m + "** -- conditions read as above, cross-episode in-sample: met "
                     + (", ".join(v["met"]) or "none") + "; not met " + (", ".join(v["unmet"]) or "none")
                     + ("; not measured " + ", ".join(v["unknown"]) if v["unknown"] else "") + ".")
            for k, c in v["conditions"].items():
                L.append("- " + k + ": " + c["said"])
            L.append("")
    if R.get("versions"):
        L.append("## The candidates")
        L.append("")
        L.append("Written under data/adapter/ and stamped with the proposed floor and bar and the key "
                 "numbers. None is live: `current` is untouched.")
        L.append("")
        for m, v in R["versions"].items():
            L.append("- " + m + " -> " + v["name"] + "  floor " + _f(v["floor"]) + "  bar " + _f(v["bar"]))
        L.append("")
    L.append("## Caveats")
    L.append("")
    L.append("- The same-episode table is diagnostic: the essences quote the rows they stand for, "
             "so a fold pair is part copy-detection. It chose λ and nothing else.")
    L.append("- The decisive set is thin: " + str(R["cross"][DECISIVE]["identity"]["n"])
             + " queries without the writer-fault ones, " + str(R["cross"]["all cross-episode"]["identity"]["n"])
             + " with. Recall at seven moves in steps of one over that; the bootstrap interval says "
             "what the steps are worth, and a wide one is the honest answer.")
    L.append("- Two miss queries are the writer's fault -- about remembering, not the thing -- and no "
             "map over the essences can mend a query that names no subject; they are shown with and "
             "without.")
    L.append("- Early stopping needs a slice the training never sees, so each decision map trained "
             "over four fifths of the fold pairs' families and stopped on the fifth. The shipped-shape "
             "maps did the same with the fold pairs and took every miss and hers pair.")
    L.append("- The same query with two labels is one observation with two right answers; a hit on a "
             "sister or a co-label counts.")
    L.append("- The floor rule keeps the placement measured with the base floor in proportion to the "
             "real probes' span; it is one rule stated here, and it can be changed. The gap is judged "
             "in the same unit. The base space has moved since then and is owed a re-measure of its own.")
    L.append("- X-said fits the adapter to today's query writer; when the writer is retrained these "
             "numbers and the floor are re-measured before anything stays live.")
    L.append("- Nothing here went live. `adapt.use` was not called and `current` was not written.")
    return L


# -- the command line ---------------------------------------------------------------

def _args(argv, flags=(), opts=()) -> tuple:
    got_flags, got_opts, rest = {f: False for f in flags}, {o: None for o in opts}, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in flags:
            got_flags[a] = True
        elif a in opts and i + 1 < len(argv):
            got_opts[a] = argv[i + 1]
            i += 1
        else:
            rest.append(a)
        i += 1
    return got_flags, got_opts, rest


def _main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    what = (argv[1] if len(argv) > 1 else "").lower()
    try:
        if what == "measure":
            flags, opts, rest = _args(argv[2:], flags=("--quick",),
                                      opts=("--pairs", "--seed", "--out", "--prefix", "--store"))
            if rest:
                print("  measure takes --pairs FILE, --seed N, --quick, --out DIR, --prefix NAME, "
                      "--store PATH, not " + repr(rest[0]))
                return 2
            measure(pairs_path=opts["--pairs"], store=opts["--store"],
                    seed=int(opts["--seed"] or SEED), quick=flags["--quick"],
                    prefix=opts["--prefix"], out_dir=opts["--out"])
            return 0
        if what == "train":
            flags, opts, rest = _args(argv[2:], flags=("--all",),
                                      opts=("--rank", "--pairs", "--name", "--lambda", "--seed", "--store"))
            if rest or not opts["--rank"]:
                print("  train needs --rank R; takes --pairs FILE, --name NAME, --all, --lambda L, "
                      "--seed N, --store PATH")
                return 2
            rank, seed = int(opts["--rank"]), int(opts["--seed"] or SEED)
            conn, store_path = adapt_pairs.open_store(opts["--store"])
            try:
                data = Data.load(conn, opts["--pairs"] or latest_pairs(), print)
                if opts["--lambda"]:
                    lam = float(opts["--lambda"])
                else:
                    tried = {}
                    for l in LAMBDAS:
                        tried[l] = same_episode(data, rank=rank, lam=l, seed=seed, say=print)["mrr"]["mean"]
                    lam = max(LAMBDAS, key=lambda l: (round(tried[l] or 0, 6), l))
                    print("lambda     " + format(lam, "g") + " chosen by the same-episode folds: "
                          + ", ".join(format(l, "g") + " " + _f(v) for l, v in tried.items()))
                sets = SHIPPED_SETS if flags["--all"] else DECISION_SETS
                got = fit(data, rank, lam, seed=seed, sets=sets,
                          upweight=({"miss": UPWEIGHT, "hers": UPWEIGHT} if flags["--all"] else None),
                          say=print)
            finally:
                conn.close()
            name = opts["--name"] or (datetime.now(timezone.utc).strftime("%Y-%m-%d") + "-r" + str(rank)
                                      + ("-all" if flags["--all"] else ""))
            meta = adapt.write(name, got["U"], got["V"], got["mean"], model=embed.MODEL,
                               revision=embed.REVISION, trained_on=data.stamp,
                               numbers={"rank": rank, "lambda": lam, "epochs": got["epochs"],
                                        "best_epoch": got["best_epoch"], "held_out_mrr": got["held_out_mrr"]},
                               note=("rank " + str(rank) + ", lambda " + format(lam, "g") + ", trained over "
                                     + " + ".join(sets) + " pairs of " + data.stamp + ", " + str(got["epochs"])
                                     + " epochs, best at " + str(got["best_epoch"]) + ". Not measured: "
                                     "no floor or bar until `python -m server.search floor " + name + "`."))
            print("wrote      " + meta["name"] + "  rank " + str(meta["rank"]) + "  no floor or bar yet; "
                  "not live")
            return 0
    except (Refused, adapt.Refused, adapt_pairs.Refused) as exc:
        print("  refused: " + str(exc))
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
