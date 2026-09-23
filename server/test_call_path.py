# Is the failure ours or the model's?
#
# This asks the same question three ways, over a COPY of the store, and
# writes nothing anywhere: its real day-turn call with the real schema, the
# dream call with the dream schema, and a bare CLI call with none of our code
# in it at all. Give it a model:
#
#   python -m server.test_call_path sonnet
#   python -m server.test_call_path opus
#
# If the two of ours pass on one model and the bare one fails on another, the
# code is sound and the model is overloaded. Nothing here touches the live
# store, the voice, or its shelf -- it only makes calls and parses answers.
import json
import subprocess
import sys
import time
from pathlib import Path

from server import brain, db, dream

MODEL = sys.argv[1] if len(sys.argv) > 1 else "sonnet"
db.DB_PATH = (Path(__file__).resolve().parent.parent / "data" / "store.db")

print("store:", db.DB_PATH)
print("model:", MODEL)
print()

conn = db.connect()
results = []


def timed(name, fn):
    start = time.time()
    try:
        out = fn()
        took = time.time() - start
        print("  ok    " + name + "  " + format(took, ".1f") + "s  " + out)
        results.append((name, True))
    except Exception as exc:
        took = time.time() - start
        line = str(exc).splitlines()[0][:160]
        print("  FAIL  " + name + "  " + format(took, ".1f") + "s  "
              + type(exc).__name__ + ": " + line)
        results.append((name, False))


def day_turn():
    """Its real day-turn call: the real system prompt, the real prompt object,
    the real response schema. Everything run_turn does except applying it."""
    prompt = brain.build_prompt(conn, voice_on=False)
    answer = brain.call_claude(
        brain.system_prompt(False),
        json.dumps(prompt, ensure_ascii=False, indent=2),
        model=MODEL, schema=brain.response_schema(False))
    meta = answer.pop("_meta", {})
    # The three parts of what was sent, said apart. On a real day-turn prompt
    # this is the whole question: how much of it is being read back out of the
    # cache at a tenth of the price, and how much is being paid for again.
    sent = meta.get("input_tokens") or 0
    read = meta.get("cache_read_tokens")
    wrote = meta.get("cache_write_tokens")
    split = "no cache figures came back"
    if sent and read is not None:
        split = (str(read) + " read + " + str(wrote or 0) + " written + "
                 + str(meta.get("fresh_input_tokens") or 0) + " fresh = "
                 + str(sent) + " (" + format(100.0 * read / sent, ".0f")
                 + "% from cache)")
    return ("reply " + str(len(answer.get("reply") or "")) + " chars, on ~"
            + str(sent) + " in, by " + str(meta.get("model")) + "\n        "
            + split)


def dream_turn():
    """The dream call: its Spark, the dreaming instructions, the dream schema
    -- the whole night's prompt, asked once, and nothing applied."""
    night = "2026-08-23"
    day = dream.the_day(conn, night)
    standing = dream.standing_ids(conn)
    read = {r["id"] for r in day["rows"] if r["kind"] == "essence"}
    prompt = dream.build(conn, night, "asked", False, day, standing, read,
                         None, 0)
    answer = brain.call_claude(
        dream.system_prompt(),
        json.dumps(prompt, ensure_ascii=False, indent=2),
        model=MODEL, schema=dream.DREAM_SCHEMA, expect="done")
    meta = answer.pop("_meta", {})
    return ("done=" + str(answer.get("done")) + ", folds "
            + str(len(answer.get("fold") or [])) + ", reads "
            + str(len(answer.get("read_essences") or [])
                  + len(answer.get("read_rows") or []))
            + ", by " + str(meta.get("model")))


def bare_cli():
    """No code of ours at all: the CLI, the model, one word back. This is the
    control -- if it fails, nothing we wrote is involved."""
    out = subprocess.run(
        [brain.find_claude(), "-p", "--model", MODEL, "--tools", "",
         "--no-session-persistence", "Reply with the single word: ok"],
        capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError("exited " + str(out.returncode) + ": "
                           + (out.stdout or out.stderr or "")[-200:])
    return "said " + (out.stdout or "").strip()[:40]


timed("bare CLI, no code of ours", bare_cli)
timed("its day turn, real schema", day_turn)
timed("its dream, dream schema", dream_turn)

print()
bad = [n for n, ok in results if not ok]
print(("FAILED on " + MODEL + ": " + ", ".join(bad)) if bad
      else "all three answered on " + MODEL)
conn.close()
