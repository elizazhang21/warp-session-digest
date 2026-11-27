#!/usr/bin/env python3
"""Cross-platform Claude Code hook runner for Warp notifications + session-digest labels.

Replaces the bash + jq hook scripts so hooks work identically on macOS, Linux, and Windows
without requiring jq or shell file associations.
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time

PLUGIN_CURRENT_PROTOCOL_VERSION = 1
TERMINAL_SEQUENCE_MIN_VERSION = (2, 1, 141)
LAST_BROKEN = {
    "dev": "",
    "stable": "v0.2026.03.25.08.24.stable_05",
    "preview": "v0.2026.03.25.08.24.preview_05",
}
NOISE_RE = re.compile(
    r"^\s*(<task-notification|<system-reminder|<local-command|<bash-input|<bash-stdout|<bash-stderr"
    r"|Caveat: The messages below|\[Request interrupted|This session is being continued)"
)
STATE_FILE = os.environ.get("WARP_DIGEST_STATE") or os.path.join(
    os.path.expanduser("~"), ".cache", "warp-session-digest", "titles.json"
)


def should_use_structured():
    if not os.environ.get("WARP_CLI_AGENT_PROTOCOL_VERSION"):
        return False
    client_ver = os.environ.get("WARP_CLIENT_VERSION", "")
    if not client_ver:
        return False
    threshold = ""
    for chan, thr in LAST_BROKEN.items():
        if chan in client_ver:
            threshold = thr
            break
    if threshold and not (client_ver > threshold):
        return False
    return True


def parse_version(raw):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw or "")
    return tuple(int(x) for x in m.groups()) if m else None


def emit_terminal_sequence(seq):
    if not seq:
        return
    ver = parse_version(os.environ.get("CLAUDE_CODE_VERSION", ""))
    if ver is not None:
        if ver >= TERMINAL_SEQUENCE_MIN_VERSION:
            print(json.dumps({"terminalSequence": seq}, ensure_ascii=False))
        elif sys.platform != "win32":
            try:
                with open("/dev/tty", "w", encoding="utf-8") as tty:
                    tty.write(seq)
            except OSError:
                pass
        return
    if sys.platform != "win32":
        try:
            with open("/dev/tty", "w", encoding="utf-8") as tty:
                tty.write(seq)
            return
        except OSError:
            pass
    print(json.dumps({"terminalSequence": seq}, ensure_ascii=False))


def warp_notify(title, body):
    if not should_use_structured():
        return
    emit_terminal_sequence(f"\033]777;notify;{title};{body}\007")


def negotiate_protocol_version():
    try:
        warp_v = int(os.environ.get("WARP_CLI_AGENT_PROTOCOL_VERSION") or PLUGIN_CURRENT_PROTOCOL_VERSION)
    except ValueError:
        warp_v = PLUGIN_CURRENT_PROTOCOL_VERSION
    return min(warp_v, PLUGIN_CURRENT_PROTOCOL_VERSION)


def build_payload(data, event, **extra):
    cwd = data.get("cwd") or ""
    payload = {
        "v": negotiate_protocol_version(),
        "agent": "claude",
        "event": event,
        "session_id": data.get("session_id") or "",
        "cwd": cwd,
        "project": os.path.basename(os.path.normpath(cwd)) if cwd else "",
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def truncate(s, limit):
    if s and len(s) > limit:
        return s[: limit - 3] + "..."
    return s or ""


def readable_prompt(text):
    if not text:
        return ""
    m = re.search(r"<command-name>([^<]*)</command-name>", text)
    if not m:
        return text
    args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
    return f"{m.group(1)} {args.group(1) if args else ''}".rstrip()


def is_noise_prompt(text):
    return bool(NOISE_RE.search(text or ""))


def parse_epoch(ts):
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    try:
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def extract_prompt_text(content):
    texts = [content] if isinstance(content, str) else [
        b.get("text") or "" for b in (content or []) if isinstance(b, dict) and b.get("type") == "text"
    ]
    out = []
    for t in texts:
        if not t or NOISE_RE.search(t):
            continue
        if "<command-name>" in t and "<command-message>" not in t:
            continue
        out.append(readable_prompt(t))
    return " ".join(x for x in out if x)


def transcript_facts(path):
    ts_val, text, response, local = None, "", "", False
    if not path or not os.path.isfile(path):
        return ts_val, text, response
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict):
                    continue
                typ = e.get("type")
                msg_content = (e.get("message") or {}).get("content")
                if typ == "user" and e.get("isMeta"):
                    raw = msg_content if isinstance(msg_content, str) else json.dumps(msg_content or "")
                    if re.search(r"^\s*<local-command-caveat", raw):
                        local = True
                elif typ == "user" and not e.get("isMeta") and not e.get("isSidechain") and not e.get("isCompactSummary"):
                    t = extract_prompt_text(msg_content)
                    raw = msg_content if isinstance(msg_content, str) else json.dumps(msg_content or "")
                    is_cmd = "<command-name>" in raw
                    if local and is_cmd:
                        local = False
                    elif t:
                        text = t
                        ts_val = parse_epoch(e.get("timestamp"))
                        local = False
                    else:
                        local = False
                elif typ == "attachment":
                    att = e.get("attachment") or {}
                    if att.get("type") == "queued_command" and att.get("commandMode") == "prompt":
                        t = extract_prompt_text(att.get("prompt"))
                        if t:
                            text = t
                            ts_val = parse_epoch(att.get("timestamp") or e.get("timestamp"))
                elif typ == "assistant" and isinstance(msg_content, list):
                    r = " ".join(b.get("text") or "" for b in msg_content if isinstance(b, dict) and b.get("type") == "text")
                    if r:
                        response = r
    except OSError:
        pass
    return ts_val, text, response


def cached_summary(session_id, newer_than=0.0):
    if not session_id or not os.path.isfile(STATE_FILE):
        return ""
    try:
        with open(STATE_FILE, encoding="utf-8", errors="replace") as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            entry = state.get(f"cc:{session_id}")
            if isinstance(entry, dict) and (entry.get("at") or 0) > (newer_than or 0):
                return entry.get("title") or ""
    except (OSError, ValueError):
        pass
    return ""


def detect_cc_version():
    if os.environ.get("CLAUDE_CODE_VERSION"):
        return
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    claude = shutil.which("claude")
    if not env_file or not claude:
        return
    try:
        kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
        r = subprocess.run([claude, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5, **kw)
        ver = (r.stdout.splitlines() or [""])[0].strip()
        if ver:
            with open(env_file, "a", encoding="utf-8") as fh:
                fh.write(f'export CLAUDE_CODE_VERSION="{ver}"\n')
            os.environ["CLAUDE_CODE_VERSION"] = ver
    except (OSError, subprocess.SubprocessError):
        pass


def handle_session_start(data):
    if not should_use_structured():
        return
    detect_cc_version()
    plugin_json = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".claude-plugin", "plugin.json")
    try:
        with open(plugin_json, encoding="utf-8") as fh:
            plugin_version = json.load(fh).get("version") or "unknown"
    except (OSError, ValueError):
        plugin_version = "unknown"
    body = build_payload(data, "session_start", plugin_version=plugin_version)
    warp_notify("warp://cli-agent", body)


def handle_stop(data):
    if not should_use_structured() or data.get("stop_hook_active") is True:
        return
    transcript_path = data.get("transcript_path") or ""
    time.sleep(0.3)
    query, response = "", ""
    if transcript_path and os.path.isfile(transcript_path):
        last_ts, query, response = transcript_facts(transcript_path)
        query = truncate(query, 200)
        response = truncate(response, 200)
        session_id = data.get("session_id") or ""
        if last_ts is not None:
            summary = cached_summary(session_id, last_ts)
            if summary:
                query = summary
    bg_count = len(data.get("background_tasks") or [])
    cron_count = len(data.get("session_crons") or [])
    body = build_payload(
        data,
        "stop",
        query=query,
        response=response,
        transcript_path=transcript_path,
        background_task_count=bg_count,
        session_cron_count=cron_count,
    )
    warp_notify("warp://cli-agent", body)


def handle_notification(data):
    if not should_use_structured():
        return
    notif_type = data.get("notification_type") or "unknown"
    msg = truncate(data.get("message") or "Input needed", 120)
    body = build_payload(data, notif_type, summary=msg)
    warp_notify("warp://cli-agent", body)


def handle_permission_request(data):
    if not should_use_structured():
        return
    tool_name = data.get("tool_name") or "unknown"
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    preview = tool_input.get("command") or tool_input.get("file_path") or json.dumps(tool_input, ensure_ascii=False)[:80]
    summary = f"Wants to run {tool_name}"
    if preview:
        summary = f"{summary}: {truncate(str(preview), 120)}"
    body = build_payload(data, "permission_request", summary=summary, tool_name=tool_name, tool_input=tool_input)
    warp_notify("warp://cli-agent", body)


def handle_prompt_submit(data):
    if not should_use_structured():
        return
    query = data.get("prompt") or ""
    if is_noise_prompt(query):
        session_id = data.get("session_id") or ""
        transcript_path = data.get("transcript_path") or ""
        last_ts, last_text, _ = transcript_facts(transcript_path)
        label = cached_summary(session_id, last_ts) if last_ts is not None else ""
        if not label:
            label = last_text
        query = label or readable_prompt(query)
    else:
        query = readable_prompt(query)
    query = truncate(query, 200)
    body = build_payload(data, "prompt_submit", query=query)
    warp_notify("warp://cli-agent", body)


def handle_post_tool_use(data):
    if not should_use_structured():
        return
    tool_name = data.get("tool_name") or ""
    body = build_payload(data, "tool_complete", tool_name=tool_name)
    warp_notify("warp://cli-agent", body)


def handle_stop_failure(data):
    if not should_use_structured():
        return
    error_type = data.get("error") or ""
    error_message = data.get("last_assistant_message") or ""
    transcript_path = data.get("transcript_path") or ""
    _, query, _ = transcript_facts(transcript_path)
    query = truncate(query, 200)
    body = build_payload(
        data,
        "stop_failure",
        query=query,
        response=error_message,
        error_type=error_type,
        transcript_path=transcript_path,
    )
    warp_notify("warp://cli-agent", body)


HANDLERS = {
    "session-start": handle_session_start,
    "stop": handle_stop,
    "notification": handle_notification,
    "permission-request": handle_permission_request,
    "prompt-submit": handle_prompt_submit,
    "post-tool-use": handle_post_tool_use,
    "stop-failure": handle_stop_failure,
}


def main():
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2 or sys.argv[1] not in HANDLERS:
        return
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    HANDLERS[sys.argv[1]](data)


if __name__ == "__main__":
    main()
