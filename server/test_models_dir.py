"""Lean on the models folder without a model, over a scratch folder:

    python -m server.test_models_dir C:\\somewhere\\scratch

Everything it writes lands in the scratch: a pretend default cache, a
chosen folder, a pretend LM Studio settings file and the choice itself. The
real `data/models.json`, the real cache and LM Studio's settings are never
touched. What is checked: nothing chosen means the default cache and nothing
handed to the library; a relative path, a drive that is not here, a file and
a folder inside its repo are refused in words and leave nothing written; a
chosen folder is made and remembered and handed to the library; a move
carries every byte, counts them, removes the old copy, treats an identical
copy at the far end as the duplicate it is and leaves a differing one alone
with its name said; leftovers are listed and offered; while a move is under
way the old place is still the one read from; and an empty text clears the
choice.
"""

import json
import os
import shutil
import sys
from pathlib import Path

from . import models_dir as md


FAILED = []


def check(name, ok, detail=""):
    print("  " + ("ok  " if ok else "FAIL") + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def refused(fn, *args):
    try:
        fn(*args)
        return False
    except md.Refused:
        return True


def same(a, b):
    return os.path.normcase(os.path.normpath(str(a))) == \
        os.path.normcase(os.path.normpath(str(b)))


def main():
    if len(sys.argv) < 2:
        print("  needs a scratch folder: python -m server.test_models_dir <path>")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    root = Path(__file__).resolve().parent.parent
    if root == scratch or root in scratch.parents:
        print("  that scratch is inside the repo. Give me one outside it.")
        sys.exit(2)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

    md.SETTINGS_PATH = scratch / "models.json"
    md.LM_STUDIO_SETTINGS = scratch / "lmstudio.json"
    default = scratch / "hub"
    default.mkdir()
    md.default_folder = lambda: default
    md._MOVE.clear()
    md._MOVE["active"] = False

    # -- nothing chosen --------------------------------------------------
    st = md.status()
    check("nothing chosen reads the default cache",
          same(md.here(), default) and md.cache_folder() is None
          and st["folder"] is None and same(st["here"], default), st)
    check("nothing there is said as nothing", st["items"] == [] and st["bytes"] == 0)
    check("LM Studio's folder is unknown without its file", md.lm_studio_folder() is None)
    md.LM_STUDIO_SETTINGS.write_text(
        json.dumps({"downloadsFolder": "D:\\LM Studio Models"}), encoding="utf-8")
    check("LM Studio's folder is read from its own settings",
          md.lm_studio_folder() == "D:\\LM Studio Models")

    # -- refusals --------------------------------------------------------
    check("a relative path is refused", refused(md.set_folder, "models"))
    if not Path("Q:/").exists():
        check("a drive that is not here is refused", refused(md.set_folder, "Q:\\models"))
    afile = scratch / "afile.txt"
    afile.write_text("x", encoding="utf-8")
    check("a file is refused", refused(md.set_folder, str(afile)))
    check("inside its repo is refused",
          refused(md.set_folder, str(root / "data" / "models")))
    check("a refusal writes nothing", not md.SETTINGS_PATH.exists()
          and md.folder() is None)

    # -- a pretend model in the default cache ----------------------------
    snap = default / "models--BAAI--bge-m3" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    big = os.urandom(3 * 1024 * 1024)
    (snap / "model.safetensors").write_bytes(big)
    refs = default / "models--BAAI--bge-m3" / "refs"
    refs.mkdir()
    (refs / "main").write_text("abc", encoding="utf-8")
    weight = len(big) + 2 + 3
    st = md.status()
    check("the model is listed by name and weight",
          len(st["items"]) == 1 and st["items"][0]["name"] == "BAAI/bge-m3"
          and st["items"][0]["bytes"] == weight and st["bytes"] == weight, st["items"])

    # -- choosing --------------------------------------------------------
    chosen = scratch / "chosen"
    got = md.set_folder(str(chosen))
    check("a folder is chosen, made and remembered",
          chosen.is_dir() and got["changed"] and same(md.folder(), chosen), got)
    check("the library is handed the folder", same(md.cache_folder(), chosen))
    check("the settings file says so",
          same(json.loads(md.SETTINGS_PATH.read_text(encoding="utf-8"))["folder"], chosen))
    st = md.status()
    check("the old place shows as a leftover",
          st["leftover"] and same(st["leftover"]["folder"], default)
          and st["leftover"]["bytes"] == weight, st["leftover"])
    check("choosing the same folder again changes nothing",
          not md.set_folder(str(chosen))["changed"])

    # -- moving ----------------------------------------------------------
    out = md.move(default, chosen)
    moved = chosen / "models--BAAI--bge-m3" / "snapshots" / "abc" / "model.safetensors"
    check("every byte moved",
          moved.is_file() and moved.read_bytes() == big
          and (chosen / "models--BAAI--bge-m3" / "refs" / "main").read_text(encoding="utf-8") == "abc"
          and (chosen / "models--BAAI--bge-m3" / "snapshots" / "abc" / "config.json").read_text(encoding="utf-8") == "{}")
    check("the old copy is gone", not (default / "models--BAAI--bge-m3").exists())
    check("the move counted its bytes and named what moved",
          out["done"] == weight and out["total"] == weight
          and out["moved"] == ["BAAI/bge-m3"] and not out["error"]
          and not out["active"] and out["finished"], out)
    check("no part files were left behind",
          not list(chosen.rglob("*.part")))
    st = md.status()
    check("no leftover after the move", st["leftover"] is None, st["leftover"])
    check("read from the chosen folder now",
          same(md.here(), chosen) and same(st["here"], chosen))

    # -- a duplicate at the far end --------------------------------------
    dup = default / "models--x--dup"
    dup.mkdir()
    (dup / "w.bin").write_bytes(b"same")
    shutil.copytree(dup, chosen / "models--x--dup")
    out = md.move(default, chosen)
    check("an identical copy is removed as the duplicate it is",
          not dup.exists() and (chosen / "models--x--dup" / "w.bin").read_bytes() == b"same"
          and out["moved"] == ["x/dup"] and not out["error"], out)

    # -- a differing copy at the far end ---------------------------------
    dif = default / "models--x--dif"
    dif.mkdir()
    (dif / "w.bin").write_bytes(b"old")
    (chosen / "models--x--dif").mkdir()
    (chosen / "models--x--dif" / "w.bin").write_bytes(b"newer")
    out = md.move(default, chosen)
    check("a differing copy is left alone and named",
          dif.is_dir() and (dif / "w.bin").read_bytes() == b"old"
          and out["skipped"] == ["x/dif"] and out["moved"] == []
          and (chosen / "models--x--dif" / "w.bin").read_bytes() == b"newer", out)
    st = md.status()
    check("and it shows as a leftover",
          st["leftover"] and [i["name"] for i in st["leftover"]["items"]] == ["x/dif"],
          st["leftover"])

    # -- during a move, the old place is still read from -----------------
    md._MOVE.update({"active": True, "from": str(default), "started": 1.0})
    check("during a move the old place is still read from",
          same(md.here(), default) and same(md.cache_folder(), default))
    check("and no leftover is offered while it runs", md.status()["leftover"] is None)
    check("a second move is refused while one runs",
          refused(md.start_move, default, chosen))
    md._MOVE.update({"active": False})

    # -- the leftover button's refusals ----------------------------------
    check("leftovers refuse the chosen folder itself",
          refused(md.move_leftovers, str(chosen)))
    check("leftovers refuse a place that is not there",
          refused(md.move_leftovers, str(scratch / "nowhere")))
    empty = scratch / "empty"
    empty.mkdir()
    check("leftovers refuse a place with nothing in it",
          refused(md.move_leftovers, str(empty)))

    # -- clearing --------------------------------------------------------
    got = md.set_folder("")
    check("an empty text clears the choice",
          md.folder() is None and got["changed"] and got["after"] is None
          and same(md.here(), default), got)
    check("leftovers need a chosen folder", refused(md.move_leftovers, str(default)))

    print()
    if FAILED:
        print("  " + str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print("  all good")


if __name__ == "__main__":
    main()
