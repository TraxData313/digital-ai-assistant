"""Lean on the digest organ without a model. Run over a scratch folder:

    python -m server.test_digest C:\\somewhere\\scratch

Points the organ's settings and run folder into the scratch and fakes the
model call; nothing here touches the live room, LM Studio, or the store.
The rules under test are the digest's four conditions: a live path or no
digest at all; lossless on digits; refusals, asks and cut-offs never
digested; a first page under a new specialty arrives whole. Plus the
floor, the assistant's per-send `whole`, and the misses said out loud.
"""

import sys
import time
from pathlib import Path

from . import digest, recall, worker


FAILED = []


def check(name, ok, detail=""):
    print("  " + ("ok " if ok else "FAIL") + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


LONG = ("The bench ran 27 checks over the scratch store and every one held. "
        "The new watcher writes data/watch.json and the ledger shows 1,345 "
        "rows after the run. I merged nothing; the branch assistant/example is "
        "three commits ahead and waits on its word. Costs: $0.0412 this "
        "page. What it must decide: whether the ledger's cap of 40 stands "
        "or moves. ") * 8


def base_out(**over):
    out = {"report": LONG, "ended": "completed", "run": "r1",
           "role": "reader", "page": 2, "name": "wren",
           "asked_whole": False, "refusals": [], "denials": [],
           "asked_her": []}
    out.update(over)
    return out


def main():
    if len(sys.argv) < 2:
        print("give me a scratch folder; I will not run over the live data")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    live = Path(__file__).resolve().parent.parent / "data"
    if scratch in (live, live.parent):
        print("that is the live room; give me a scratch folder")
        sys.exit(2)
    scratch.mkdir(parents=True, exist_ok=True)

    digest.SETTINGS_PATH = scratch / "digest.json"
    digest.RUNS = scratch / "workers"
    (digest.RUNS / "r1").mkdir(parents=True, exist_ok=True)

    # The model side, faked whole: a chosen, loaded model and a scripted
    # answer. Nothing in this file reaches LM Studio.
    real = (recall.settings, recall._models, recall._find, recall._instances,
            recall._switch)
    recall.settings = lambda: {"model": "fake/model"}
    recall._models = lambda: [{"key": "fake/model",
                               "loaded_instances": [{"id": "i1"}]}]
    recall._switch = lambda *a, **k: None
    answers = []

    def fake_chat(key, messages, out_tokens):
        got = answers.pop(0)
        if isinstance(got, Exception):
            raise got
        if callable(got):
            got = got()
        return {"raw": "{}", "paragraph": got, "answered_by": "fake/model"}
    real_chat = digest._chat
    digest._chat = fake_chat

    print("the digest organ, leant on over " + str(scratch))

    try:
        # 1. The knobs clamp and keep their floor semantics.
        cfg = digest.settings()
        check("on by default, floor at 1500", cfg["on"] and cfg["floor_chars"] == 1500)
        got = digest.set_knobs({"floor_chars": 5, "timeout_s": 9999,
                                "nonsense": 12})
        check("a knob past its end is set to the end, not refused",
              got["floor_chars"] == 200 and got["timeout_s"] == 120.0)
        got = digest.set_knobs({"reset": True})
        check("reset puts the defaults back", got["floor_chars"] == 1500)

        # 2. Standing aside: the rules, each one, quiet.
        cfg = digest.settings()
        aside = lambda out: digest.stands_aside(out, digest.settings(),
                                                (out.get("report") or "").strip())
        digest.set_knobs({"on": False})
        check("off stands aside", aside(base_out()) == "off")
        digest.set_knobs({"on": True})
        check("it per-send whole stands aside",
              aside(base_out(asked_whole=True)) is not None)
        check("a short report is under the floor",
              aside(base_out(report="all done, nothing to decide")) == "under the floor")
        check("a cut-off page is never digested",
              "never digested" in aside(base_out(ended="max_turns")))
        check("salvage is never digested",
              aside(base_out(salvaged=True)) is not None)
        check("a refusal on the page stands the digest aside",
              aside(base_out(refusals=[{"tool_name": "Bash"}])) is not None)
        check("an ask on the page stands the digest aside",
              aside(base_out(asked_her=[{"ask": "x"}])) is not None)
        check("a first page under a new specialty arrives whole",
              aside(base_out(role="builder", page=1)) is not None)
        check("a builder's second page may be digested",
              aside(base_out(role="builder", page=2)) is None)
        check("an angel's first page is not held to the specialty rule",
              aside(base_out(role="angel", page=1)) is None)
        no_model = recall.settings
        recall.settings = lambda: {"model": None}
        check("no model chosen stands aside, not a miss",
              aside(base_out()) == "no model chosen")
        recall.settings = no_model

        # 3. Lossless on digits: the check that throws a paragraph away.
        check("a faithful figure passes",
              digest.unfaithful("the ledger shows 1,345 rows", LONG) is None)
        check("a smoothed figure is caught",
              digest.unfaithful("about 1300 rows", LONG) == "1300")
        check("an invented path is caught",
              digest.unfaithful("it wrote data/other/thing.json", LONG)
              == "data/other/thing.json")
        check("a real path passes",
              digest.unfaithful("it writes data/watch.json", LONG) is None)
        check("prose with no figures passes",
              digest.unfaithful("it finished and asks for its word", LONG) is None)

        # 4. A long report is cut with the cut marked.
        cut, was = digest._cut_body("x" * 20000, 9000)
        check("a long report is cut head and tail, said so",
              was and "was cut here" in cut and len(cut) < 20000)
        cut, was = digest._cut_body("short", 9000)
        check("a short report is not cut", not was and cut == "short")

        # 5. The whole of stand_in, happy path: paragraph in, full text on
        # disk, path on the block.
        answers[:] = ["It ran 27 checks and every one held; the ledger "
                      "shows 1,345 rows. It asks whether the cap of 40 "
                      "stands or moves."]
        block = digest.stand_in(base_out())
        check("a digest stands in", block and block["paragraph"] is not None)
        check("the whole text is on disk beside the run",
              (digest.RUNS / "r1" / "report.md").read_text(encoding="utf-8")
              == LONG.strip())
        check("the path stands on the block",
              (block or {}).get("path", "").endswith("report.md"))
        check("the last run is kept whole for Developer",
              (digest.STATE["last"] or {}).get("sent") is not None)

        # 6. A smoothed figure is asked once more; a second offence throws
        # the paragraph away and the report goes whole, said out loud.
        answers[:] = ["about 1300 rows held",
                      "the ledger shows 1,345 rows and every check held"]
        block = digest.stand_in(base_out())
        check("one smoothed figure earns one retry",
              block["paragraph"] is not None and "asked once more"
              in (block.get("note") or ""))
        answers[:] = ["about 1300 rows", "roughly 1400 rows"]
        block = digest.stand_in(base_out())
        check("a second offence is a miss, said",
              block["paragraph"] is None
              and "changed a figure twice" in (block.get("missed") or ""))

        # 7. The deadline is the room's: a slow model is a miss, not a wait.
        digest.set_knobs({"timeout_s": 2})
        answers[:] = [lambda: time.sleep(4) or "late words"]
        block = digest.stand_in(base_out())
        check("a slow model misses out loud",
              "ran out of time" in (block.get("missed") or ""))
        digest.set_knobs({"reset": True})

        # 8. No run folder means no path, and no path means no digest.
        answers[:] = ["never reached"]
        block = digest.stand_in(base_out(run="nowhere"))
        check("no live path, no digest -- the first condition",
              "without a live path is refused" in (block.get("missed") or ""))

        # 9. The model gone mid-call is a miss, not a crash.
        answers[:] = [recall.NoServer("gone")]
        block = digest.stand_in(base_out())
        check("a vanished model is a miss, said",
              "did not answer" in (block.get("missed") or ""))

        # 10. What the assistant actually reads: the row text, three ways.
        plain = worker.report_text(base_out())
        check("no digest leaves the row exactly as it was",
              plain == worker.report_text(base_out(), digest=None))
        stood = worker.report_text(base_out(), digest={
            "paragraph": "It held 27 checks; the ledger shows 1,345 rows.",
            "by": "fake/model", "path": "data/workers/r1/report.md",
            "full_chars": len(LONG.strip())})
        check("a digested row says DIGEST and carries the path",
              "DIGEST" in stood and "data/workers/r1/report.md" in stood)
        check("a digested row does not carry the whole body",
              "assistant/example" not in stood and len(stood) < len(plain))
        missed = worker.report_text(base_out(), digest={
            "paragraph": None, "missed": "it ran out of time at 25 s"})
        check("a miss is said on the row and the report comes whole",
              "tried and missed" in missed and "assistant/example" in missed)
    finally:
        digest._chat = real_chat
        (recall.settings, recall._models, recall._find, recall._instances,
         recall._switch) = real

    if FAILED:
        print(str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print("every rule held")


if __name__ == "__main__":
    main()
