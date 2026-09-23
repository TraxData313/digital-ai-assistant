"""Native subscription voice transport. No API key or default-voice fallback."""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from . import home


class ProbeError(RuntimeError):
    pass


def credits_available(credits):
    """Whether Codex reports credit headroom after included limits are spent."""
    if not isinstance(credits, dict):
        return False
    if credits.get("unlimited") is True:
        return True
    if credits.get("hasCredits") is False:
        return False
    try:
        balance = Decimal(str(credits.get("balance")))
        return balance.is_finite() and balance > 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def executable():
    installed = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
    choices = list(installed.glob("*/codex.exe"))
    if choices:
        return str(max(choices, key=lambda p: p.stat().st_mtime))
    found = shutil.which("codex")
    if found:
        return found
    raise ProbeError("The installed Codex executable was not found.")


def launch_environment():
    env = dict(os.environ)
    for key in list(env):
        if key.upper().startswith(("OPENAI_", "AZURE_OPENAI_")) or key.upper() in {
            "CODEX_API_KEY", "CODEX_HOME", "CODEX_SQLITE_HOME", "CODEX_CONFIG",
        }:
            env.pop(key)
    return env


class NativeClient:
    def __init__(self, cwd):
        self.counter = 0
        self.inbox = queue.Queue()
        self.events = []
        args = [executable(), "app-server", "--stdio"]
        # Process-scoped only. Codex owns credentials in its normal home.
        config = {
            "forced_login_method": "chatgpt", "model_provider": "openai",
            "features.apps": False, "features.plugins": False,
            "features.browser_use": False, "features.shell_tool": False,
            "features.multi_agent": False, "features.multi_agent_v2": False,
            "agents.enabled": False, "features.memories": False,
            "web_search": "disabled", "project_doc_max_bytes": 0,
            "skills.include_instructions": False,
            "orchestrator.mcp.enabled": False, "orchestrator.skills.enabled": False,
        }
        for key, value in config.items():
            args.extend(["-c", key + "=" + json.dumps(value)])
        self.proc = subprocess.Popen(
            args, cwd=cwd, env=launch_environment(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    self.inbox.put(json.loads(line))
                except json.JSONDecodeError:
                    pass
        finally:
            self.inbox.put(None)

    def send(self, message):
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def next(self, timeout):
        try:
            event = self.inbox.get(timeout=max(0, timeout))
        except queue.Empty:
            return None
        if event is None:
            raise ProbeError("Native Codex app-server closed.")
        if "method" in event and "id" in event:
            self.send({"id": event["id"], "error": {
                "code": -32601, "message": "Voice transport grants no tools or credentials."}})
        return event

    def request(self, method, params, timeout=20):
        self.counter += 1
        ident = self.counter
        self.send({"id": ident, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.next(deadline - time.monotonic())
            if event is None:
                break
            if event.get("id") == ident and "method" not in event:
                if event.get("error"):
                    raise ProbeError(method + ": " + event["error"].get("message", "refused"))
                return event.get("result") or {}
            self.events.append(event)
        raise ProbeError(method + " timed out.")

    def initialize(self):
        self.request("initialize", {"clientInfo": {
            "name": "digital_ai_assistant_voice", "version": "1.0.0"},
            "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized", "params": {}})

    def close(self):
        # This is only the process launched above, never the desktop or the room.
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        self.reader.join(timeout=2)


def preflight(client):
    account = client.request("account/read", {"refreshToken": False}).get("account") or {}
    if account.get("type") != "chatgpt" or account.get("planType") not in {"pro", "prolite"}:
        raise ProbeError("Voice requires a ChatGPT Pro sign-in; no model was started. " + json.dumps({k: account.get(k) for k in ("type", "planType")}))
    limits = client.request("account/rateLimits/read", {})
    buckets = limits.get("rateLimitsByLimitId") or {}
    current = buckets.get("codex") or limits.get("rateLimits") or {}
    windows = [current[k] for k in ("primary", "secondary") if current.get(k) is not None]
    included_headroom = bool(windows) and not current.get("rateLimitReachedType") and all(
        isinstance(w, dict) and not isinstance(w.get("usedPercent"), bool)
        and isinstance(w.get("usedPercent"), (float, int))
        and 0 <= w["usedPercent"] < 99 for w in windows)
    credits = current.get("credits")
    credit_headroom = credits_available(credits)
    if current.get("spendControlReached") or not (included_headroom or credit_headroom):
        raise ProbeError("Neither included subscription headroom nor Codex credits are available; no voice started. " + json.dumps({
            "windows": windows, "credits": credits,
            "spendControlReached": current.get("spendControlReached"),
            "rateLimitReachedType": current.get("rateLimitReachedType"),
            "bucketNames": list(buckets), "limitKeys": list(current)}))
    config = client.request("config/read", {"includeLayers": False}).get("config") or {}
    # Inspect only safe configuration metadata; never log the full config.
    suspicious = [k for k, v in config.items() if v is not None and
                  any(s in k.lower() for s in ("base_url", "realtime", "voice"))]
    providers = config.get("model_providers") or {}
    provider = providers.get("openai") or {}
    if provider.get("base_url") or provider.get("env_key") or provider.get("experimental_bearer_token"):
        raise ProbeError("Custom OpenAI provider configuration prevents proving the native route.")
    for key in suspicious:
        if "url" in key.lower() and config[key] and not (key == "chatgpt_base_url" and urlparse(str(config[key])).hostname == "chatgpt.com" and urlparse(str(config[key])).scheme == "https"):
            raise ProbeError("A custom service URL prevents proving the native route: " + key)
    client.thread_config = {}
    for name in (config.get("mcp_servers") or {}):
        if not name.replace("_", "").replace("-", "").isalnum():
            raise ProbeError("Cannot safely disable an inherited MCP server; no voice started.")
        client.thread_config["mcp_servers." + name + ".enabled"] = False
    voices = client.request("thread/realtime/listVoices", {})
    return {
        "authType": account["type"], "planType": account["planType"],
        "modelProvider": config.get("model_provider"),
        "subscriptionWindows": windows,
        "voiceConfigKeys": suspicious, "credits": current.get("credits"),
        "usageSource": "included" if included_headroom else "credits",
        "voices": voices.get("voices"),
        "billingBasis": "Desktop voice uses the Codex usage budget and draws from available credits after included limits. https://learn.chatgpt.com/docs/pricing#how-much-does-voice-cost",
        "modelRequestSentByPreflight": False,
    }



import hashlib

def available_voices(evidence):
    # Native v3's desktop picker uses the v1 voice family. The API v2 family
    # is a separate catalog, not extra choices for this speech connection.
    catalog = evidence.get('voices') or {}
    values = catalog.get('v1') if isinstance(catalog, dict) else None
    if not isinstance(values, list):
        raise ProbeError('The native voice catalog is unavailable.')
    voices = sorted({value for value in values if isinstance(value, str)
                     and 1 <= len(value) <= 64 and value.isascii()
                     and value[0].isalpha() and value.replace('-', '').replace('_', '').isalnum()
                     and value == value.lower()}, key=lambda value: (value != 'sol', value))
    if not voices:
        raise ProbeError('No supported native voices are available.')
    return voices


def select_voice(evidence, requested=None):
    available = available_voices(evidence)
    requested = 'sol' if requested is None else requested
    if not isinstance(requested, str) or requested not in available:
        raise ProbeError('The selected voice is unavailable. Choose a supported voice; no other voice was substituted.')
    return requested


def summarize_offer(sdp):
    lines = sdp.splitlines()
    result = {"bytes": len(sdp.encode()), "sha256": hashlib.sha256(sdp.encode()).hexdigest(),
              "audio": any(x.startswith('m=audio ') for x in lines),
              "dataChannel": any(x.startswith('m=application ') for x in lines),
              "iceCredentials": any(x.startswith('a=ice-ufrag:') for x in lines) and any(x.startswith('a=ice-pwd:') for x in lines),
              "dtlsFingerprint": any(x.startswith('a=fingerprint:') for x in lines),
              "opus": any(' opus/' in x for x in lines), "direction": 'sendrecv' if 'a=sendrecv' in lines else 'other'}
    if not all(result[k] for k in ('audio', 'dataChannel', 'iceCredentials', 'dtlsFingerprint', 'opus')):
        raise ProbeError('Browser did not produce a complete audio/data WebRTC offer.')
    return result

def negotiate(client, sdp, report, prompt=None, voice=None, voices=None):
    report['offer'] = summarize_offer(sdp)
    if not isinstance(voice, str) or voice not in (voices or ()):
        raise ProbeError('Select a voice from the native catalog; no default voice will be used.')
    report['requestedVoice'] = voice
    thread = client.request('thread/start', {'ephemeral': True, 'cwd': str(Path(__file__).resolve().parent),
        'baseInstructions': 'You provide native voice for ' + home.APP_NAME + '. Client-managed handoffs run in the existing room backend.',
        'developerInstructions': '', 'modelProvider': 'openai', 'approvalPolicy': 'never',
        'sandbox': 'read-only', 'environments': [], 'selectedCapabilityRoots': [], 'dynamicTools': [],
        'personality': 'none', 'config': getattr(client, 'thread_config', {})})
    tid = thread['thread']['id']
    report['voiceStartRequested'] = True
    try:
        client.request('thread/realtime/start', {'threadId': tid, 'outputModality': 'audio',
            'version': 'v3', 'voice': voice, 'transport': {'type': 'webrtc', 'sdp': sdp},
            'includeStartupContext': False, 'clientManagedHandoffs': True,
            'prompt': prompt or 'Wait quietly. This is only a connection test. Do not call tools.'})
        report['startRequestAcknowledged'] = True
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            event = client.events.pop(0) if client.events else client.next(.2)
            if not event:
                continue
            method, params = event.get('method', ''), event.get('params') or {}
            if params.get('threadId') != tid:
                continue
            report['events'].append(method)
            if method == 'thread/realtime/sdp':
                answer = params.get('sdp')
                if not answer:
                    raise ProbeError('Native SDP event did not contain an answer.')
                report['answerReceived'] = True
                return tid, answer
            if method == 'thread/realtime/error':
                raise ProbeError(params.get('message', 'Unknown native voice error'))
            if method == 'thread/realtime/closed':
                raise ProbeError('Native voice closed before SDP negotiation completed.')
        raise ProbeError('No SDP answer within the 20-second negotiation window.')
    except Exception:
        stop(client, tid, report)
        raise

def stop(client, tid, report):
    try:
        client.request('thread/realtime/stop', {'threadId': tid}, timeout=5)
        report['stopAcknowledged'] = True
    except ProbeError:
        report['stopAcknowledged'] = False
