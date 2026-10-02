"""Foreground native Codex tools, command environment and durable activity."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
from . import home

ROOT = Path(__file__).resolve().parent.parent
PROFILE = "assistant-chat"
ACCOUNT_HOME = Path.home()
SCRATCH_PARENT = ACCOUNT_HOME
ACTIVE = ContextVar("native_tools", default=False)
PROOF = ContextVar("fixture_diagnostic", default=False)
RUNNING = threading.Event()
CANCEL = threading.Event()
LOGS = home.LOGS / "native"

def protected():
    user = Path.home()
    return [user / ".codex", user / ".claude", user / ".ssh", user / ".aws",
            Path(os.environ.get("TEMP", str(user / "AppData/Local/Temp"))),
            ROOT / "data", ROOT / "data_backups", ROOT / "logs", ROOT / ".git",
            home.DATA, home.BACKUPS, home.LOGS, home.HOME / ".git",
            home.CODE / "home.json",
            ROOT / ".codex", ROOT / ".claude", ROOT / "server/passwords.py"]


def private_path(path):
    p = Path(path).resolve()
    return (any(p == x.resolve() or x.resolve() in p.parents for x in protected())
            or p.name.lower().startswith(".env") or p.suffix.lower() in (".token", ".pem", ".key"))


def redact(text):
    # Never print authorization headers or pairing/angel query credentials.
    text = re.sub(r"([?&][^=&\s\"']+=)[^&\s\"']+", r"\1[redacted]", str(text))
    text = re.sub(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)[^\s\"']+",
                  r"\1[redacted]", text)
    return re.sub(r"(?i)(cookie\s*[:=]\s*)[^\r\n]+", r"\1[redacted]", text)


def clean(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if k.lower() in ("authorization", "cookie", "access_token", "refresh_token", "id_token", "api_key") else clean(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return redact(value) if isinstance(value, str) else value


@contextmanager
def activated(proof=False):
    token = ACTIVE.set(True)
    proof_token = PROOF.set(proof)
    RUNNING.set()
    try:
        yield
    finally:
        RUNNING.clear()
        PROOF.reset(proof_token)
        ACTIVE.reset(token)


def chat_allowed(request, model, woken=None):
    from . import providers
    return bool(woken is None and request and request.get("kind") == "user"
                and providers.native_tools_enabled()
                and providers.resolve(model)["service"] == "codex")


def log_activity(row, text, detail=None):
    """Append a redacted, independently readable per-turn troubleshooting log."""
    LOGS.mkdir(parents=True, exist_ok=True)
    entry = clean({"at": datetime.now(timezone.utc).isoformat(), "request_row": int(row),
                   "summary": text, "detail": detail})
    with (LOGS / ("turn-" + str(int(row)) + ".jsonl")).open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry, ensure_ascii=False) + "\n")
        out.flush()
        os.fsync(out.fileno())


def configuration(scratch):
    """Native profile + command environment. Trusted auth remains in its own home."""
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    shell_env = {"SystemRoot": str(windows), "WINDIR": str(windows),
                 "COMSPEC": str(windows / "System32/cmd.exe"),
                 "PATH": str(windows / "System32") + os.pathsep + str(windows / "System32/WindowsPowerShell/v1.0"),
                 "TEMP": str(scratch), "TMP": str(scratch),
                 "HOME": str(scratch), "USERPROFILE": str(scratch)}
    # The owner grants the room account's home tree, including Desktop, Downloads,
    # Documents and Pictures, with explicit authentication/integrity exclusions.
    # Keep trusted adapter code read-only; a command must not rewrite its auth code.
    # Elevated Windows sandboxing requires a root-read baseline. Keep writes
    # scoped below and retain the explicit denies for private/trusted paths.
    fs = {":root": "read", ":minimal": "read", str(ACCOUNT_HOME): "write",
          str(ROOT): "read", str(scratch): "write"}
    fs.update({str(p): "deny" for p in protected()})
    return {
        "default_permissions": PROFILE, "approval_policy": "never",
        "windows": {"sandbox": "elevated"},
        "permissions": {PROFILE: {"filesystem": fs, "network": {"enabled": True}}},
        "shell_environment_policy": {"inherit": "none", "set": shell_env},
        "mcp_servers": {},
        "features": {"apps": False, "plugins": False, "browser_use": False,
                     "multi_agent": False, "multi_agent_v2": False, "memories": False,
                     "hooks": False,
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


def instructions():
    return ("\nNative Codex filesystem and PowerShell/CMD tools are available for this human conversation. "
            "Use them when needed; ordinary conversation and room memory keep the existing final JSON protocol. "
            + home.OWNER_NAME + " deliberately grants the room account's home folder, including Desktop, Downloads, "
            "Documents and Pictures, plus networking through its Codex permission profile. The old fixture "
            "is not a boundary. Actual Windows "
            "and platform denials still apply; approval escalation is unavailable. "
            "Authentication locations, trusted temporary data and the running adapter are protected. "
            "Use the existing room protocol for Spark, memory and attribution. "
            "Commands, working directories, output, exit codes, failures and native patch diffs are saved "
            "and shown in the room. Prefer native patches for visible edit diffs. "
            "PowerShell may use constrained language; ordinary cmdlets remain available. "
            "No Claude calls when Codex-only mode is on, shell-launched background workers or automatic restart. "
            "Use the room's codex JSON operations for authorized visible desktop tasks, not shell commands. "
            "Report actual denials and failures without bypassing them.\n")
