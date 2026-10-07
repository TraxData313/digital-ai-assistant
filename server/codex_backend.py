"""The assistant's JSON turns through the installed Codex app server and ChatGPT login.

No credentials are read by the assistant and an API-key login is never
accepted. Each call is ephemeral: the assistant's own working set remains the
conversation history.
"""
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import home, providers, native_tools, native_proof

LIMIT_TTL = 60
_LIMIT_LOCK = threading.Lock()
_LIMIT_CACHE = {
    "at": 0.0, "limits": None, "credits": None,
    "error": None, "asking": False,
}


def executable():
    override = os.environ.get("ASSISTANT_CODEX_EXE")
    if override:
        return override
    found = shutil.which("codex")
    # A .cmd/.bat wrapper on PATH runs inside the native turn's bare
    # environment (no LOCALAPPDATA, no PSModulePath) and can fail to find the
    # real binary, so a desktop install's own codex.exe is preferred to it.
    if found and Path(found).suffix.lower() not in (".cmd", ".bat"):
        return found
    # Desktop installs need not be on the tray process's older PATH.
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
    choices = list(root.glob("*/codex.exe"))
    if choices:
        return str(max(choices, key=lambda p: p.stat().st_mtime))
    if found:
        return found
    raise providers.TurnBroke("Codex is not installed. Install Codex and run codex login first.")


class Client:
    def __init__(self, cwd, timeout=600, native=False):
        self.deadline = time.monotonic() + timeout
        self.events = queue.Queue()
        self.counter = 0
        self.native = native
        env = dict(os.environ)
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            env.pop(name, None)
        # The desktop app's own config can carry MCP servers and plugins that
        # have no place in the assistant's model-only backend (and may not even have a
        # transport the standalone app server understands). Give this process
        # an empty Codex home and carry only the ChatGPT authentication into it.
        args = []
        if self.native:
            profile = native_proof if native_tools.PROOF.get() else native_tools
            args, env = profile.launch_options(Path(cwd))
        else:
            isolated_home = Path(cwd) / ".codex"
            isolated_home.mkdir()
            auth = Path.home() / ".codex" / "auth.json"
            if auth.is_file():
                shutil.copyfile(auth, isolated_home / "auth.json")
            env["CODEX_HOME"] = str(isolated_home)
            env["CODEX_SQLITE_HOME"] = str(isolated_home)
        self.proc = subprocess.Popen(
            [executable(), "app-server", "--stdio", *args], cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    self.events.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self.events.put(None)

    def send(self, message):
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def next(self):
        try:
            left = max(0, self.deadline - time.monotonic())
            message = self.events.get(timeout=min(left, .2) if self.native else left)
        except queue.Empty:
            if self.native and time.monotonic() < self.deadline:
                return {"method": "local/poll", "params": {}}
            raise providers.TurnBroke("Codex timed out before completing the answer.") from None
        if message is None:
            raise providers.TurnBroke("Codex closed before completing the answer.")
        # This client never grants tools, permissions, or credential requests.
        if "method" in message and "id" in message:
            if self.native and message["method"] in (
                    "item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                self.send({"id": message["id"], "result": {"decision": "decline"}})
            else:
                self.send({"id": message["id"], "error": {
                    "code": -32601, "message": "This room does not grant client permissions or tools."}})
        return message

    def request(self, method, params):
        self.counter += 1
        ident = self.counter
        self.send({"id": ident, "method": method, "params": params})
        while True:
            event = self.next()
            if event.get("id") == ident and "method" not in event:
                if event.get("error"):
                    raise providers.TurnBroke("Codex refused " + method + ": " +
                                              str(event["error"].get("message", "unknown error")))
                return event.get("result") or {}

    def initialize(self):
        self.request("initialize", {"clientInfo": {
            "name": "digital_ai_assistant", "version": "1.0.0"},
            "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized", "params": {}})

    def close(self):
        if self.proc.poll() is None:
            if os.name == "nt":
                # Some desktop installations use a launcher process. Stop
                # its app-server child as well, so its pipe cannot stay open.
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.reader.join(timeout=2)
        self.proc.stdin.close()
        if not self.reader.is_alive():
            self.proc.stdout.close()


def require_chatgpt(client):
    account = client.request("account/read", {"refreshToken": False}).get("account") or {}
    if account.get("type") != "chatgpt":
        raise providers.TurnBroke(
            "Codex needs a ChatGPT subscription sign-in. Run codex login on this "
            "computer, then try again. An API key cannot use the subscription; "
            "nothing was sent to a model.")


def _iso(stamp):
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _tidy_limit(window):
    if not isinstance(window, dict) or window.get("usedPercent") is None:
        return None
    minutes = window.get("windowDurationMins")
    if minutes == 300:
        name, label = "five_hour", "5-hour limit"
    elif minutes == 10080:
        name, label = "seven_day", "Weekly"
    elif isinstance(minutes, (int, float)) and minutes > 0:
        name, label = f"{int(minutes)}_minute", f"{int(minutes)}-minute limit"
    else:
        name, label = "subscription", "Subscription limit"
    return {
        "window": name, "label": label,
        "used_fraction": window["usedPercent"] / 100.0,
        "status": None, "severity": None,
        "resets_at": _iso(window.get("resetsAt")), "using_overage": False,
    }


def _tidy_credits(snapshot):
    """The live Codex-credit balance carried beside subscription limits."""
    credits = snapshot.get("credits") if isinstance(snapshot, dict) else None
    if not isinstance(credits, dict):
        return None
    if credits.get("unlimited") is True:
        return {"unlimited": True, "balance": None}
    balance = credits.get("balance")
    if isinstance(balance, bool) or balance is None:
        return None
    try:
        number = float(balance)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0 or number == float("inf") or number != number:
        return None
    # Keep the service's decimal text for an exact hover reading. The room
    # draws a conservative whole-credit figure, as Codex's Usage page does.
    return {"unlimited": False, "balance": str(balance)}


def _fetch_limits():
    try:
        with tempfile.TemporaryDirectory(prefix="assistant-codex-limits-") as cwd:
            client = Client(cwd, 20)
            try:
                client.initialize()
                require_chatgpt(client)
                result = client.request("account/rateLimits/read", {})
            finally:
                client.close()
        all_limits = result.get("rateLimitsByLimitId") or {}
        snapshot = all_limits.get("codex") or result.get("rateLimits") or {}
        rows = [
            _tidy_limit(snapshot.get("primary")),
            _tidy_limit(snapshot.get("secondary")),
        ]
        rows = [row for row in rows if row]
        return (rows or None, _tidy_credits(snapshot),
                None if rows else "Codex named no subscription windows")
    except Exception as exc:
        return None, None, str(exc)[:180]


def _refresh_limits():
    found, credits, error = _fetch_limits()
    with _LIMIT_LOCK:
        _LIMIT_CACHE.update(at=time.time(), error=error, asking=False)
        if found is not None:
            _LIMIT_CACHE.update(limits=found, credits=credits)


def limits_now():
    """Live ChatGPT subscription windows, cached for the room's fast poll."""
    with _LIMIT_LOCK:
        stale = time.time() - _LIMIT_CACHE["at"] >= LIMIT_TTL
        start = stale and not _LIMIT_CACHE["asking"]
        cold = _LIMIT_CACHE["limits"] is None
        if start:
            _LIMIT_CACHE["asking"] = True
    if start:
        if cold:
            _refresh_limits()
        else:
            threading.Thread(target=_refresh_limits, daemon=True).start()
    with _LIMIT_LOCK:
        return {
            "limits": _LIMIT_CACHE["limits"],
            "credits": _LIMIT_CACHE["credits"],
            "error": _LIMIT_CACHE["error"],
        }


def call(spec, system, prompt_json, schema, on_step=None, on_write=None,
         expect="reply", images=None):
    say = on_step or (lambda *a, **k: None)
    wrote = on_write or (lambda *a, **k: None)
    started = time.monotonic()
    raw, usage, context = "", {}, None
    prompt_input, context_input = None, None
    client = None
    native = native_tools.ACTIVE.get() and expect == "reply"
    fixture = native and native_tools.PROOF.get()
    fixture_before = None
    unfinished = {}
    def activity(text, detail):
        say(native_tools.redact(text), "native", native_tools.clean(detail))
    try:
        if fixture:
            fixture_before = native_proof.fixture_text()
        # Windows cannot reopen a writable child beneath the protected Temp root.
        # Native scratch contains no subscription credentials and is per-call.
        with tempfile.TemporaryDirectory(prefix="assistant-codex-", ignore_cleanup_errors=True,
                dir=native_tools.SCRATCH_PARENT if native and not fixture else None) as cwd:
            try:
                client = Client(cwd, providers.CALL_TIMEOUT, True) if native else Client(cwd, providers.CALL_TIMEOUT)
                client.initialize()
                require_chatgpt(client)
                if native:
                    readiness = client.request("windowsSandbox/readiness", {})
                    if readiness.get("status") != "ready":
                        raise providers.TurnBroke("Native Windows sandbox is not ready; no model turn was started.")
                    if not fixture:
                        # Readiness alone does not test this profile's ACL setup.
                        # Surface native setup failures before spending a model turn.
                        activity("Checking native command startup", {"status": "started", "profile": native_tools.PROFILE,
                            "command": "cmd.exe /d /c exit 0", "cwd": str(Path.home())})
                        check = client.request("command/exec", {
                            "command": [str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/cmd.exe"), "/d", "/c", "exit 0"],
                            "cwd": str(Path.home()), "permissionProfile": native_tools.PROFILE, "timeoutMs": 10000})
                        activity("Native command startup result", {"command": "cmd.exe /d /c exit 0",
                            "cwd": str(Path.home()), "exitCode": check.get("exitCode"),
                            "stdout": check.get("stdout", ""), "stderr": check.get("stderr", "")})
                        if check.get("exitCode") != 0:
                            raise providers.TurnBroke("Native command startup failed; no model turn was started.")
                config = {
                    "features.apps": False, "features.plugins": False,
                    "features.browser_use": False, "features.shell_tool": False,
                    "features.multi_agent": False, "features.multi_agent_v2": False,
                    "agents.enabled": False, "features.memories": False,
                    "web_search": "disabled", "project_doc_max_bytes": 0,
                    "skills.include_instructions": False,
                    "orchestrator.mcp.enabled": False,
                    "orchestrator.skills.enabled": False,
                }
                params = {
                    "model": spec["id"], "modelProvider": "openai",
                    "baseInstructions": system, "developerInstructions": "",
                    "ephemeral": True, "cwd": cwd, "environments": [],
                    "selectedCapabilityRoots": [], "dynamicTools": [],
                    "approvalPolicy": "never", "sandbox": "read-only",
                    "config": config, "personality": "none"}
                if native:
                    params.pop("environments")
                    params.pop("sandbox")
                    profile = native_proof if fixture else native_tools
                    params.update(permissions="assistant-proof" if fixture else native_tools.PROFILE,
                                  cwd=str(native_proof.DOCUMENTS if fixture else Path.home()),
                                  baseInstructions=system + profile.instructions())
                    config["features.shell_tool"] = True
                    inherited = client.request("config/read", {"includeLayers": False}).get("config") or {}
                    for name in (inherited.get("mcp_servers") or {}):
                        if not name.replace("_", "").replace("-", "").isalnum():
                            raise providers.TurnBroke("Cannot safely disable an inherited MCP server with a non-simple configuration name; no model turn started.")
                        config["mcp_servers." + name + ".enabled"] = False
                thread = client.request("thread/start", params)
                thread_id = thread["thread"]["id"]
                content = [{"type": "text", "text": prompt_json}]
                for img in providers._images_for_openai(images):
                    content.append({"type": "image", "url": img["image_url"]["url"]})
                say(home.NAME + " is thinking through Codex on the subscription")
                # Read the start response in the event loop: deltas can arrive
                # before it, and must not be discarded while waiting for an id.
                client.counter += 1
                start_id = client.counter
                client.send({"id": start_id, "method": "turn/start", "params": {
                    "threadId": thread_id, "input": content,
                    "outputSchema": providers.strict_schema(schema)}})
                turn_id, interrupted = None, False
                interrupted_at = None
                messages = {}
                while True:
                    event = client.next()
                    if event.get("id") == start_id:
                        turn_id = ((event.get("result") or {}).get("turn") or {}).get("id", turn_id)
                    if event.get("id") == start_id and event.get("error"):
                        raise providers.TurnBroke(str(event["error"].get("message")))
                    method, params = event.get("method"), event.get("params") or {}
                    if params.get("threadId") not in (None, thread_id):
                        continue
                    if method == "turn/started":
                        turn_id = (params.get("turn") or {}).get("id", turn_id)
                    if native and native_tools.CANCEL.is_set() and not interrupted and turn_id:
                        interrupted = True
                        interrupted_at = time.monotonic()
                        client.counter += 1
                        client.send({"id": client.counter, "method": "turn/interrupt", "params": {
                            "threadId": thread_id, "turnId": turn_id}})
                        activity("Cancellation requested", {"status": "cancelling"})
                    if interrupted_at and time.monotonic() - interrupted_at > 5:
                        raise providers.TurnBroke("Cancellation did not settle in 5 seconds; stopping this proof process. Command exit status is unknown.")
                    if native and "id" in event and method:
                        activity("Native request refused", {"method": method, "status": "denied"})
                    if native and method in ("item/started", "item/completed"):
                        item = params.get("item") or {}
                        if item.get("type") in ("commandExecution", "fileChange"):
                            if method == "item/started":
                                unfinished[item.get("id")] = item
                            else:
                                unfinished.pop(item.get("id"), None)
                            activity(item["type"] + ": " + item.get("status", method), item)
                        elif item.get("type") == "agentMessage":
                            ident = item.get("id")
                            previous = messages.get(ident, {})
                            messages[ident] = {**previous, **item}
                            if method == "item/completed":
                                if item.get("phase") == "final_answer":
                                    raw = native_tools.redact(item.get("text", previous.get("text", "")))
                                    wrote({"mode": "writing", "thought_chars": 0,
                                           "answer_chars": len(raw), "reply": providers._peek(raw)})
                                else:
                                    activity("Codex commentary", {"phase": item.get("phase"), "text": item.get("text", "")})
                        continue
                    if native and method in ("item/commandExecution/outputDelta", "item/fileChange/outputDelta", "turn/diff/updated"):
                        activity("Native output" if "outputDelta" in method else "Turn diff", params)
                        continue
                    if method == "item/agentMessage/delta":
                        if native:
                            message = messages.setdefault(params.get("itemId"), {})
                            message["text"] = message.get("text", "") + params.get("delta", "")
                            continue
                        raw += params.get("delta", "")
                        wrote({"mode": "writing", "thought_chars": 0,
                               "answer_chars": len(raw), "reply": providers._peek(raw)})
                    elif method == "item/completed":
                        item = params.get("item") or {}
                        if item.get("type") == "agentMessage":
                            raw = item.get("text", raw)
                    elif method == "thread/tokenUsage/updated":
                        measured = params.get("tokenUsage") or {}
                        usage = measured.get("total") or {}
                        last = measured.get("last") or {}
                        last_input = last.get("inputTokens")
                        if isinstance(last_input, int) and not isinstance(last_input, bool) and last_input > 0:
                            context_input = last_input
                            # Each adapter call starts a fresh thread. Only the
                            # first inference measures the assembled room prompt;
                            # later ones include tool output and repeated input.
                            if prompt_input is None and usage.get("inputTokens") == last_input:
                                prompt_input = last_input
                        context = measured.get("modelContextWindow")
                    elif method == "turn/completed":
                        turn = params.get("turn") or {}
                        if native:
                            activity("Native turn " + str(turn.get("status")), {"status": turn.get("status"), "error": turn.get("error")})
                        if turn.get("status") != "completed":
                            error = turn.get("error") or {}
                            raise providers.TurnBroke("Codex did not finish: " +
                                str(error.get("message") or turn.get("status")))
                        break
            finally:
                if client is not None:
                    client.close()
                if native:
                    for item in unfinished.values():
                        activity("Native activity ended without a completion event", {
                            **item, "status": "unknown", "exitCode": None,
                            "note": "Native process stopped; no final result was received for this item."})
                if native and fixture_before is not None:
                    try:
                        diff = native_proof.fixture_diff(fixture_before)
                        if diff:
                            activity("Fixture diff measured after native tools", {"path": str(native_proof.DOCUMENTS / "Assistant Native Proof/fixture.txt"), "diff": diff})
                    except (OSError, ValueError) as exc:
                        activity("Fixture diff unavailable", {"status": "failed", "error": str(exc)})
        payload = json.loads(raw)
        if not isinstance(payload, dict) or expect not in payload:
            raise providers.TurnBroke("Codex returned an answer without `" + expect + "`.")
        payload = providers.denull(payload, schema)
        if fixture:
            native_proof.validate_answer(payload)
        incoming, cached = usage.get("inputTokens"), usage.get("cachedInputTokens")
        payload["_meta"] = {
            "model": spec["id"], "model_key": spec["key"], "service": "codex",
            "billing": "subscription", "cost_usd": None, "limits": [],
            "context_max_tokens": context, "input_tokens": incoming,
            "prompt_input_tokens": prompt_input,
            "last_model_input_tokens": context_input,
            "input_tokens_scope": "cumulative_thread",
            "cache_read_tokens": cached, "cache_write_tokens": usage.get("cacheWriteInputTokens"),
            "fresh_input_tokens": max(0, incoming - (cached or 0)) if incoming is not None else None,
            "output_tokens": usage.get("outputTokens"),
            "thinking_tokens": usage.get("reasoningOutputTokens"),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        say("the answer came back whole")
        return payload
    except Exception as exc:
        if native:
            activity("Native turn did not produce a valid answer", {"status": "failed", "error": str(exc)})
        raise providers.TurnBroke("Codex: " + str(exc), raw=raw,
                                  reply=providers._peek(raw)) from exc
