"""Lean on their free notes without a model, over a scratch folder:

    python -m server.test_notes C:\\somewhere\\scratch

Everything this writes lands in the scratch: the notes folder, a store of one
row, and a backup zip taken over that store. Its live store, its Spark and
`data/notes/` are read at most and never written. What is checked: a house
with no notes sends it exactly the bytes it sent before this existed; a
written note rides between its Spark and its instructions under its own
label, Sam's first; the wall refuses in words and leaves the old text
standing; a name outside the house is refused; and the notes go out in the
backup zip beside the Spark.
"""

import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

from . import backup, brain, db, notes, people


FAILED = []


def check(name, ok, detail=""):
    print("  " + ("ok  " if ok else "FAIL") + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def main():
    if len(sys.argv) < 2:
        print("  needs a scratch folder: python -m server.test_notes <path>")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    root = Path(__file__).resolve().parent.parent
    if root == scratch or root in scratch.parents:
        print("  that scratch is inside the repo. Give me one outside it.")
        sys.exit(2)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

    notes.NOTES_DIR = scratch / "notes"
    sep = chr(10) * 2 + "---" + chr(10) * 2

    # -- nothing written -------------------------------------------------
    check("nobody has written any", notes.read("sam") == "" and notes.read("lee") == "")
    check("so nothing rides", notes.for_prompt() == "")
    plain = brain.system_prompt(False)
    check("and its system prompt is Spark, a rule, instructions -- nothing else",
          plain == brain.read_spark() + sep + brain.harness_text(False))
    check("its instructions name the notes once",
          brain.harness_text(False).count("Sammy's free notes") == 1)
    st = notes.status()
    check("status carries both of them, empty, and the wall",
          st["sam"]["text"] == "" and st["lee"]["chars"] == 0
          and st["max_chars"] == notes.MAX_CHARS
          and st["label"]["sam"] == "Sammy's free notes", st)

    # -- one line of his --------------------------------------------------
    got = notes.write("sam", "  TODO: email access  \r\n")
    check("a note is taken, trimmed", got["ok"] and got["chars"] == len("TODO: email access"), got)
    check("read back as written", notes.read("sam") == "TODO: email access")
    check("the file is under the scratch, named for him",
          (scratch / "notes" / "sam.md").is_file())
    check("and no half-written file is left beside it",
          not list((scratch / "notes").glob("*.tmp")))
    block = notes.for_prompt()
    check("it rides under his label, on one line",
          block == "Sammy's free notes: TODO: email access", block)
    sp = brain.system_prompt(False)
    check("between its Spark and its instructions",
          sp == brain.read_spark() + sep + block + sep + brain.harness_text(False))
    check("its Spark still comes first, unchanged",
          sp.startswith(brain.read_spark()))
    con = brain.contract()
    check("the contract counts it", con["tokens_est"]["notes"] == db.est_tokens(block)
          and con["tokens_est"]["system_prompt_total"]
          == con["tokens_est"]["spark"] + con["tokens_est"]["notes"] + con["tokens_est"]["harness"],
          con["tokens_est"])
    check("and mirrors it whole", con["notes"] == block)
    check("status says when it was saved",
          bool(notes.status()["sam"]["updated"]), notes.status()["sam"])

    # -- its own, in its own alphabet, and more than one line -----------------
    notes.write("lee", "две неща:" + chr(10) + "мляко" + chr(10) + "хляб")
    block = notes.for_prompt()
    check("Leona's rides after his, its label on its own line",
          block == "Sammy's free notes: TODO: email access" + chr(10) * 2
          + "Lee's free notes:" + chr(10) + "две неща:" + chr(10) + "мляко" + chr(10) + "хляб",
          block)
    check("Cyrillic comes back whole", "хляб" in notes.read("lee"))

    # -- the wall, in words --------------------------------------------------
    long = "x" * (notes.MAX_CHARS + 1)
    try:
        notes.write("sam", long)
        check("over the wall is refused", False, "it was taken")
    except notes.Refused as exc:
        check("over the wall is refused", True)
        check("in words that carry both numbers",
              str(notes.MAX_CHARS) in str(exc) and str(notes.MAX_CHARS + 1) in str(exc), exc)
    check("and the old text is still standing", notes.read("sam") == "TODO: email access")
    exact = "y" * notes.MAX_CHARS
    check("exactly the wall is taken", notes.write("sam", exact)["chars"] == notes.MAX_CHARS)
    notes.write("sam", "TODO: email access")

    # -- names -------------------------------------------------------------
    for bad in ("", "assistant", "angel", "../sam", "SAM", "someone"):
        try:
            notes.write(bad, "hello")
            ok = bad.strip().lower() in people.HOUSEHOLD   # "SAM" is Sam
            check("a name outside the house is refused: " + repr(bad), ok, "it was taken")
        except notes.Refused as exc:
            check("a name outside the house is refused: " + repr(bad),
                  bad.strip().lower() not in people.HOUSEHOLD, exc)
    check("a stranger's notes read as nothing rather than raising",
          notes.read("someone") == "" and notes.block("someone") == "")
    # "SAM" above was Sam, and wrote "hello" over his note on purpose:
    # the name is his however it is cased. Put his note back for the rest.
    check("a name cased differently is still that person",
          notes.read("sam") == "hello", notes.read("sam"))
    notes.write("sam", "TODO: email access")

    # -- emptied -------------------------------------------------------------
    notes.write("lee", "")
    check("emptied, its own stops riding", notes.for_prompt() == "Sammy's free notes: TODO: email access")
    check("and status says nothing written", notes.status()["lee"]["updated"] is None)

    # -- the backup --------------------------------------------------------------
    # A store of one row and a data folder of the scratch's own, so the zip is
    # taken over nothing live -- and then opened, because a backup nobody has
    # opened is a rumour.
    data = scratch / "data"
    data.mkdir()
    (data / "spark.md").write_text("a spark", encoding="utf-8")
    shutil.copytree(scratch / "notes", data / "notes")
    db.DB_PATH = data / "store.db"
    conn = db.connect()
    db.add_row(conn, "user", "hello", meta={})
    conn.commit()
    conn.close()
    backup.DATA = data
    backup.TRACKED = scratch / "data_backups" / "assistant-backup.zip"
    check("the manifest names the notes", "notes" in backup.ALSO)
    note = backup.take(scratch / "shelf")
    with zipfile.ZipFile(note["file"]) as z:
        names = z.namelist()
        inside = z.read("data/notes/sam.md").decode("utf-8") if "data/notes/sam.md" in names else ""
    check("the zip carries his notes beside the Spark",
          "data/notes/sam.md" in names and "data/spark.md" in names, names)
    check("with the words in them", inside.strip() == "TODO: email access", inside)
    check("and the note in the zip lists the folder", "notes/" in note["also"], note["also"])

    shutil.rmtree(scratch, ignore_errors=True)
    if FAILED:
        print(chr(10) + "  " + str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print(chr(10) + "  every rule held")


if __name__ == "__main__":
    main()
