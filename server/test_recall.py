"""The automatic memory's pure parts, leant on without a model or a store.

`python -m server.test_recall`

Nothing here calls LM Studio, loads the embedder, or opens the store. What
is benched is every rule that was measured into `recall.py` and could be
un-measured by an innocent edit: which words of "about" become literal
searches, how the bar and the gap choose what the assistant is shown, what
the reader's why must prove before it is believed, where the window read
starts, and what a copy or a question is. Each check names the failure it
came from, because a rule without its measurement is a rule the next
editor will "tidy".
"""
import sys

from . import db, recall

FAILED = []


def check(name, ok, detail=""):
    mark = "ok  " if ok else "FAIL"
    print("  " + mark + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def _hit(i, score, matched="meaning", **more):
    return dict({"id": i, "title": "t" + str(i), "date": "2026-09-01",
                 "score": score, "matched": matched}, **more)


CFG = {"titles_min": 3, "titles_max": 7, "gap": 0.03, "suggest_floor": 0.55}


def bench_keywords():
    print("keywords_from_about")
    got = recall.keywords_from_about("the 0.45 number")
    check("a decimal stays whole", got == ["0.45", "number"], got)
    got = recall.keywords_from_about("setting up thresholds and 0.45")
    check("a number goes first, 'up' and 'and' drop",
          got == ["0.45", "setting", "thresholds"], got)
    got = recall.keywords_from_about("his cat Pepper, and what his uncle cooks")
    check("names and nouns survive, the rest does not",
          got == ["cat", "Pepper", "uncle"], got)
    got = recall.keywords_from_about("Мурка и старото правило")
    check("Cyrillic kept as written", got == ["Мурка", "старото", "правило"], got)


def bench_pick():
    print("pick")
    hits = [_hit(1, 0.80), _hit(2, 0.79), _hit(3, 0.79), _hit(4, 0.79),
            _hit(5, 0.56), _hit(6, 0.35)]
    shown, held = recall.pick(hits, CFG)
    check("the docstring's example shows four",
          [h["id"] for h in shown] == [1, 2, 3, 4], [h["id"] for h in shown])
    check("and holds the cliff and the low one",
          [h["id"] for h in held] == [5, 6], [h["id"] for h in held])
    hits = [_hit(1, 0.54), _hit(2, 0.53), _hit(3, 0.52)]
    shown, held = recall.pick(hits, CFG)
    check("under the bar the minimum is not filled", shown == [] and len(held) == 3)
    hits = [_hit(1, 0.60), _hit(2, 0.50)]
    shown, held = recall.pick(hits, CFG)
    check("one over the bar shows alone", [h["id"] for h in shown] == [1])
    check("held keeps score order for the rescue read",
          [h["id"] for h in held] == [2])


def bench_why():
    print("_why_holds")
    floor = ("12 Mar. The floor down from 0.50 to 0.45, and the merge that "
             "was wrong to defend.")
    purse = "14 Mar. The purse: $30 of worker spend per rolling five hours."
    check("a number the essence has, holds",
          recall._why_holds("it holds the 0.45 floor the line asks about", floor))
    check("a number the essence lacks, fails (the purse)",
          not recall._why_holds("the 0.45 number", purse))
    check("the line's number must be among the why's (the $30 alibi)",
          not recall._why_holds("it is where the $30 of worker spend, the number "
                                "the line asks after", purse,
                                "that 0.45 number?"))
    check("a why number that IS the line's, holds",
          recall._why_holds("it is where the floor was lowered from 0.50 to 0.45",
                            floor, "that 0.45 number?"))
    check("every number in the why must be in the essence, not only the line's",
          not recall._why_holds("it is where the floor was lowered from 0.60 to 0.45",
                                floor, "that 0.45 number?"))
    cat = "Pepper, the fat grey cat, sits in the talk but will not be held."
    check("a name the essence has, holds",
          recall._why_holds("it is where Pepper refuses to be held", cat))
    check("a name the essence lacks, fails",
          not recall._why_holds("it is where Pepper refuses to be held", purse))
    records = "Old films till morning, the window left open."
    check("no number, no name: one solid word is enough",
          recall._why_holds("it is the night of the films", records))
    check("but not a word made of noise",
          not recall._why_holds("both have the line", records))
    restart_line = "did the restart go through, and who called it?"
    check("a why with the essence's naming and the line's word, holds",
          recall._why_holds('it is where the restart was granted on my own condition '
                            '-- the "restart" the line asks after',
                            floor + " The restart, granted on my own condition.",
                            restart_line))
    check("the pad phrase with one word of the line stapled on fails ('bench')",
          not recall._why_holds('the line asks after "bench',
                                "the bench of carve-out guards", "your bench is green?"))
    check("the line's word alone, with nothing of the essence, fails",
          not recall._why_holds("it is where the restart was mine to call",
                                floor + " restart", restart_line))
    check("the two verdicts are different strings",
          recall.UNRELATED != recall.CHECK_FAILED)
    check("a why about something else fails, whatever names it has",
          not recall._why_holds("it is where the Harbour story, the name the line asks after",
                                "The Harbour story, worked example.", restart_line))
    check("the prompt's placeholder handed back fails",
          not recall._why_holds("it is where a THING, the thing the line asks after",
                                "a thing of no consequence", restart_line))
    check("a four-letter function word is not a tie ('than')",
          not recall._why_holds("it is where the door was better than the ask",
                                "the door was better than the ask", "greyer than tea"))
    check("a mirror of the essence with nothing of the line fails",
          not recall._why_holds("the door, and theirs was better than what I asked for",
                                "the door, and theirs was better than what I asked for",
                                "a grey morning and tea before the next thing"))
    check("cross-language: the line's own word copied into the why, holds",
          recall._why_holds('it is where Murka will not be held -- the "Мурка" the line asks after',
                            "Murka, the cat who won't be held.", "Разкажи ми за Мурка?"))
    check("cross-language without the copied word fails",
          not recall._why_holds("it is where Murka will not be held",
                                "Murka, the cat who won't be held.", "Разкажи ми за Мурка?"))
    check("a name in the line and the essence, at a word boundary, holds",
          recall._why_holds("it is where Vera Lindqvist wrote Мост",
                            "Vera Lindqvist, the poet who wrote Мост.",
                            "who was Vera Lindqvist?"))
    print("read_text")
    top = [{"id": i, "title": "t" + str(i)} for i in range(7)]
    texts = {i: "word " * 900 for i in range(7)}
    lines = [{"who": "Sam", "text": "a line"}]
    sent = recall.read_text(lines, top, texts, {"line_tokens": 200, "read_tokens": 800})
    per_chars = (recall.READ_BUDGET // 7) * db.CHARS_PER_TOKEN
    check("seven reads shrink to the budget",
          len(sent) < 7 * (per_chars + 60) + 400, len(sent))
    sent = recall.read_text(lines, top[:2], texts, {"line_tokens": 200, "read_tokens": 100})
    check("two reads keep their own size",
          len(sent) < 2 * (100 * db.CHARS_PER_TOKEN + 60) + 400, len(sent))


def bench_slice():
    print("_read_slice")
    cap = 100 * db.CHARS_PER_TOKEN
    text = ("front " * 400) + "needle " + ("back " * 400)
    got = recall._read_slice(text, 100, "needle")
    check("the window holds the word", "needle" in got, got[:60])
    check("and is marked as a cut from the middle", got.startswith("… "))
    check("and is about one cap long", abs(len(got) - cap) < 40, len(got))
    got = recall._read_slice(text, 100, None)
    check("no word: the opening, unmarked", got.startswith("front") and got.endswith(" …"))
    got = recall._read_slice("short text", 100, "text")
    check("a short essence comes whole", got == "short text", got)
    got = recall._read_slice(text, 100, "front")
    check("a word already in the opening reads the opening", not got.startswith("… "))


def bench_flat():
    print("_flat")
    check("a why keeps its closing quotation mark ('bench)",
          recall._flat('the bench rule; "bench"') == 'the bench rule; "bench"')
    check("a why wrapped whole in quotes is unwrapped",
          recall._flat('"it is where the loaf rose"') == "it is where the loaf rose")
    check("think-tags and whitespace go",
          recall._flat("<think>hm</think>  a   b\n c") == "a b c")


def bench_first_words():
    print("_first_words")
    long = "word " * 100
    got = recall._first_words(long)
    check("cut at a word, marked", got.endswith(" …") and len(got) <= 143, len(got))
    check("a short one is itself", recall._first_words("22 Aug. Short.") == "22 Aug. Short.")
    check("whitespace folded", recall._first_words("a\n\n  b") == "a b")


def bench_flaw():
    print("flaw")
    lines = [{"who": "Sam", "text": "the garden fence came back at 0.81 metres short, "
                                       "worse than I'd hoped, and I am tired"}]
    check("a label in front is a copy",
          recall.flaw("Sam: the garden fence", lines) == "echo")
    check("eight words lifted whole is a copy",
          recall.flaw("I kept the garden fence came back at 0.81 metres short, worse than "
                      "I'd hoped", lines) == "echo")
    check("sharing words is not a copy",
          recall.flaw("The garden fence, and its 0.81 metres.", lines) is None)
    check("a question is a question",
          recall.flaw("do you remember the kite", lines) == "question")
    check("a Cyrillic question too",
          recall.flaw("Помниш ли хвърчилото", lines) == "question")
    check("'and' in about no longer sends it back (a retired rule)",
          recall.flaw("The kite and the gulls.", lines, "the kite and the gulls") is None)


def bench_writer():
    print("the query writer, and the eight-words guard")
    texts = {7: "The kayak trip, Saturday at nine, bring the thermos and the red one",
             9: "Rex barked at the postman again and the loaf was dense."}
    check("a query copying eight words of an essence's text names it",
          recall.copies_text("the kayak trip, Saturday at nine, bring the thermos and", texts) == 7)
    check("a query sharing words but no run of eight does not",
          recall.copies_text("The kayak, and the thermos on Saturday.", texts) is None)
    check("a short query never copies", recall.copies_text("the kayak", texts) is None)
    check("a title-shaped query against no text is free",
          recall.copies_text("The kayak trip, Saturday at nine, bring the thermos and the red one", {}) is None)
    check("the run is the echo rule's", recall.COPY_RUN == 8)
    models = [{"key": "google/gemma-4-e4b", "publisher": "google"},
              {"key": "smoke-gemma", "publisher": "ada"},
              {"key": "ada/old-style", "publisher": "ada"}]
    check("a trained writer's key at the wire is its bare name",
          recall.lm_key("ada/smoke-gemma") == "smoke-gemma")
    check("a model's key at the wire is its own", recall.lm_key("google/gemma-4-e4b") == "google/gemma-4-e4b")
    check("the assistant's imported model is found under ada/",
          (recall._find(models, "ada/smoke-gemma") or {}).get("key") == "smoke-gemma")
    check("a bare name with another publisher is not the assistant's",
          recall._find([{"key": "smoke-gemma", "publisher": "somebody"}], "ada/smoke-gemma") is None)
    check("the trained writers are listed under ada/ whichever way LM Studio keys them",
          recall.trained_writers(models) == ["ada/old-style", "ada/smoke-gemma"])
    import tempfile
    from pathlib import Path
    old = recall.SETTINGS_PATH
    try:
        with tempfile.TemporaryDirectory() as d:
            recall.SETTINGS_PATH = Path(d) / "recall.json"
            recall.save(model="google/gemma-4-e4b", writer="ada/writer-test")
            cfg = recall.settings()
            check("a trained writer under ada/ is kept", cfg["writer"] == "ada/writer-test")
            check("and is who writes", recall.writer_key(cfg) == "ada/writer-test")
            recall.save(writer="google/gemma-4-e4b")
            check("a writer equal to the model is none", recall.settings()["writer"] is None)
            recall.save(writer="somebody/else")
            check("an unknown writer is none", recall.settings()["writer"] is None)
            recall.save(writer=None)
            check("none means the model writes", recall.writer_key(recall.settings()) == "google/gemma-4-e4b")
    finally:
        recall.SETTINGS_PATH = old
    ws = recall.writers()
    check("the writer options start with the models", [w["key"] for w in ws[:3]] == [m["key"] for m in recall.MODELS])
    block = recall._block("google/gemma-4-e4b", {n: k["default"] for n, k in recall.KNOBS.items()},
                          writer="ada/writer-test")
    check("the block carries the writer", block["writer"] == "ada/writer-test" and block["asked_for"] == "google/gemma-4-e4b")


def bench_summary():
    print("summary")
    block = {"query": "q", "titles": [{"id": 1}], "under_bar": True,
             "set_aside": [{"id": 2}], "held_back": 0}
    line = recall.summary(block)
    check("says a title came from under the bar", "from under the bar" in line, line)
    check("counts the set-aside", "1 set aside" in line, line)
    check("the reader's no is one fixed string", recall.UNRELATED == "does not look related")


def _main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for bench in (bench_keywords, bench_pick, bench_why, bench_slice,
                  bench_flat, bench_first_words, bench_flaw, bench_writer, bench_summary):
        bench()
    print()
    if FAILED:
        print("FAILED: " + ", ".join(FAILED))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
