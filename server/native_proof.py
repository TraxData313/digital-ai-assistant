"""Legacy opt-in fixture diagnostic; ordinary chat uses native_tools directly."""
import difflib
import json
import os
from pathlib import Path
from . import native_tools
from .native_tools import (ROOT, ACTIVE, PROOF, RUNNING, CANCEL, LOGS,
                           private_path, redact, clean, chat_allowed, log_activity)

DOCUMENTS = Path.home() / "Documents"
ARMED = set()

def protected():
    # Preserve the diagnostic's original profile, including its Temp scratch.
    trusted_temp = Path(os.environ.get("TEMP", str(Path.home() / "AppData/Local/Temp")))
    return [p for p in native_tools.protected() if p != trusted_temp]

def activated(proof=True):
    return native_tools.activated(proof=proof)

def available():
    return os.environ.get("ASSISTANT_NATIVE_PROOF", "") == "1"


def instructions():
    return ("\nThis explicitly requested proof turn has native Codex working tools. "
            "Read and edit the named fixture and run its PowerShell assertion with native tools; "
            "these are separate from the room's read-only files operations. "
            "Report actual results, keep attribution, and finish using the existing final JSON schema. "
            "Documents writes and command networking are deliberately trusted. Documents is not an "
            "exclusive read boundary on this Windows installation. Do not launch other agents, "
            "Claude/Anthropic tools, servers or background processes; do not access credentials, "
            "modify this room's code/data, or restart. Approval escalation is unavailable.\n")


def validate_answer(answer):
    # Same output schema; this proof cannot change room state or page helpers.
    def meaningful(value):
        if isinstance(value, dict):
            return any(meaningful(v) for v in value.values())
        return bool(value)
    for field in ("spark", "budget_target_tokens", "essences", "drop", "claude", "job",
                  "project", "clock", "watch", "restart", "comments", "web"):
        if meaningful(answer.get(field)):
            raise ValueError("Native fixture proof cannot apply room operation: " + field)


def fixture_text():
    path = DOCUMENTS / "Assistant Native Proof" / "fixture.txt"
    # Do not follow a replaced fixture into a trusted/private location.
    if path.is_symlink() or path.resolve() != path.absolute() or private_path(path):
        raise ValueError("Proof fixture was redirected; adapter will not read it.")
    if not path.exists():
        return ""
    if path.stat().st_size > 65536:
        raise ValueError("Proof fixture exceeds 64 KiB; adapter will not copy it into activity.")
    return path.read_text(encoding="utf-8")


def fixture_diff(before):
    return "".join(difflib.unified_diff(before.splitlines(True), fixture_text().splitlines(True),
        fromfile="fixture.txt (before)", tofile="fixture.txt (after)"))


def configuration(scratch):
    """Native profile + command environment. Trusted auth remains in its own home."""
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    shell_env = {"SystemRoot": str(windows), "WINDIR": str(windows),
                 "COMSPEC": str(windows / "System32/cmd.exe"),
                 "PATH": str(windows / "System32") + os.pathsep + str(windows / "System32/WindowsPowerShell/v1.0"),
                 "TEMP": str(scratch), "TMP": str(scratch),
                 "HOME": str(scratch), "USERPROFILE": str(scratch)}
    # Required by the elevated Windows sandbox; narrower denies still apply.
    fs = {":root": "read", ":minimal": "read", str(DOCUMENTS): "write",
          str(scratch): "write", str(ROOT): "read"}
    fs.update({str(p): "deny" for p in protected()})
    # Exact existing paths avoid unbounded glob expansion on the native backend.
    # Room data/auth roots are denied wholesale, including files minted later.
    for base, dirs, names in os.walk(DOCUMENTS):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git")
                   and not private_path(Path(base) / d)]
        for name in names:
            path = Path(base) / name
            if name.lower().startswith(".env") or path.suffix.lower() in (".token", ".pem", ".key"):
                fs[str(path)] = "deny"
    return {
        "default_permissions": "assistant-proof", "approval_policy": "never",
        "windows": {"sandbox": "elevated"},
        "permissions": {"assistant-proof": {"filesystem": fs, "network": {"enabled": True}}},
        "shell_environment_policy": {"inherit": "none", "set": shell_env},
        "mcp_servers": {},
        "features": {"apps": False, "plugins": False, "browser_use": False,
                     "multi_agent": False, "multi_agent_v2": False, "memories": False,
                     "shell_tool": True, "network_proxy": {"enabled": False}},
        "web_search": "disabled", "project_doc_max_bytes": 0,
    }


def launch_options(scratch):
    config = configuration(scratch)
    env = dict(config["shell_environment_policy"]["set"])
    env["CODEX_HOME"] = str(Path.home() / ".codex")
    env["CODEX_SQLITE_HOME"] = str(scratch / "state")
    def toml(value):
        if isinstance(value, dict):
            return "{" + ", ".join(json.dumps(k) + " = " + toml(v) for k, v in value.items()) + "}"
        return json.dumps(value)
    args = []
    for key, value in config.items():
        args.extend(["-c", key + "=" + toml(value)])
    return args, env


def prepare():
    """Only called by the owner's authenticated explicit proof action."""
    folder = DOCUMENTS / "Assistant Native Proof"
    folder.mkdir(exist_ok=True)
    fixture = folder / "fixture.txt"
    if fixture.exists():
        raise ValueError("fixture.txt already exists; keep or move the previous proof before starting another.")
    try:
        with fixture.open("x", encoding="utf-8") as out:
            out.write("value=1\n")
    except FileExistsError:
        raise ValueError("Fixture already exists; previous proof kept.") from None
    return ("Run the explicitly activated native-tool proof on " + str(fixture) +
            ". Read it, change only value=1 to value=2, then run a harmless PowerShell "
            "assertion that reads the actual file and prints FIXTURE_OK if its trimmed contents "
            "equal value=2, otherwise exits nonzero. Report the actual output and exit code. "
            "Keep all room memory and Spark unchanged; no other edits or background processes.")
