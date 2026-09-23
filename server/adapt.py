"""The adapter: one small map laid over the shelf's vectors at the moment of a
search.

The embedder that reads the essences into vectors -- BGE-M3, pinned to the
commit that wrote every vector on the shelf -- stays exactly as it is, and
so does every vector. Nothing on the shelf is ever rewritten by this. What
this is, is a map applied to both sides of a search, the probe and every
essence, at the moment the two are compared:

    f(v) = normalise(A · (v - c))        A = I + U·Vᵀ

U and V are the vector's width by a small rank r; c is a mean vector. With
U and V at zero and c at zero it is the identity, which is today. With U and
V at zero and c the mean of the shelf it is centering, a map with no
parameters at all: every essence shares a shape -- a date header, first
person, the assistant's own diary voice -- and that shared part inflates
every cosine and bunches the scores; subtracting the mean turns it down.
Whether centering alone is the gain, or a trained U and V are, is what the
measurement decides (`adapt_pairs` and `adapt_train`); this file is where a
version lives, how it is stamped, and how it falls back.

Where it lives. `data/adapter/<name>.npz` holds U, V and the mean;
`data/adapter/<name>.json` holds what it was made over and what was
measured with it -- the embedder's name and revision, the rank, a hash of
the training set, the floor and the bar that belong to this version, the
date, the numbers. Versions are kept the way the Spark keeps its versions,
never overwritten: a name that exists is refused in words. What is measured
with a version afterwards -- its floor, its bar, its figures -- is stamped
into the description and never into the map. `data/adapter/current` is one
line naming the live one, and no file means the identity. It is read fresh
whenever it changes, so `use` from the command line is seen by the running
room without a restart.

Identity or nothing. A version made over a different embedder or revision
than the one loaded is not applied -- likeness would move and nothing would
look broken -- and the search says so in words, naming both. A missing file,
an unreadable one, a `current` naming a version that is not there: the
identity, said in words, never a crash. The floor travels inside the
version, so a rollback -- `use` the previous name, or `use none` -- restores
the floor that was measured with it.

    python -m server.adapt                          where things stand
    python -m server.adapt use <name> | none        which version is live
    python -m server.adapt centering <name>         a centering version from the live shelf's mean
    python -m server.adapt stamp <name> floor=0.31 bar=0.40   the numbers measured with it
"""

import json
import re
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import db, embed
from . import home

ROOT = Path(__file__).resolve().parent.parent
# A variable rather than a constant so a bench can point it at a scratch.
ADAPTER_DIR = home.DATA / "adapter"
CURRENT = "current"

# A version's name is a file's name, so it is kept to what is safe in one --
# and it is never the word that names the live one.
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# What a measurement may write into a version's description afterwards.
# Everything else in there says what the map is, and that never changes.
MEASURED = ("floor", "bar", "numbers", "note")


class Refused(ValueError):
    pass


def _dir() -> Path:
    return Path(ADAPTER_DIR)


def _name(name) -> str:
    name = str(name or "").strip()
    if not NAME.match(name) or name == CURRENT:
        raise Refused("a version is named with letters, digits, dots and "
                      "dashes -- v1, centering-2026-09-04 -- not " + repr(name))
    return name


def _paths(name):
    d = _dir()
    return d / (name + ".npz"), d / (name + ".json")


def _rel(p: Path) -> str:
    """A path as it is said to a person: relative to the repo when inside it."""
    try:
        return str(Path(p).relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def _number(x):
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        raise Refused("a floor or a bar is a number, not " + repr(x))


def _plain(o):
    """numpy's own scalars, made into something json can write."""
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def _short(rev) -> str:
    return (rev or "")[:12]


# -- one version, loaded -----------------------------------------------------

class Adapter:
    """One version, loaded: the map and what is known about it.

    `apply` takes one vector and gives one back; `apply_many` takes a list or
    an array of them, one per row. Both come back unit length, in float64,
    so a dot product between two is their cosine -- the same convention the
    embedder's own vectors follow."""

    def __init__(self, name, U, V, mean, meta):
        self.name = name
        self.meta = dict(meta or {})
        self.U = np.asarray(U, dtype=np.float64)
        self.V = np.asarray(V, dtype=np.float64)
        self.mean = np.asarray(mean, dtype=np.float64).reshape(-1)
        if self.U.ndim != 2 or self.U.shape != self.V.shape:
            raise Refused("adapter " + name + " is malformed: U and V must "
                          "both be width by rank, and they are "
                          + str(self.U.shape) + " and " + str(self.V.shape))
        if self.mean.shape[0] != self.U.shape[0]:
            raise Refused("adapter " + name + " is malformed: the mean is "
                          + str(self.mean.shape[0]) + " numbers wide and the "
                          "map is " + str(self.U.shape[0]))
        self.dim = int(self.U.shape[0])
        self.rank = int(self.U.shape[1])
        self.model = self.meta.get("model")
        self.revision = self.meta.get("revision")
        self.floor = _number(self.meta.get("floor"))
        self.bar = _number(self.meta.get("bar"))
        # A map that moves nothing: no mean to subtract, and factors that
        # are zero or absent. Known so a status can say "the identity" in
        # words rather than showing a page of zeros.
        self.is_identity = bool(not self.mean.any()
                                and (self.rank == 0 or not self.U.any()
                                     or not self.V.any()))

    def _fits(self, X):
        if X.ndim != 2 or X.shape[1] != self.dim:
            raise Refused("adapter " + self.name + " is over vectors of "
                          + str(self.dim) + " numbers and was handed "
                          + ("one of " + str(X.shape[-1]) if X.ndim
                             else "something that is not a vector"))

    def _map(self, X):
        D = X - self.mean
        if self.rank:
            D = D + (D @ self.V) @ self.U.T
        norms = np.linalg.norm(D, axis=1, keepdims=True)
        # A vector the map sends to nothing stays nothing rather than
        # becoming a division by zero; it scores zero against everything.
        norms[norms == 0.0] = 1.0
        return D / norms

    def apply(self, vec):
        X = np.asarray(vec, dtype=np.float64).reshape(1, -1)
        self._fits(X)
        return self._map(X)[0]

    def apply_many(self, vecs):
        X = np.asarray(vecs, dtype=np.float64)
        if X.size == 0:
            return np.zeros((0, self.dim))
        if X.ndim == 1:
            X = X.reshape(1, -1)
        self._fits(X)
        return self._map(X)


# -- the files ----------------------------------------------------------------

def write(name, U, V, mean, **meta) -> dict:
    """Keep one version. Refuses a name that exists -- versions are never
    overwritten, so a search stamped with a name always means one map -- and
    writes the map first, the description second, each whole or not at all.

    The description takes: model, revision (the embedder it was made over;
    the loaded one when not said), trained_on (a hash of the pairs, or
    None), floor, bar (measured with it, or None until they are), made,
    numbers (a free dict of what was measured), note."""
    name = _name(name)
    npz, js = _paths(name)
    if npz.exists() or js.exists():
        raise Refused("there is already an adapter named " + name
                      + " -- versions are never overwritten; the next name "
                      "is the way")
    strange = set(meta) - {"model", "revision", "trained_on", "floor", "bar",
                           "made", "numbers", "note"}
    if strange:
        raise Refused("a version's description does not keep "
                      + ", ".join(sorted(strange)))
    U = np.asarray(U, dtype=np.float32)
    V = np.asarray(V, dtype=np.float32)
    mean = np.asarray(mean, dtype=np.float32).reshape(-1)
    if U.ndim != 2 or U.shape != V.shape:
        raise Refused("U and V must both be width by rank; got "
                      + str(U.shape) + " and " + str(V.shape))
    if mean.shape[0] != U.shape[0]:
        raise Refused("the mean must be as wide as the map: "
                      + str(mean.shape[0]) + " against " + str(U.shape[0]))
    out = {
        "name": name,
        "rank": int(U.shape[1]),
        "dim": int(U.shape[0]),
        "model": meta.get("model") or embed.MODEL,
        "revision": meta.get("revision") or embed.REVISION,
        "trained_on": meta.get("trained_on"),
        "floor": _number(meta.get("floor")),
        "bar": _number(meta.get("bar")),
        "made": meta.get("made") or datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "numbers": dict(meta.get("numbers") or {}),
        "note": str(meta.get("note") or ""),
    }
    _dir().mkdir(parents=True, exist_ok=True)
    tmp = npz.with_name(npz.name + ".tmp")
    with open(tmp, "wb") as f:
        np.savez(f, U=U, V=V, mean=mean)
    tmp.replace(npz)
    tmp = js.with_name(js.name + ".tmp")
    tmp.write_text(json.dumps(out, indent=1, ensure_ascii=False,
                              default=_plain), encoding="utf-8")
    tmp.replace(js)
    return out


def describe(name) -> dict:
    """One version's description, read fresh from its json. Refused in words
    when it is not there or cannot be read."""
    name = _name(name)
    npz, js = _paths(name)
    missing = [_rel(p) for p in (npz, js) if not p.is_file()]
    if missing:
        raise Refused("there is no adapter named " + name + ": "
                      + " and ".join(missing)
                      + (" is" if len(missing) == 1 else " are") + " not there")
    try:
        meta = json.loads(js.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(_rel(js) + " could not be read (" + type(exc).__name__
                      + ": " + str(exc) + ")")
    if not isinstance(meta, dict):
        raise Refused(_rel(js) + " does not describe a version")
    meta.setdefault("name", name)
    return meta


def load(name) -> Adapter:
    """One version, loaded and checked for shape. Refused in words, never a
    stack trace, for everything that can be wrong with two files."""
    name = _name(name)
    meta = describe(name)
    npz, _ = _paths(name)
    try:
        with np.load(npz) as got:
            U, V, mean = got["U"], got["V"], got["mean"]
    except Exception as exc:
        raise Refused(_rel(npz) + " could not be read (" + type(exc).__name__
                      + ": " + str(exc) + ")")
    return Adapter(name, U, V, mean, meta)


def stamp(name, **measured) -> dict:
    """Write what was measured with a version into its description: the
    floor, the bar, the figures, a note. The map is never touched -- these
    are facts found about it after it was made -- and nothing else in the
    description can be changed this way."""
    name = _name(name)
    strange = set(measured) - set(MEASURED)
    if strange:
        raise Refused("only " + ", ".join(MEASURED) + " can be stamped on a "
                      "version, not " + ", ".join(sorted(strange)))
    meta = describe(name)
    for key, value in measured.items():
        if key in ("floor", "bar"):
            meta[key] = _number(value)
        elif key == "numbers":
            meta["numbers"] = dict(meta.get("numbers") or {}, **dict(value or {}))
        else:
            meta["note"] = str(value or "")
    _, js = _paths(name)
    tmp = js.with_name(js.name + ".tmp")
    tmp.write_text(json.dumps(meta, indent=1, ensure_ascii=False,
                              default=_plain), encoding="utf-8")
    tmp.replace(js)
    return meta


def applies(meta) -> tuple:
    """Whether a version made over `meta`'s embedder can be laid over the
    vectors the loaded embedder writes. (True, None), or (False, why) in
    words that name both."""
    model = meta.get("model") or ""
    rev = meta.get("revision") or ""
    if model != (embed.MODEL or "") or rev != (embed.REVISION or ""):
        return False, (
            "adapter " + str(meta.get("name")) + " was made over "
            + (model or "an unnamed model") + " at revision "
            + (_short(rev) or "(none pinned)") + " and the embedder loaded is "
            + embed.MODEL + " at revision "
            + (_short(embed.REVISION) or "(none pinned)")
            + ", so it is not applied: likeness would move and nothing "
            "would look broken. The search runs in the base space.")
    return True, None


def versions() -> list:
    """Every version on the disk, oldest made first, each with whether it
    applies to the loaded embedder -- a broken one is listed with what is
    wrong with it rather than left out."""
    d = _dir()
    if not d.is_dir():
        return []
    names = sorted({p.stem for p in d.iterdir()
                    if p.suffix in (".npz", ".json") and p.stem != CURRENT
                    and NAME.match(p.stem)})
    out = []
    for name in names:
        try:
            meta = describe(name)
        except Refused as exc:
            out.append({"name": name, "rank": None, "dim": None, "model": None,
                        "revision": None, "floor": None, "bar": None,
                        "made": None, "numbers": {}, "note": "",
                        "trained_on": None, "applies": False,
                        "problem": str(exc)})
            continue
        ok, why = applies(meta)
        out.append({"name": name, "rank": meta.get("rank"),
                    "dim": meta.get("dim"), "model": meta.get("model"),
                    "revision": meta.get("revision"),
                    "floor": meta.get("floor"), "bar": meta.get("bar"),
                    "made": meta.get("made"),
                    "numbers": meta.get("numbers") or {},
                    "note": meta.get("note") or "",
                    "trained_on": meta.get("trained_on"),
                    "applies": ok, "problem": why})
    out.sort(key=lambda m: (str(m.get("made") or ""), m["name"]))
    return out


# -- the live one -------------------------------------------------------------
#
# `current` is one short file, and it is looked at on every search: a stat
# and, when anything about it has moved, a read. The verdict is kept against
# what was seen -- the file's name, time and size, the version's own
# description, the embedder loaded -- so `use` from the command line is seen
# by the running room on its next search, and nothing is loaded twice.

_CACHE = {"key": None, "adapter": None, "problem": None}
_LOCK = threading.Lock()


def named():
    """What `current` names, or None when there is no file or it is empty."""
    try:
        return (_dir() / CURRENT).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _key():
    p = _dir() / CURRENT
    try:
        st = p.stat()
        text = p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ("absent",)
    except OSError as exc:
        return ("unreadable", type(exc).__name__ + ": " + str(exc))
    js_seen = None
    if text and NAME.match(text):
        try:
            js = _paths(text)[1].stat()
            js_seen = (js.st_mtime_ns, js.st_size)
        except OSError:
            js_seen = None
    return ("named", text, st.st_mtime_ns, st.st_size, js_seen,
            embed.MODEL, embed.REVISION)


def _resolve():
    key = _key()
    with _LOCK:
        if key == _CACHE["key"]:
            return _CACHE["adapter"], _CACHE["problem"]
    adapter = problem = None
    here = _rel(_dir() / CURRENT)
    try:
        if key[0] == "unreadable":
            problem = (here + " could not be read (" + key[1] + "), so no "
                       "adapter is applied and the search runs in the base space.")
        elif key[0] == "named":
            name = key[1]
            if not name:
                problem = (here + " is empty, so no adapter is applied and the "
                           "search runs in the base space; `use none` removes "
                           "the file, `use <name>` fills it.")
            else:
                try:
                    adapter = load(name)
                except Refused as exc:
                    problem = (here + " names " + name + " and " + str(exc)
                               + ", so no adapter is applied and the search "
                               "runs in the base space.")
                else:
                    ok, why = applies(adapter.meta)
                    if not ok:
                        adapter, problem = None, why
    except Exception as exc:   # never a crash over one small file
        adapter = None
        problem = ("the adapter could not be looked at (" + type(exc).__name__
                   + ": " + str(exc) + "), so none is applied and the search "
                   "runs in the base space.")
    with _LOCK:
        _CACHE.update(key=key, adapter=adapter, problem=problem)
    return adapter, problem


def current():
    """The live adapter, or None: nothing named, or the named one cannot be
    applied. Never raises."""
    return _resolve()[0]


def problem():
    """What is wrong with the named version, in words -- or None. None also
    when nothing is named at all: that is not wrong, it is today."""
    return _resolve()[1]


def why_not():
    """A sentence for when no adapter is live: the problem, or the plain
    fact that none is named. None while one is live."""
    adapter, trouble = _resolve()
    if adapter is not None:
        return None
    return trouble or ("No adapter is on: nothing is named in "
                       + _rel(_dir() / CURRENT) + ", so a search runs in the "
                       "base space, as it always has.")


def use(name_or_none) -> dict:
    """Make one version live, or none. Refuses a name that is not there and
    one that cannot be applied to the loaded embedder -- a `current` naming
    a version that would never be used is a stamp that lies. Returns the
    status afterwards."""
    p = _dir() / CURRENT
    text = str(name_or_none or "").strip()
    if not text or text.lower() in ("none", "off", "identity"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return status()
    name = _name(text)
    adapter = load(name)
    ok, why = applies(adapter.meta)
    if not ok:
        raise Refused(why)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(CURRENT + ".tmp")
    tmp.write_text(name + "\n", encoding="utf-8")
    tmp.replace(p)
    return status()


def status() -> dict:
    """For the Developer tab and the command line: which version is live,
    which is named, every version there is, the embedder they are held to,
    and what is wrong, if anything, in words."""
    adapter, trouble = _resolve()
    out = {
        "current": adapter.name if adapter is not None else None,
        "named": named(),
        "versions": versions(),
        "model": embed.MODEL,
        "revision": embed.REVISION,
        "problem": trouble,
        "floor": adapter.floor if adapter is not None else None,
        "bar": adapter.bar if adapter is not None else None,
        "note": None,
        "folder": str(_dir()),
    }
    if adapter is not None and adapter.floor is None:
        out["note"] = (adapter.name + " carries no floor of its own, so the "
                       "search's base floor stands until one is measured "
                       "with it: python -m server.search floor " + adapter.name
                       + ", then stamp the number on it.")
    return out


# -- centering ----------------------------------------------------------------

def _read_only():
    """The store, opened so that nothing in here can write to it."""
    p = Path(db.DB_PATH)
    if not p.is_file():
        raise Refused("there is no store at " + str(p))
    conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def shelf_mean(conn=None):
    """The mean of the live shelf's vectors: the essences a search runs over,
    each read by the loaded embedder and still saying what it said when it
    was read. Returns (mean, how many). Read-only on the store when no
    connection is handed in."""
    own = conn is None
    if own:
        conn = _read_only()
    try:
        shelf = db.essence_shelf(conn, include_retired=False)
        vectors = db.vectors_for(conn, embed.MODEL)
    finally:
        if own:
            conn.close()
    rows = [vectors[r["id"]]["vec"] for r in shelf
            if r["id"] in vectors
            and vectors[r["id"]]["text_hash"] == db.text_hash(r["text"])]
    if not rows:
        raise Refused("the shelf has no vectors by " + embed.MODEL
                      + " to take a mean of")
    return np.asarray(rows, dtype=np.float64).mean(axis=0), len(rows)


def centering(name, conn=None, note=None) -> dict:
    """Step zero of the plan: the map with no parameters. U and V empty, the
    mean of the shelf as c, so a search subtracts what every essence shares
    and compares what is left. Refuses a name that exists. No floor or bar
    until they are measured with it."""
    name = _name(name)
    mean, n = shelf_mean(conn)
    empty = np.zeros((mean.shape[0], 0), dtype=np.float32)
    return write(name, empty, empty, mean,
                 numbers={"essences": n,
                          "mean_norm": round(float(np.linalg.norm(mean)), 4)},
                 note=note or ("centering: the mean of " + str(n) + " essence "
                               "vectors subtracted and the rest re-normalised; "
                               "no parameters, nothing trained. No floor or "
                               "bar until measured with it."))


# -- the command line ---------------------------------------------------------

def _num(x) -> str:
    return "--" if x is None else format(float(x), ".3f")


def _print_status(s: dict):
    print("embedder   " + s["model"] + "  pinned to "
          + (_short(s["revision"]) or "nothing -- the hub's head"))
    live = next((v for v in s["versions"] if v["name"] == s["current"]), None)
    if live:
        print("live       " + live["name"] + "  rank " + str(live["rank"])
              + "  floor " + _num(live["floor"]) + "  bar " + _num(live["bar"]))
    else:
        print("live       none -- the identity, which is today")
    if s["named"] and s["named"] != s["current"]:
        print("named      " + s["named"] + "  (not applied)")
    if s["problem"]:
        print("problem    " + s["problem"])
    if s["note"]:
        print("note       " + s["note"])
    if not s["versions"]:
        print("versions   none yet -- `centering <name>` makes the first")
    else:
        print("versions   " + str(len(s["versions"])))
        for v in s["versions"]:
            print("  " + v["name"].ljust(22) + "rank " + str(v["rank"]).ljust(5)
                  + "floor " + _num(v["floor"]).ljust(7)
                  + "bar " + _num(v["bar"]).ljust(7)
                  + "made " + str(v["made"] or "?")[:19]
                  + ("" if v["applies"] else
                     "   NOT APPLIED: " + str(v["problem"])))
    print("folder     " + s["folder"])


def _main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    what = (argv[1] if len(argv) > 1 else "status").lower()
    try:
        if what == "status":
            _print_status(status())
            return 0
        if what == "use":
            if len(argv) < 3:
                print("  use needs a version name, or none")
                return 2
            s = use(argv[2])
            print("live       " + (s["current"] or "none -- the identity"))
            if s["note"]:
                print("note       " + s["note"])
            return 0
        if what == "centering":
            if len(argv) < 3:
                print("  centering needs a name for the version it makes")
                return 2
            meta = centering(argv[2])
            print("made       " + meta["name"] + "  over "
                  + str(meta["numbers"]["essences"]) + " essence vectors, "
                  "mean norm " + str(meta["numbers"]["mean_norm"]))
            print("           no floor or bar yet: python -m server.search "
                  "floor " + meta["name"] + " measures them, `stamp` keeps "
                  "them, `use` makes it live")
            return 0
        if what == "stamp":
            if len(argv) < 4:
                print("  stamp needs a name and floor=..., bar=..., note=...")
                return 2
            measured = {}
            for arg in argv[3:]:
                key, _, value = arg.partition("=")
                if key not in ("floor", "bar", "note"):
                    print("  stamp takes floor=, bar= and note=, not " + repr(key))
                    return 2
                measured[key] = value
            meta = stamp(argv[2], **measured)
            print("stamped    " + meta["name"] + "  floor " + _num(meta["floor"])
                  + "  bar " + _num(meta["bar"]))
            return 0
    except Refused as exc:
        print("  refused: " + str(exc))
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
