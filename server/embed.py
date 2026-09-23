"""Reading an essence into a vector.

The essence is the door; this is a handle on it. Nothing else is read. The
messages behind a door are still reached through the trail, the way they
always were -- putting them in here would make the door pointless.

Everything below is derived. It reads `rows` and writes only `vectors`, so
the whole store can be thrown away and made again from the text alone. That
is what `rebuild` is for: changing model is not a migration, it is a few
minutes of compute.

    python -m server.embed status     -- what has been read, and by what
    python -m server.embed backfill   -- read whatever is missing or drifted
    python -m server.embed rebuild    -- throw it all away and read again

Nothing here is wired into the assistant's turn beyond writing a vector when
it writes an essence. The reading side is `search.py`.
"""

import os
import sys
import threading
import time

from . import db, models_dir

# One place to change the model. Everything else -- the dimension, the length
# it can take -- is asked of the model itself rather than written down here,
# because a number kept in two places is a number that will disagree.
MODEL = os.environ.get("ASSISTANT_EMBED_MODEL", "BAAI/bge-m3")

# Pinned to one commit of the model: a folder emptied by hand and filled
# again from the hub must come back with the same weights, or the stored
# vectors are read by a model that is not the one that wrote them, likeness
# moves, and nothing looks broken. This is the hub's head since 3 Jul 2024
# (pytorch_model.bin, 2.27 GB), measured at cosine 1.000000 against vectors
# it had written before. Changing the model is `rebuild`, never a quiet
# drift of the head.
REVISION = os.environ.get("ASSISTANT_EMBED_REVISION",
                          "5617a9f61b028005a4858fdac845db406aefb181")

# Set ASSISTANT_EMBED=0 to stop a turn writing vectors. The backfill still works;
# this only decides whether a new essence is read the moment it is written.
ON_WRITE = os.environ.get("ASSISTANT_EMBED", "1") not in ("0", "false", "no")

BATCH = int(os.environ.get("ASSISTANT_EMBED_BATCH", "8"))

_backend = None
# Two threads asking for the model at once -- the automatic memory warming it
# while a turn searches -- must not both build it.
_LOAD = threading.Lock()


class Unavailable(RuntimeError):
    """No model to read with. Said out loud rather than swallowed -- a store
    that quietly stops filling looks exactly like a store that is full."""


def backend():
    """The model, loaded once and kept. The first call is slow: a download the
    very first time, and a second or two of loading after that."""
    global _backend
    if _backend is None:
        with _LOAD:
            if _backend is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as e:
                    raise Unavailable(
                        "sentence-transformers is not installed, so nothing "
                        "can be read into a vector. `pip install "
                        "sentence-transformers`."
                    ) from e
                # Kept where the owner chose under Settings, or in the library's
                # own cache when nothing is chosen -- `models_dir` decides,
                # and says the old place during a move.
                _backend = SentenceTransformer(
                    MODEL, revision=REVISION or None,
                    cache_folder=models_dir.cache_folder())
    return _backend


def ready() -> bool:
    """Whether a read would work, without paying for the model to find out."""
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return True


def limit() -> int:
    """The longest text this model will take whole."""
    return int(backend().max_seq_length)


def count_tokens(text: str) -> int:
    """The model's own count, not our four-characters-to-a-token guess. This
    number decides whether a text fits, so it has to be the real one."""
    tok = backend().tokenizer
    return len(tok(text, add_special_tokens=True, truncation=False)["input_ids"])


def read(texts):
    """Read a handful of texts. Returns one record each, in order.

    A text longer than the model can take is still read -- refusing would
    leave a hole in the store -- but it comes back marked `truncated`, and
    every caller passes that mark along. Silent truncation is the failure this
    whole file is arranged to prevent."""
    model = backend()
    cap = int(model.max_seq_length)
    counts = [count_tokens(t) for t in texts]
    vecs = model.encode(list(texts), batch_size=BATCH,
                        normalize_embeddings=True, show_progress_bar=False)
    out = []
    for text, n, vec in zip(texts, counts, vecs):
        values = [float(x) for x in vec]
        out.append({
            "vec": values,
            "dim": len(values),
            "n_tokens": n,
            "truncated": n > cap,
            "hash": db.text_hash(text),
        })
    return out


def embed_row(conn, row_id: int, force: bool = False):
    """Read one essence and keep the reading. Returns the record, or None if
    the row is not an essence, or has already been read as it now stands."""
    row = db.get_row(conn, row_id)
    if row is None or row["kind"] != "essence":
        return None
    if not force:
        had = db.get_vector(conn, row_id, MODEL)
        if had and had["text_hash"] == db.text_hash(row["text"]):
            return None
    rec = read([row["text"]])[0]
    db.put_vector(conn, row_id, MODEL, rec["dim"], rec["vec"], rec["hash"],
                  n_tokens=rec["n_tokens"], truncated=rec["truncated"])
    return rec


def embed_row_quietly(conn, row_id: int):
    """The version the assistant's turn calls. It can fail -- the model may
    not be there, the machine may be out of memory -- and a failure here must
    never cost the essence just written. Returns something to say as a snag,
    or None when all is well."""
    if not ON_WRITE:
        return None
    try:
        rec = embed_row(conn, row_id)
    except Unavailable as e:
        return ("Essence #" + str(row_id) + " was kept, but not read into a "
                "vector: " + str(e) + " The text is safe and a backfill will "
                "pick it up.")
    except Exception as e:  # a broken model must not break the memory
        return ("Essence #" + str(row_id) + " was kept, but reading it into a "
                "vector failed (" + type(e).__name__ + ": " + str(e)
                + "). The text is safe and a backfill will pick it up.")
    if rec and rec["truncated"]:
        return ("Essence #" + str(row_id) + " is " + str(rec["n_tokens"])
                + " tokens and " + MODEL + " takes " + str(limit())
                + ", so its vector stands for the beginning of it and not the "
                "whole of it. Worth knowing before anything trusts it.")
    return None


def backfill(conn, rebuild: bool = False, say=None):
    """Read every essence that needs reading -- loaded and retired alike. A
    retired essence keeps its vector: it was let go of, not unsaid.

    `rebuild` throws away this model's readings first, which is the honest way
    to start over when anything about the reading has changed."""
    say = say or (lambda *a: None)
    started = time.time()

    if rebuild:
        gone = db.drop_vectors(conn, MODEL)
        say("threw away " + str(gone) + " reading"
            + ("" if gone == 1 else "s") + " by " + MODEL)

    stale = db.stale_vectors(conn, MODEL)
    todo = sorted(set(stale["missing"]) | set(stale["drifted"]))
    total = len(db.essence_rows(conn))
    if not todo:
        say("nothing to do -- all " + str(total) + " essences are read")
        return {"model": MODEL, "essences": total, "read": 0,
                "skipped": total, "drifted": [], "truncated": [],
                "seconds": 0.0, "dim": None}

    say("reading " + str(len(todo)) + " of " + str(total) + " essences with "
        + MODEL + (" (loading the model first)" if _backend is None else ""))
    cap = limit()
    say("it takes " + str(cap) + " tokens whole")

    truncated, dim = [], None
    for i in range(0, len(todo), BATCH):
        rows = db.rows_by_ids(conn, todo[i:i + BATCH])
        recs = read([r["text"] for r in rows])
        for row, rec in zip(rows, recs):
            db.put_vector(conn, row["id"], MODEL, rec["dim"], rec["vec"],
                          rec["hash"], n_tokens=rec["n_tokens"],
                          truncated=rec["truncated"])
            dim = rec["dim"]
            if rec["truncated"]:
                truncated.append({"id": row["id"], "tokens": rec["n_tokens"]})
        say("  " + str(min(i + BATCH, len(todo))) + "/" + str(len(todo)))

    took = time.time() - started
    if truncated:
        say("!! " + str(len(truncated)) + " essence"
            + ("" if len(truncated) == 1 else "s")
            + " did not fit and were read only as far as " + str(cap)
            + " tokens: "
            + ", ".join("#" + str(t["id"]) + " (" + str(t["tokens"]) + ")"
                        for t in truncated))
    say("read " + str(len(todo)) + " in " + format(took, ".1f") + "s")
    return {"model": MODEL, "essences": total, "read": len(todo),
            "skipped": total - len(todo), "drifted": stale["drifted"],
            "truncated": truncated, "seconds": round(took, 2), "dim": dim}


def status(conn) -> dict:
    """What has been read, by what, and what is still owed."""
    stale = db.stale_vectors(conn, MODEL)
    return {
        "model": MODEL,
        "revision": REVISION,
        "folder": str(models_dir.here()),
        "installed": ready(),
        "on_write": ON_WRITE,
        "essences": len(db.essence_rows(conn)),
        "missing": stale["missing"],
        "drifted": stale["drifted"],
        "models": db.vector_models(conn),
    }


def _ids(prefix, ids):
    return (" -- " + ", ".join("#" + str(i) for i in ids[:20])) if ids else ""


def _main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    what = (argv[1] if len(argv) > 1 else "status").lower()
    conn = db.connect()

    if what == "status":
        s = status(conn)
        print("model      " + s["model"] + "  pinned to "
              + (s["revision"][:10] if s["revision"] else "nothing -- the hub's head"))
        print("kept in    " + s["folder"])
        print("installed  " + ("yes" if s["installed"] else "no"))
        print("on write   " + ("yes" if s["on_write"] else "no"))
        print("essences   " + str(s["essences"]))
        print("unread     " + str(len(s["missing"])) + _ids("", s["missing"]))
        print("drifted    " + str(len(s["drifted"])) + _ids("", s["drifted"]))
        for m in s["models"]:
            print("read by    " + m["model"] + "  dim " + str(m["dim"])
                  + "  " + str(m["n"]) + " essences"
                  + "  longest " + str(m["max_tokens"]) + " tokens"
                  + ("  " + str(m["truncated"]) + " TRUNCATED"
                     if m["truncated"] else "")
                  + "  last " + str(m["last"]))
        return 0

    if what in ("backfill", "rebuild"):
        backfill(conn, rebuild=(what == "rebuild"), say=print)
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
