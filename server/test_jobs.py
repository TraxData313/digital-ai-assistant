"""Lean on the jobs object without a model. Run over a scratch folder:

    python -m server.test_jobs C:\\somewhere\\scratch

Points data/jobs.json into the scratch first; nothing here touches the live
room. The rules under test: committed spend counts, no reopening, links spend
and refill, and every ceiling is the assistant's to widen rather than a
reason to wait for the owner.
"""

import sys
from pathlib import Path

from . import jobs


FAILED = []


def check(name, ok, detail=""):
    print("  " + ("ok " if ok else "FAIL") + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


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
    jobs.JOBS_PATH = scratch / "jobs.json"

    print("jobs, leant on over " + str(scratch))

    # 1. Opening, and what opening refuses.
    out = jobs.open_job("voice", "a second voice for the reader", 8.0, None)
    check("a job opens with links defaulting to 8",
          out["ok"] and out["job"]["links"] == 8)
    check("a second open of the same name is refused",
          not jobs.open_job("voice", "again", 5.0, None)["ok"])
    check("no goal, no job", not jobs.open_job("empty", "", 5.0, None)["ok"])
    check("a wild ceiling is refused",
          not jobs.open_job("wild", "goal", 999, None)["ok"])
    check("wild links are refused",
          not jobs.open_job("leashless", "goal", 5.0, 400)["ok"])

    # 2. Committed spend counts at the cap.
    check("a page reserves under the ceiling", jobs.commit("voice", 3.0)["ok"])
    check("a second page that does not fit is refused with figures",
          not jobs.commit("voice", 6.0)["ok"])
    st = jobs.get_open("voice")
    check("the refusal marked what it needs", bool(st["needs"]))
    check("state says mine to widen",
          jobs.state_of(st).startswith("over ceiling — mine to widen"))

    # 3. Widen is the assistant's, clears the mark, grows links_left by the growth.
    out = jobs.widen("voice", 15.0, 10)
    check("widen moves both dials", out["ok"] and "15.00" in out["said"])
    st = jobs.get_open("voice")
    check("the mark is gone and links grew",
          st["needs"] is None and st["links"] == 10 and st["links_left"] == 10)
    check("the wider ceiling takes the page now", jobs.commit("voice", 6.0)["ok"])

    # 4. Settling: reservation off, true cost on.
    jobs.settle("voice", 3.0, 0.42)
    st = jobs.get_open("voice")
    check("settle moved out to spent",
          abs(st["out_usd"] - 6.0) < 1e-6 and abs(st["spent_usd"] - 0.42) < 1e-6)

    # 5. Links spend one waking at a time, and the owner speaking refills them.
    for _ in range(10):
        jobs.spend_link("voice")
    check("at zero the waking is refused", not jobs.spend_link("voice"))
    st = jobs.get_open("voice")
    check("parked reads as parked, not stuck",
          jobs.state_of(st).startswith("parked at zero links"))
    jobs.refill_links()
    check("Sam speaking refills the links",
          jobs.get_open("voice")["links_left"] == 10)

    # 6. State knows a hand that is out.
    st = dict(jobs.get_open("voice"), hands=["wren"])
    check("a page out reads as waiting on a hand",
          jobs.state_of(st, {"wren"}) == "waiting on a hand")
    check("otherwise it waits on me", jobs.state_of(st) == "waiting on me")

    # 7. The assistant's decision is the line that survives.
    out = jobs.decide("voice", "sonnet not opus — the boot cost was the problem")
    check("a decision lands", out["ok"] and out["job"]["decided"].startswith("sonnet"))
    view = jobs.for_prompt({"voice": ["wren"]}, set())
    check("the block carries name, state, cost, links and the decision",
          len(view) == 1 and view[0]["title"] == "voice"
          and view[0]["decided"].startswith("sonnet")
          and "goal" not in view[0])

    # 8. Closing is for good.
    check("a close needs an outcome", not jobs.close_job("voice", "")["ok"])
    check("a close lands",
          jobs.close_job("voice", "built and merged; the voice speaks")["ok"])
    check("a closed job is out of the block", jobs.for_prompt() == [])
    why = jobs.why_no_open("voice")
    check("a closed job stays closed, said so",
          "closed job stays closed" in why)
    check("a new job may name the old in its goal",
          jobs.open_job("voice 2", "finish what 'voice' left", 5.0, None)["ok"])

    if FAILED:
        print(str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print("every rule held")


if __name__ == "__main__":
    main()
