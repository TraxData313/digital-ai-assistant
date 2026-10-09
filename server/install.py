"""Install an assistant on this machine in one go, from its two repositories.

    python -m server.install <home>             everything below, in order
    python -m server.install <home> --codex-only   just (re)register with Codex

With this code cloned and the assistant's home cloned beside it, this is the
whole install. Each step says what it did, skips what is already done, and
never overwrites a file that is there -- so it is safe to run again, and a
second run after fixing whatever the first one reported finishes the job.

  1. what this machine still lacks: Git, the GitHub CLI signed in, Claude
     Code, Codex, LM Studio -- reported, not installed (they want a person's
     sign-in)
  2. the Python packages in requirements.txt
  3. the home chosen (`home.json`); an empty folder becomes a new assistant,
     asking its name and its owner's, as `python -m server.setup` does
  4. its memories: if the home has no store yet, the newest nightly backup
     from its own private repo's releases (or `--backup <tag>`), unpacked
     beside the tracked files without replacing any of them, then the store
     checked with SQLite's own integrity check
  5. a models folder on a drive this machine does not have is let go of, so
     the embedder downloads into the usual cache instead of failing
  6. the home's `restore/` kit, if it has one: files that lived outside the
     home (Claude's and Codex's own folders, notes in Documents) put back
     where they were, only where nothing is there yet
  7. the privacy hook, if the home keeps a denylist at `privacy/denylist.txt`
  8. Codex: the memory plug (`server/codex_mcp.py`) registered as an MCP
     server and a SessionStart hook for this home, in Codex's own config
  9. the shortcut and start-at-login, and the manager started

Keys are never part of this. API keys are entered again on the Settings page,
the room mints its own pairing keys, and a phone is paired again.
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
REQUIREMENTS = CODE / "requirements.txt"
PLUG = CODE / "server" / "codex_mcp.py"
LEGACY_PLUG = "codex_memory_mcp.py"
WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if WIN else 0

# Where restore/ puts things: its first folder name, and where that lives.
KIT_PLACES = {
    "dot-claude": lambda: Path.home() / ".claude",
    "dot-codex": lambda: codex_home(),
    "Documents": lambda: Path.home() / "Documents",
}
# Never from the kit: the installer writes Codex's hooks itself, and an old
# hand-installed copy of the plug (a `<name>-memory` folder in Codex's own)
# is replaced by the one in this code.
KIT_SKIP = re.compile(r"^dot-codex/(hooks\.json$|[^/]+-memory/)")

# What a backup zip says about itself; not part of the home.
BACKUP_MANIFEST = "what-is-in-here.json"


def say(step, text):
    print(f"[{step}] {text}", flush=True)


def run(args, cwd=None, timeout=900):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=NO_WINDOW)
        return p.returncode == 0, (p.stdout or "").strip(), (p.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "", str(exc)


def console_python() -> str:
    """python.exe, never pythonw.exe: Codex talks to the plug over stdio."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def identity(folder: Path) -> dict:
    return json.loads((folder / "identity.json").read_text(encoding="utf-8"))


# -- 1. what the machine lacks -----------------------------------------------

def check_machine() -> list:
    missing = []
    if not shutil.which("git"):
        missing.append("Git")
    if not shutil.which("gh"):
        missing.append("the GitHub CLI (gh) -- the backups come from it")
    else:
        ok, _, _ = run(["gh", "auth", "status"], timeout=60)
        if not ok:
            missing.append("gh signed in: run `gh auth login`")
    if not shutil.which("claude"):
        missing.append("Claude Code (`claude`), signed in")
    codex_bin = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    if not (shutil.which("codex") or codex_bin.exists() or codex_home().exists()):
        missing.append("Codex, signed in once")
    if not (Path.home() / ".lmstudio").exists():
        missing.append("LM Studio (for the automatic memory's small model)")
    if sys.version_info < (3, 11):
        missing.append("Python 3.11 or newer")
    for m in missing:
        say("machine", "missing: " + m)
    if not missing:
        say("machine", "Git, gh, Claude Code, Codex and LM Studio are all here")
    return missing


# -- 2. packages -------------------------------------------------------------

def install_packages() -> bool:
    ok, out, err = run([sys.executable, "-m", "pip", "install", "-q", "-r", str(REQUIREMENTS)], cwd=CODE)
    say("packages", "requirements installed" if ok else "pip failed: " + (err or out)[-400:])
    return ok


# -- 3. the home -------------------------------------------------------------

def choose_home(folder: Path, yes: bool) -> bool:
    from . import setup
    if not (folder / "identity.json").exists():
        say("home", f"{folder} holds no assistant yet; making one")
        argv = [str(folder)] + (["--yes"] if yes else [])
        if setup.main(argv) != 0 or not (folder / "identity.json").exists():
            say("home", "setup did not finish; nothing more done")
            return False
    else:
        setup.point_at(folder)
    who = identity(folder)
    say("home", f"this install runs {who.get('name')} from {folder}")
    return True


# -- 4. memories -------------------------------------------------------------

def _backup_tags(folder: Path, slug: str) -> list:
    pattern = re.compile(r"^" + re.escape(slug) + r"-backup-\d{4}-\d{2}-\d{2}$")
    ok, out, err = run(["gh", "release", "list", "--limit", "200", "--json", "tagName"], cwd=folder, timeout=120)
    if not ok:
        raise RuntimeError(err or "gh could not list the releases")
    tags = [r.get("tagName", "") for r in json.loads(out or "[]")]
    return sorted((t for t in tags if pattern.match(t)), reverse=True)


def _unpack(zpath: Path, folder: Path) -> tuple:
    root = folder.resolve()
    added = kept = 0
    with zipfile.ZipFile(zpath) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError("the backup is damaged at " + bad)
        for info in z.infolist():
            if info.is_dir() or info.filename == BACKUP_MANIFEST:
                continue
            dest = (root / info.filename).resolve()
            if not dest.is_relative_to(root):
                raise RuntimeError("the backup names a path outside the home: " + info.filename)
            if dest.exists():
                kept += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            added += 1
    return added, kept


def integrity(store: Path) -> str:
    conn = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("pragma integrity_check").fetchone()[0]
    finally:
        conn.close()


def restore_memories(folder: Path, tag: str = None) -> bool:
    store = folder / "data" / "store.db"
    if store.exists():
        say("memories", f"a store is already here ({store.stat().st_size // 1_000_000} MB); left as it is")
    else:
        slug = identity(folder).get("slug") or folder.name.lower()
        if not shutil.which("gh"):
            say("memories", "no store and no gh to fetch one; it will start with no memories")
            return False
        try:
            tags = _backup_tags(folder, slug)
        except Exception as exc:
            say("memories", f"could not read the backups: {exc}")
            return False
        if tag and tag not in tags:
            say("memories", f"no backup tagged {tag}; the newest are: {', '.join(tags[:5]) or 'none'}")
            return False
        tag = tag or (tags[0] if tags else None)
        if not tag:
            say("memories", "this home has no backups yet; it starts with no memories")
            return True
        with tempfile.TemporaryDirectory() as tmp:
            ok, _, err = run(["gh", "release", "download", tag, "--pattern", slug + "-backup.zip",
                              "--dir", tmp], cwd=folder, timeout=1800)
            got = Path(tmp) / (slug + "-backup.zip")
            if not ok or not got.exists():
                say("memories", f"could not download {tag}: {err}")
                return False
            added, kept = _unpack(got, folder)
        say("memories", f"unpacked {tag}: {added} files added, {kept} already here and kept")
    if not store.exists():
        say("memories", "the backup held no store.db")
        return False
    verdict = integrity(store)
    conn = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute("select count(*) from rows").fetchone()[0]
    except sqlite3.Error:
        rows = "?"
    finally:
        conn.close()
    say("memories", f"store integrity: {verdict}; {rows} rows")
    return verdict == "ok"


# -- 5. models folder --------------------------------------------------------

def fix_models_folder(folder: Path):
    path = folder / "data" / "models.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    chosen = cfg.get("folder")
    if not chosen:
        return
    anchor = Path(Path(chosen).anchor or ".")
    if anchor.exists():
        say("models", f"models folder {chosen} is on a drive this machine has")
        return
    cfg["folder"] = None
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    say("models", f"{chosen} is on a drive this machine does not have; models go to the usual cache now")


# -- 6. the restore kit ------------------------------------------------------

def put_back_kit(folder: Path):
    kit = folder / "restore"
    if not kit.is_dir():
        return
    added = kept = 0
    for f in sorted(kit.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(kit).as_posix()
        if KIT_SKIP.match(rel):
            continue
        top, _, rest = rel.partition("/")
        place = KIT_PLACES.get(top)
        if not place or not rest:
            continue
        dest = place() / rest
        if dest.exists():
            kept += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        added += 1
    say("restore kit", f"{added} files put back, {kept} already there and kept")


# -- 7. privacy hook ---------------------------------------------------------

def privacy_hook(folder: Path):
    denylist = folder / "privacy" / "denylist.txt"
    if not denylist.exists():
        return
    ok, hooks, _ = run(["git", "rev-parse", "--git-path", "hooks"], cwd=CODE)
    if not ok:
        say("privacy", "the code folder is not a git checkout; no hook")
        return
    hooks_dir = (CODE / hooks).resolve()
    hooks_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CODE / "tools" / "pre-push", hooks_dir / "pre-push")
    run(["git", "config", "privacy.denylist", denylist.as_posix()], cwd=CODE)
    say("privacy", "pre-push hook installed against " + denylist.as_posix())


# -- 8. Codex ----------------------------------------------------------------

def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _drop_tables(text: str, key: str) -> str:
    """Remove [mcp_servers.<key>] and its sub-tables, leaving everything else."""
    head = re.compile(r"^\s*\[mcp_servers\." + re.escape(key) + r"(\.[^\]]*)?\]\s*$")
    out, skipping = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            skipping = bool(head.match(line))
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


def _same_root(command: str, folder: Path) -> bool:
    flat = command.replace("\\\\", "\\").replace("/", "\\").lower()
    return str(folder.resolve()).replace("/", "\\").lower() in flat


def register_codex(folder: Path) -> bool:
    ch = codex_home()
    if not ch.exists():
        say("codex", f"Codex has no folder yet ({ch}); open Codex and sign in once, then run "
                     f"`python -m server.install {folder} --codex-only`")
        return False
    who = identity(folder)
    slug, name = who.get("slug") or "assistant", who.get("name") or "the assistant"
    key = slug.replace("-", "_") + "_memory"
    py = console_python()
    home_s = str(folder.resolve())

    config = ch / "config.toml"
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if text:
        shutil.copy2(config, config.with_name("config.toml.before-install"))
    block = (f"[mcp_servers.{key}]\n"
             f"command = {_toml_str(py)}\n"
             f"args = [\"-B\", {_toml_str(str(PLUG))}, \"--root\", {_toml_str(home_s)}]\n")
    text = _drop_tables(text, key) if text else ""
    config.write_text((text + "\n" if text.strip() else "") + block, encoding="utf-8")

    hooks_path = ch / "hooks.json"
    try:
        hooks = json.loads(hooks_path.read_text(encoding="utf-8")) if hooks_path.exists() else {}
    except ValueError:
        say("codex", f"{hooks_path} does not parse; left alone, no hook written")
        return False
    if hooks_path.exists():
        shutil.copy2(hooks_path, hooks_path.with_name("hooks.json.before-install"))
    starts = hooks.setdefault("hooks", {}).setdefault("SessionStart", [])
    allow = []
    kept = []
    for entry in starts:
        cmds = [h.get("command", "") for h in entry.get("hooks", []) if isinstance(h, dict)]
        ours = any((LEGACY_PLUG in c or "codex_mcp.py" in c) and _same_root(c, folder) for c in cmds)
        if ours:
            for c in cmds:
                allow += re.findall(r"--allow-task\s+(\S+)", c)
        else:
            kept.append(entry)
    command = subprocess.list2cmdline([py, "-B", str(PLUG), "--root", home_s, "--hook"])
    kept.append({"matcher": "startup|resume|clear|compact",
                 "hooks": [{"type": "command", "command": command, "timeout": 10,
                            "additionalContextLimit": 6500,
                            "statusMessage": f"Loading {name}'s saved context"}]})
    hooks["hooks"]["SessionStart"] = kept
    hooks_path.write_text(json.dumps(hooks, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if allow:
        # Tasks the old hand-made hook let in now live in the home, where
        # every later install finds them.
        cpath = folder / "data" / "codex.json"
        try:
            cfg = json.loads(cpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        cfg["allow_tasks"] = sorted(set(cfg.get("allow_tasks") or []) | set(allow))
        cpath.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    probe = _probe_plug(py, home_s)
    say("codex", f"registered {key} and its SessionStart hook in {ch}; plug answers: {probe}")
    return probe == "ok"


def _probe_plug(py: str, home_s: str) -> str:
    """Ask the plug what tools it has, the way Codex will."""
    try:
        p = subprocess.run([py, "-B", str(PLUG), "--root", home_s],
                           input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
                           capture_output=True, text=True, encoding="utf-8", timeout=60,
                           creationflags=NO_WINDOW)
        got = json.loads((p.stdout or "").strip().splitlines()[0])
        n = len(got["result"]["tools"])
        return "ok" if n else "no tools"
    except Exception as exc:
        return f"no answer ({exc.__class__.__name__})"


# -- 9. shortcut and start ---------------------------------------------------

def shortcut():
    if not WIN:
        return
    ok, _, err = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                      str(CODE / "make_shortcut.ps1"), "-Startup"], cwd=CODE, timeout=120)
    say("shortcut", "Desktop shortcut and start-at-login made" if ok else "shortcut failed: " + err[-300:])


def manager_up(port=8787) -> bool:
    import urllib.request
    import urllib.error
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3)
        return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def start():
    if manager_up():
        say("start", "the manager is already running; restart it from its icon to pick up a new home")
        return
    exe = Path(console_python())
    pyw = exe.with_name("pythonw.exe") if WIN and exe.with_name("pythonw.exe").exists() else exe
    flags = (0x00000008 | 0x00000200) if WIN else 0   # detached, own process group
    subprocess.Popen([str(pyw), str(CODE / "start.pyw"), "--open"], cwd=CODE,
                     creationflags=flags, close_fds=True)
    say("start", "the manager is starting: http://localhost:8787")


# -- the whole thing ---------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m server.install", description=__doc__.split("\n\n")[0])
    ap.add_argument("home", help="the assistant's home folder (its cloned repo)")
    ap.add_argument("--backup", help="a backup release tag to restore, instead of the newest")
    ap.add_argument("--codex-only", action="store_true", help="only register with Codex")
    ap.add_argument("--no-packages", action="store_true")
    ap.add_argument("--no-codex", action="store_true")
    ap.add_argument("--no-shortcut", action="store_true")
    ap.add_argument("--no-start", action="store_true")
    ap.add_argument("--yes", action="store_true", help="a new assistant takes defaults; ask nothing")
    args = ap.parse_args(argv)
    folder = Path(args.home).expanduser().resolve()
    if not folder.is_dir():
        print(f"{folder} is not a folder. Clone the assistant's repo there first.")
        return 2

    if args.codex_only:
        return 0 if register_codex(folder) else 1

    missing = check_machine()
    if not args.no_packages and not install_packages():
        return 1
    if not choose_home(folder, args.yes):
        return 1
    memories_ok = restore_memories(folder, args.backup)
    fix_models_folder(folder)
    put_back_kit(folder)
    privacy_hook(folder)
    codex_ok = True if args.no_codex else register_codex(folder)
    if not memories_ok:
        say("done", "stopped before starting: the memories did not come back cleanly (see above)")
        return 1
    if not args.no_shortcut:
        shortcut()
    if not args.no_start:
        start()
    name = identity(folder).get("name")
    slug = identity(folder).get("slug")
    still = missing + ([] if codex_ok else ["Codex registration (see above)"])
    say("done", f"{name} is installed: http://localhost:8787/{slug}/"
                + (" -- still to do: " + "; ".join(still) if still else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
