#!/usr/bin/env python3
"""Summarize what Claude Code / agy (Antigravity CLI) sessions have been doing.

Sources (all local, read-only):
  Claude Code: ~/.claude/sessions/<pid>.json (live pid -> sessionId), ~/.claude/projects/*/<sessionId>.jsonl
  agy:         ~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl (+ cache/conversation_metadata.json for titles)

Scopes: live = sessions attached to a terminal right now (what Warp's switcher shows), last 24h of each;
today = everything active since 06:00 (previous day before 06:00); <N>h = everything active in the last N hours.
"""
import argparse
import base64
import ctypes
import datetime as dt
import glob
import json
import os
import queue
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

IS_WIN = sys.platform == "win32"
HOME = os.path.expanduser("~")
CC_SESSIONS = os.path.join(HOME, ".claude", "sessions")
CC_PROJECTS = os.path.join(HOME, ".claude", "projects")
AGY = os.path.join(HOME, ".gemini", "antigravity-cli")
AGY_TRANSCRIPT = os.path.join(AGY, "brain", "{}", ".system_generated", "logs", "transcript.jsonl")
AGY_PRINT = re.compile(r"(?:^(\S*[/\\])?agy(\.exe)?\s+acp(\s|$)|\s(-p|--print|--prompt)(=|\s|$))", re.I)  # headless/ACP agy (e.g. our own summarizer), never a terminal session
AGY_WORKDIR = os.path.join(HOME, ".cache", "warp-session-digest", "agy")  # cwd & isolated ACP state parent, so summarizer calls never touch a real workspace or ~/.gemini/antigravity-cli

# Commands worth surfacing in the digest; extend with DIGEST_EXTRA_CMDS (a regex alternation, e.g. "mycli deploy|ship\.sh").
_EXTRA_CMDS = os.environ.get("DIGEST_EXTRA_CMDS", "").strip("|")
try:
    NOTABLE_CMD = re.compile(r"\b(git (commit|push|merge|rebase|worktree add)|gh pr|kubectl|helm|terraform apply|docker push|npm publish|deploy|rsync" + (f"|{_EXTRA_CMDS}" if _EXTRA_CMDS else "") + r")\b")
except re.error as err:
    sys.exit(f"bad DIGEST_EXTRA_CMDS regex: {err}")
EDIT_TOOLS_CC = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
NOISE_PREFIX = ("<local-command", "<task-notification", "<system-reminder", "<bash-input", "<bash-stdout", "<bash-stderr", "Caveat: The messages below", "[Request interrupted", "This session is being continued")
LLM_TIMEOUT_S = 300
SUMMARY_LANG = os.environ.get("DIGEST_LANG", "English")  # language of generated summaries and labels
LLM_CMD = os.environ.get("DIGEST_LLM_CMD", "").strip()  # optional custom summarizer command (reads prompt on stdin, prints reply on stdout), e.g. "claude-x -p"
NO_TTY = ("??", "?", "-")  # macOS / Linux ps spelling of "no controlling terminal"


def now():
    return dt.datetime.now().astimezone()


def parse_ts(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    # normalize fractional seconds to 6 digits so Python < 3.11 fromisoformat accepts them
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    try:
        return dt.datetime.fromisoformat(s).astimezone()
    except ValueError:
        return None


def clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def strip_tags(s):
    for tag in ("system-reminder", "ADDITIONAL_METADATA", "USER_SETTINGS_CHANGE", "LEARN", "PLAN", "SUBAGENT"):
        s = re.sub(rf"<{tag}>.*?</{tag}>", "", s, flags=re.S)
    s = re.sub(r"</?USER_REQUEST>", "", s)
    return s.strip()


def _win_proc_snapshot():
    """Windows: pid -> (ppid, exe_name) via CreateToolhelp32Snapshot."""
    import ctypes.wintypes as w

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", w.DWORD),
            ("cntUsage", w.DWORD),
            ("th32ProcessID", w.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", w.DWORD),
            ("cntThreads", w.DWORD),
            ("th32ParentProcessID", w.DWORD),
            ("pcPriClassBase", w.LONG),
            ("dwFlags", w.DWORD),
            ("szExeFile", w.WCHAR * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snap == w.HANDLE(-1).value:
        return {}
    rows = {}
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                rows[int(pe.th32ProcessID)] = (int(pe.th32ParentProcessID), pe.szExeFile)
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        k32.CloseHandle(snap)
    return rows


def ps_table():
    """pid -> (tty, start datetime or None, command)."""
    if IS_WIN:
        return {pid: (f"pid:{pid}", None, exe) for pid, (_, exe) in _win_proc_snapshot().items()}
    out = subprocess.run(["ps", "-axo", "pid=,tty=,lstart=,command="], capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"}).stdout
    rows = {}
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        pid, tty = int(parts[0]), parts[1]
        try:
            start = dt.datetime.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y").astimezone()
        except ValueError:
            start = None
        rows[pid] = (tty, start, parts[7])
    return rows


def on_tty(procs, pid):
    return pid in procs and procs[pid][0] not in NO_TTY


_ANC = None


def ancestors():
    """pids of this process and its parents (to skip the session that launched the script)."""
    global _ANC
    if _ANC is None:
        _ANC, pid = set(), os.getpid()
        if IS_WIN:
            parent = {p: pp for p, (pp, _) in _win_proc_snapshot().items()}
        else:
            out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
            parent = {int(a): int(b) for a, b in (l.split() for l in out.splitlines() if l.strip())}
        while pid in parent and pid > 1 and pid not in _ANC:
            _ANC.add(pid)
            pid = parent[pid]
    return _ANC


# ---------------- Claude Code ----------------

def cc_session_registry():
    out = []
    for f in glob.glob(os.path.join(CC_SESSIONS, "*.json")):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("pid") and meta.get("sessionId"):
            out.append(meta)
    return out


def cc_prompt(e, local):
    """(prompt text or None, new `local` flag) for one transcript entry. Same rules as claude-plugin/scripts/digest-label.sh:
    typed prompts and mid-turn queued prompts count; injected noise, meta entries, and local slash commands
    (/model, /clear, ... — preceded by a <local-command-caveat> meta entry) don't."""
    typ = e.get("type")
    if typ == "attachment":
        a = e.get("attachment") or {}
        if a.get("type") == "queued_command" and a.get("commandMode") == "prompt" and isinstance(a.get("prompt"), str):
            txt = strip_tags(a["prompt"])
            return (txt if txt and not txt.startswith(NOISE_PREFIX) else None), local
        return None, local
    if typ != "user" or e.get("isSidechain") or e.get("isCompactSummary"):
        return None, local
    content = (e.get("message") or {}).get("content")
    texts = [content] if isinstance(content, str) else [b.get("text") or "" for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
    if e.get("isMeta"):
        return None, local or any(t.lstrip().startswith("<local-command-caveat") for t in texts)
    out = []
    for txt in texts:
        name = re.search(r"<command-name>(.*?)</command-name>", txt, re.S)
        if name:
            if local or "<command-message>" not in txt:
                return None, False  # local command, not a task
            cargs = re.search(r"<command-args>(.*?)</command-args>", txt, re.S)
            txt = f"{name.group(1)} {cargs.group(1) if cargs else ''}"
        elif txt.lstrip().startswith(NOISE_PREFIX):
            continue
        txt = strip_tags(txt)
        if txt:
            out.append(txt)
    return (" ".join(out) or None), False  # any non-meta user turn ends the local-command window


def cc_transcript_path(session_id):
    hits = glob.glob(os.path.join(CC_PROJECTS, "*", f"{session_id}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def cc_targets(scope_since, live, procs):
    registry = cc_session_registry()
    own = {m["sessionId"] for m in registry if m["pid"] in ancestors()}
    targets = []
    if live:
        for meta in registry:
            pid = meta["pid"]
            if not on_tty(procs, pid) or meta.get("kind") != "interactive" or "claude" not in procs[pid][2].lower():
                continue
            if meta["sessionId"] in own:
                continue
            path = cc_transcript_path(meta["sessionId"])
            if path:
                targets.append({"path": path, "status": meta.get("status"), "pid": pid, "tty": procs[pid][0],
                                "session_id": meta["sessionId"], "session_cwd": meta.get("cwd")})
    else:
        for path in glob.glob(os.path.join(CC_PROJECTS, "*", "*.jsonl")):
            if "-private-tmp-" in path or "-private-var-" in path or "-AppData-Local-Temp-" in path:
                continue  # A/B harness and scratch worktrees
            if os.path.basename(path)[:-6] in own:
                continue
            if dt.datetime.fromtimestamp(os.path.getmtime(path)).astimezone() >= scope_since:
                targets.append({"path": path})
    return targets


def new_digest(tool, **kw):
    return {"tool": tool, "title": None, "cwd": None, "prompts": [], "edits": [], "cmds": [], "replies": [], "first": None, "last": None, **kw}


def cc_digest(t, since):
    d = new_digest("Claude Code", **{k: t[k] for k in ("status", "pid", "tty") if t.get(k)})
    custom_title, local = None, False
    with open(t["path"], encoding="utf-8", errors="replace") as fh:
        lines = list(fh)
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        typ = e.get("type")
        if typ == "ai-title":
            d["title"] = e.get("aiTitle")
        elif typ == "custom-title":
            custom_title = e.get("customTitle")
        prompt, local = cc_prompt(e, local)
        if typ not in ("user", "assistant") or e.get("isSidechain") or e.get("isMeta") or e.get("isCompactSummary"):
            if prompt:  # queued mid-turn prompt (attachment entry)
                ts = parse_ts((e.get("attachment") or {}).get("timestamp") or e.get("timestamp"))
                if not ts or ts >= since:
                    d["prompts"].append((ts, prompt))
            continue
        if e.get("entrypoint"):
            d["entrypoint"] = e["entrypoint"]
        ts = parse_ts(e.get("timestamp"))
        if ts and ts < since:
            continue
        d["cwd"] = e.get("cwd") or d["cwd"]
        if ts:
            d["first"] = d["first"] or ts
            d["last"] = ts
        if prompt:
            d["prompts"].append((ts, prompt))
        content = (e.get("message") or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if typ == "assistant" and b.get("type") == "text" and (b.get("text") or "").strip():
                d["replies"].append(b["text"])
            elif typ == "assistant" and b.get("type") == "tool_use":
                inp = b.get("input") or {}
                if b.get("name") in EDIT_TOOLS_CC and inp.get("file_path"):
                    d["edits"].append(inp["file_path"])
                elif b.get("name") == "Bash" and NOTABLE_CMD.search(inp.get("command") or ""):
                    d["cmds"].append(inp["command"])
    d["title"] = custom_title or d["title"]
    return d


# ---------------- agy ----------------

def _uri_to_path(uri):
    if not uri:
        return None
    if IS_WIN and uri.startswith("file:///"):
        return uri[len("file:///"):]
    return uri.removeprefix("file://")


def agy_meta():
    """conversation id -> (title, workspace). Reads legacy JSON cache and/or conversation_summaries.db."""
    out = {}
    try:
        with open(os.path.join(AGY, "cache", "conversation_metadata.json"), encoding="utf-8", errors="replace") as fh:
            convs = json.load(fh)["conversations"]
        for cid, v in convs.items():
            s = (v or {}).get("summary") or {}
            ws = [_uri_to_path(u) for u in s.get("WorkspaceURIs") or []]
            out[cid] = (s.get("Title") or None, ws[0] if ws else None)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    db_path = os.path.join(AGY, "conversation_summaries.db")
    if os.path.exists(db_path):
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                for cid, title, ws_json in con.execute("SELECT conversation_id, title, workspace_uris FROM conversation_summaries"):
                    if cid in out and out[cid][0] and out[cid][1]:
                        continue
                    try:
                        ws = [_uri_to_path(u) for u in (json.loads(ws_json) if ws_json else [])]
                    except ValueError:
                        ws = []
                    prev_t, prev_w = out.get(cid, (None, None))
                    out[cid] = (prev_t or title or None, prev_w or (ws[0] if ws else None))
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return out


def _win_agy_lock_owners():
    """Windows: pid -> sorted [conversationId, ...] using Restart Manager on presence/*.lock."""
    import ctypes.wintypes as w

    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [("dwProcessId", w.DWORD), ("ProcessStartTime", w.FILETIME)]

    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [
            ("Process", RM_UNIQUE_PROCESS),
            ("strAppName", w.WCHAR * 256),
            ("strServiceShortName", w.WCHAR * 64),
            ("ApplicationType", w.UINT),
            ("AppStatus", w.ULONG),
            ("TSSessionId", w.DWORD),
            ("bRestartable", w.BOOL),
        ]

    try:
        rstrtmgr = ctypes.WinDLL("rstrtmgr")
    except OSError:
        return {}
    by_pid = {}
    for lp in glob.glob(os.path.join(AGY, "presence", "*.lock")):
        m = re.fullmatch(r"([0-9a-f-]{36})\.lock", os.path.basename(lp), re.I)
        if not m:
            continue
        cid = m.group(1)
        sess = w.DWORD()
        key = ctypes.create_unicode_buffer(64)
        if rstrtmgr.RmStartSession(ctypes.byref(sess), 0, key) != 0:
            continue
        try:
            arr = (w.LPCWSTR * 1)(os.path.abspath(lp))
            if rstrtmgr.RmRegisterResources(sess, 1, arr, 0, None, 0, None) != 0:
                continue
            needed, count, reason = w.UINT(0), w.UINT(0), w.DWORD(0)
            rstrtmgr.RmGetList(sess, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reason))
            if needed.value:
                buf = (RM_PROCESS_INFO * needed.value)()
                count = w.UINT(needed.value)
                if rstrtmgr.RmGetList(sess, ctypes.byref(needed), ctypes.byref(count), buf, ctypes.byref(reason)) == 0:
                    for i in range(count.value):
                        pid = int(buf[i].Process.dwProcessId)
                        by_pid.setdefault(pid, set()).add(cid)
        finally:
            rstrtmgr.RmEndSession(sess)
    return {pid: sorted(cids) for pid, cids in by_pid.items()}


def agy_open_convs(pid):
    if IS_WIN:
        return _win_agy_lock_owners().get(int(pid), [])
    out = subprocess.run(["lsof", "-p", str(pid), "-Fn"], capture_output=True, text=True).stdout
    return sorted(set(re.findall(r"antigravity-cli/brain/([0-9a-f-]{36})", out)))


def agy_own_convs(win_owners=None):
    """Conversations held by an agy ancestor of this script (the session that launched it)."""
    own = set()
    if os.environ.get("ANTIGRAVITY_CONVERSATION_ID"):
        own.add(os.environ["ANTIGRAVITY_CONVERSATION_ID"])
    if IS_WIN:
        snap = _win_proc_snapshot()
        owners = win_owners if win_owners is not None else _win_agy_lock_owners()
        for pid in ancestors():
            exe = (snap.get(pid) or (0, ""))[1]
            if re.match(r"agy(\.exe)?$", exe, re.I):
                own.update(owners.get(pid, []))
        return own
    out = subprocess.run(["ps", "-o", "pid=,command=", "-p", ",".join(map(str, ancestors()))], capture_output=True, text=True).stdout
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if re.match(r"(\S*/)?agy(\s|$)", cmd.strip()):
            own.update(agy_open_convs(pid))
    return own


def agy_targets(scope_since, live, procs):
    win_owners = _win_agy_lock_owners() if (IS_WIN and live) else None
    meta, own = agy_meta(), agy_own_convs(win_owners)
    if not live:
        ids = []
        for path in glob.glob(AGY_TRANSCRIPT.format("*")):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue  # deleted since the glob
            if dt.datetime.fromtimestamp(mtime).astimezone() >= scope_since:
                cid = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))
                ids.append(cid)
        return [{"id": cid, "meta": meta.get(cid)} for cid in ids if cid not in own]
    # live: each agy process on a tty holds brain/<conversationId> dirs (or presence/<cid>.lock on Windows).
    targets, seen = [], set()
    for pid, (tty, _, cmd) in procs.items():
        if tty in NO_TTY or not re.match(r"(\S*[/\\])?agy(\.exe)?(\s|$)", cmd, re.I) or AGY_PRINT.search(cmd) or pid in ancestors():
            continue
        convs = win_owners.get(pid, []) if IS_WIN else agy_open_convs(pid)
        for cid in convs:
            if cid not in seen and cid not in own and os.path.exists(AGY_TRANSCRIPT.format(cid)):
                seen.add(cid)
                targets.append({"id": cid, "meta": meta.get(cid), "pid": pid, "tty": tty})
    return targets


def unquote(v):
    """agy stores tool args as JSON-encoded strings, sometimes with raw control chars."""
    if isinstance(v, str) and v[:1] == '"':
        try:
            return json.loads(v, strict=False)
        except ValueError:
            return v.strip('"')
    return v


def agy_digest(t, since):
    title, ws = t.get("meta") or (None, None)
    d = new_digest("agy", title=title, cwd=ws, **{k: t[k] for k in ("pid", "tty") if t.get(k)})
    try:
        with open(AGY_TRANSCRIPT.format(t["id"]), encoding="utf-8", errors="replace") as fh:
            lines = list(fh)
    except OSError:
        return d  # deleted since it was listed; no prompts, so keep() drops it
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        ts = parse_ts(e.get("created_at"))
        if ts and ts < since:
            continue
        if ts:
            d["first"] = d["first"] or ts
            d["last"] = ts
        typ = e.get("type")
        if typ == "USER_INPUT" and e.get("source") == "USER_EXPLICIT":
            txt = strip_tags(e.get("content") or "")
            if txt:
                d["prompts"].append((ts, txt))
        elif typ == "PLANNER_RESPONSE":
            if (e.get("content") or "").strip():
                d["replies"].append(e["content"])
            for call in e.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                name, args = call.get("name") or "", {k: unquote(v) for k, v in (call.get("args") or {}).items()}
                if re.search(r"write|edit|replace", name):
                    p = args.get("TargetFile") or args.get("Path") or args.get("AbsolutePath")
                    if p:
                        d["edits"].append(str(p))
                elif name == "run_command" and NOTABLE_CMD.search(str(args.get("CommandLine") or "")):
                    d["cmds"].append(str(args["CommandLine"]))
                if not d["cwd"] and args.get("Cwd"):
                    d["cwd"] = str(args["Cwd"])
    if not d["title"] and d["prompts"]:
        d["title"] = clip(d["prompts"][0][1], 50)
    return d


# ---------------- LLM ----------------

def find_bin(name):
    """launchd / Scheduled Task PATH may lack ~/.local/bin (where agy/claude install), so also try usual dirs."""
    candidates = [shutil.which(name)]
    if IS_WIN:
        candidates += [os.path.join(HOME, ".local", "bin", f"{name}{ext}") for ext in (".exe", ".cmd")]
    else:
        candidates += [os.path.join(HOME, ".local", "bin", name), f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"]
    for p in candidates:
        if p and os.path.isfile(p) and (IS_WIN or os.access(p, os.X_OK)):
            return p
    return None


def pick_backend(preferred):
    """preferred if installed, else the other one, else preferred (the call then reports it can't run)."""
    other = "claude" if preferred == "agy" else "agy"
    return preferred if find_bin(preferred) or not find_bin(other) else other


def agy_configured_model():
    try:
        with open(os.path.join(AGY, "settings.json"), encoding="utf-8", errors="replace") as f:
            m = json.load(f).get("model")
        return m.strip() if isinstance(m, str) and m.strip() else None
    except (OSError, ValueError, AttributeError):
        return None


def agy_resolve_model(requested, available):
    """Map an explicit model slug/display name (or settings.json default when requested is None) to an ACP modelId."""
    target = requested or agy_configured_model()
    if not target:
        return None
    for m in available or []:
        if isinstance(m, dict) and target in (m.get("modelId"), m.get("name")):
            return m.get("modelId")
    # explicit bad --agy-model should reach session/set_model and error; stale settings.json falls back to ACP default
    return requested


AGY_AGENT_MD = """---
name: summarizer
description: Concise session summarizer.
tools: []
mainAgent: true
subagent: false
mcpServers: []
skills: []
---
You are a concise summarizer.
"""


def agy_acp(exe, prompt, model, timeout):
    """One isolated `agy acp` turn: writes state & a minimal no-tools agent to a per-call dir under AGY_WORKDIR, never ~/.gemini/antigravity-cli."""
    state_dir = None
    try:
        os.makedirs(AGY_WORKDIR, exist_ok=True)
        state_dir = tempfile.mkdtemp(dir=AGY_WORKDIR, prefix="acp-")
        agent_dir = os.path.join(state_dir, ".agents", "agents", "summarizer")
        os.makedirs(agent_dir, exist_ok=True)
        with open(os.path.join(agent_dir, "agent.md"), "w", encoding="utf-8") as f:
            f.write(AGY_AGENT_MD)
    except OSError as e:
        if state_dir:
            shutil.rmtree(state_dir, ignore_errors=True)
        return False, f"cannot prepare agy scratch dir: {e}"
    gemini_dir = os.path.relpath(state_dir, HOME)
    cmd = [exe, "acp", "--agent", "summarizer", "--gemini-dir", gemini_dir, "--app-data-dir", "state", "--include-default-customizations=false", "--disable-telemetry"]
    popen_kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if IS_WIN else {"start_new_session": True}
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=state_dir, **popen_kw)
    except OSError as e:
        shutil.rmtree(state_dir, ignore_errors=True)
        return False, f"cannot run agy: {e}"

    deadline = time.monotonic() + timeout
    out_q, err_chunks, chunks = queue.Queue(), [], []
    timed_out, done, proc_lock = threading.Event(), threading.Event(), threading.Lock()
    reaped = False

    def terminate_proc(force=False):
        with proc_lock:
            if reaped:
                return
            try:
                if IS_WIN:
                    if force:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=3,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    else:
                        p.terminate()
                else:
                    os.killpg(p.pid, signal.SIGKILL if force else signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
                if IS_WIN and force:
                    try:
                        p.kill()
                    except OSError:
                        pass

    def on_timeout():
        timed_out.set()
        terminate_proc(force=False)
        if not done.wait(3):
            terminate_proc(force=True)

    def read_stdout():
        try:
            for raw in p.stdout:
                out_q.put(raw)
        except (OSError, ValueError):
            pass
        finally:
            try:
                p.stdout.close()
            except OSError:
                pass
            out_q.put(None)

    def read_stderr():
        kept = 0
        try:
            while True:
                data = p.stderr.read(65536)
                if not data:
                    break
                if kept < 65536:
                    take = data[: 65536 - kept]
                    err_chunks.append(take)
                    kept += len(take)
        except (OSError, ValueError):
            pass
        finally:
            try:
                p.stderr.close()
            except OSError:
                pass

    timer = threading.Timer(timeout, on_timeout)
    timer.daemon = True
    t_out = threading.Thread(target=read_stdout, daemon=True)
    t_err = threading.Thread(target=read_stderr, daemon=True)
    timer.start()
    t_out.start()
    t_err.start()

    def stderr_text(wait_s=0.3):
        t_err.join(timeout=wait_s)
        return b"".join(list(err_chunks)).decode("utf-8", "replace").strip()

    def send(obj):
        if timed_out.is_set() or time.monotonic() >= deadline:
            raise TimeoutError()
        p.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        p.stdin.flush()

    def recv(want_id):
        while True:
            rem = deadline - time.monotonic()
            if timed_out.is_set() or rem <= 0:
                raise TimeoutError()
            try:
                raw = out_q.get(timeout=rem)
            except queue.Empty:
                raise TimeoutError()
            if raw is None:
                return None
            if not raw.strip():
                continue
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "method" in msg and msg.get("id") is not None:
                # server-initiated request (e.g. tool permission): deny so no tool can run and server never hangs
                if msg["method"] == "session/request_permission":
                    send({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": {"outcome": "cancelled"}}})
                else:
                    send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})
            elif msg.get("method") == "session/update":
                upd = (msg.get("params") or {}).get("update") or {}
                if upd.get("sessionUpdate") == "agent_message_chunk":
                    c = upd.get("content") or {}
                    if c.get("type") == "text":
                        chunks.append(c.get("text") or "")
            elif msg.get("id") == want_id:
                return msg

    def rpc(req_id, method, params):
        send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        resp = recv(req_id)
        if resp is None:
            if timed_out.is_set():
                raise TimeoutError()
            err_txt = stderr_text()
            return None, f"agy acp exited during {method}" + (f": {err_txt[:500]}" if err_txt else "")
        if resp.get("error") is not None:
            err = resp["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return None, f"agy acp {method} failed: {msg}"
        return resp.get("result") or {}, None

    try:
        _, err = rpc(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        if err:
            return False, err
        res, err = rpc(2, "session/new", {"cwd": state_dir, "mcpServers": []})
        if err:
            return False, err
        sid = res.get("sessionId")
        if not sid:
            return False, "agy acp session/new returned no sessionId"
        models_info = res.get("models") or {}
        target_model = agy_resolve_model(model, models_info.get("availableModels"))
        if target_model and target_model != models_info.get("currentModelId"):
            _, err = rpc(3, "session/set_model", {"sessionId": sid, "modelId": target_model})
            if err:
                return False, err
        res, err = rpc(4, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]})
        if err:
            return False, err
        out = "".join(chunks).strip()
        stop = res.get("stopReason")
        if stop not in (None, "end_turn", "max_tokens") or not out:
            err_txt = stderr_text()
            return False, f"agy acp failed (stopReason={stop}): {err_txt[:500]}"
        return True, out
    except TimeoutError:
        return False, f"agy acp timed out ({timeout}s)"
    except OSError as e:
        if timed_out.is_set():
            return False, f"agy acp timed out ({timeout}s)"
        err_txt = stderr_text()
        return False, f"agy acp I/O error ({e})" + (f": {err_txt[:500]}" if err_txt else "")
    finally:
        done.set()
        timer.cancel()
        timer.join(timeout=5)
        try:
            p.stdin.close()
        except OSError:
            pass
        terminate_proc(force=False)
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            terminate_proc(force=True)
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        with proc_lock:
            reaped = p.poll() is not None
        t_out.join(timeout=0.5)
        t_err.join(timeout=0.5)
        shutil.rmtree(state_dir, ignore_errors=True)


def split_cmd(cmd_str):
    """Split a command string into argv using native rules (CommandLineToArgvW on Windows, shlex on POSIX)."""
    if not cmd_str or not cmd_str.strip():
        return []
    if IS_WIN:
        import ctypes.wintypes as w
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32.CommandLineToArgvW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(w.LPWSTR)
        k32.LocalFree.argtypes = [w.HLOCAL]
        argc = ctypes.c_int(0)
        argv = shell32.CommandLineToArgvW(cmd_str, ctypes.byref(argc))
        if not argv:
            raise ValueError("CommandLineToArgvW failed")
        try:
            return [argv[i] for i in range(argc.value)]
        finally:
            k32.LocalFree(argv)
    return shlex.split(cmd_str)


def decode_llm_cmd(raw, raw_b64):
    if raw_b64:
        try:
            return base64.b64decode(raw_b64).decode("utf-8").strip()
        except (ValueError, UnicodeDecodeError) as e:
            sys.exit(f"bad --llm-cmd-b64: {e}")
    return (raw or "").strip()


def custom_llm(cmd_str, prompt, model, timeout):
    """Run a user-configured summarizer command (reads prompt on stdin, prints reply on stdout). Supports optional {model} placeholder."""
    try:
        parts = split_cmd(cmd_str)
    except ValueError as e:
        return False, f"bad llm command {cmd_str!r}: {e}"
    if not parts or not parts[0]:
        return False, "empty llm command"
    if any("{model}" in p for p in parts):
        if not model:
            return False, "{model} placeholder requires --model"
        cmd = [p.replace("{model}", model) for p in parts]
    else:
        cmd = list(parts)
    exe = os.path.expanduser(cmd[0])
    if os.path.sep in exe or (os.altsep and os.altsep in exe):
        cmd[0] = os.path.abspath(exe)
    else:
        cmd[0] = find_bin(exe) or exe
    kw = {"cwd": AGY_WORKDIR}
    if IS_WIN:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        os.makedirs(AGY_WORKDIR, exist_ok=True)
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **kw)
        stdout, stderr, rc = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return False, f"{parts[0]} timed out ({timeout}s)"
    except OSError as e:
        return False, f"cannot run {parts[0]}: {e}"
    if rc != 0 or not (stdout or "").strip():
        return False, f"{parts[0]} failed (exit={rc}): {(stderr or stdout or '').strip()[:500]}"
    return True, stdout.strip()


def llm(prompt, backend, model, timeout, llm_cmd=None):
    """One non-interactive call. backend: claude | agy | custom; model None = the CLI's default. Returns (ok, text or error)."""
    if backend == "custom":
        cmd_str = (llm_cmd or LLM_CMD).strip()
        return custom_llm(cmd_str, prompt, model, timeout)
    exe = find_bin(backend)
    if not exe:
        return False, f"cannot run {backend}: not found"
    if backend == "agy":
        return agy_acp(exe, prompt, model, timeout)
    cmd = [
        exe, "-p", "--no-session-persistence", "--tools", "", "--setting-sources", "",
        "--strict-mcp-config", "--disable-slash-commands", "--no-chrome",
        "--system-prompt", "You are a concise summarizer.",
    ] + (["--model", model] if model else [])
    kw = {"cwd": AGY_WORKDIR, "env": {**os.environ, "CLAUDE_CODE_SAFE_MODE": "1"}}
    if IS_WIN:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        os.makedirs(AGY_WORKDIR, exist_ok=True)
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **kw)
        stdout, stderr, rc = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return False, f"claude -p timed out ({timeout}s)"
    except OSError as e:
        return False, f"cannot run {backend}: {e}"
    if rc != 0 or not (stdout or "").strip():
        return False, f"claude -p failed (exit={rc}): {(stderr or stdout or '').strip()[:500]}"
    return True, stdout.strip()


# ---------------- render ----------------

def is_probe(d):
    """Health-check one-shots like 'Reply with just: ok' — one short prompt, no edits, no commands."""
    return len(d["prompts"]) <= 1 and sum(len(p) for _, p in d["prompts"]) < 40 and not d["edits"] and not d["cmds"]


def is_summarizer(d):
    return bool(d["prompts"]) and d["prompts"][0][1].startswith(SUMMARIZER_PROMPT_HEADS)


def keep(d):
    if not d["prompts"]:
        return False  # agy subagents and SDK warm-ups have no human input
    if (d.get("entrypoint") or "").startswith("sdk"):
        return False  # headless claude -p / orchestrator sessions
    if is_summarizer(d):
        return False  # legacy summarizer conversations, if any remain in ~/.gemini/antigravity-cli/brain
    return not is_probe(d)


def uniq(xs):
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def cmd_snip(c, n=160):
    """Clip around the notable part; commands often start with a long `S=/private/tmp/...;` prefix."""
    c = re.sub(r"\s+", " ", c).strip()
    m = NOTABLE_CMD.search(c)
    start = max(0, m.start() - 30) if m else 0
    s = c[start:start + n]
    return ("…" if start else "") + s + ("…" if start + n < len(c) else "")


def render(d, max_prompts=12):
    home = lambda p: (p or "").replace(HOME, "~")
    hm = lambda t: t.strftime("%m-%d %H:%M") if t else "?"
    lines = [f"### [{d['tool']}] {d.get('title') or '(untitled)'}"]
    meta = f"cwd: {home(d['cwd'])} | active: {hm(d['first'])} → {hm(d['last'])}"
    if d.get("status"):
        meta += f" | status: {d['status']}"
    if d.get("tty"):
        meta += f" | {d['tty']}"
    lines.append(meta)
    prompts = d["prompts"]
    if len(prompts) > max_prompts:
        lines.append(f"User prompts ({len(prompts)} total; first + last {max_prompts - 1}):")
        prompts = prompts[:1] + prompts[-(max_prompts - 1):]
    else:
        lines.append("User prompts:")
    lines += [f"- {hm(ts)} {clip(p, 300)}" for ts, p in prompts]
    edits = uniq(home(p) for p in d["edits"])
    if edits:
        lines.append(f"Files edited ({len(edits)}): " + ", ".join(edits[:20]) + (" …" if len(edits) > 20 else ""))
    cmds = uniq(cmd_snip(c) for c in d["cmds"])
    if cmds:
        lines.append("Notable commands:")
        lines += [f"- `{c}`" for c in cmds[-10:]]
    if d["replies"]:
        lines.append("Last replies:")
        lines += [f"> {clip(r, 700)}" for r in d["replies"][-2:]]
    return "\n".join(lines)


PROMPT = """Below are raw excerpts from several AI coding sessions (Claude Code / agy) I have open in my terminal.
Write a concise work summary in markdown, in {lang}:

1. Open with a 2-3 sentence overview: which threads of work are in progress today.
2. One section per session, headed "<session title, translated into {lang} if needed> · <repo>", with three lines:
   - **Goal**: one sentence.
   - **Progress**: substantive changes/conclusions (what changed, how far commit/PR/deploy got); don't list files.
   - **Status**: done / in progress / blocked (on what) / waiting on me (for what decision).
3. End with a "Needs me" list: only items that truly need a human; write "None" if there are none.

Only use what the excerpts say; if unsure, write "unclear from the log". Don't quote the prompts verbatim, no pleasantries.

---
{body}
"""
# first words of the prompts digest.py / titles.py send; keep() drops agy conversations that start with them
SUMMARIZER_PROMPT_HEADS = ("Below are raw excerpts from several AI coding sessions", "Write a label for each AI coding session")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scope", default="live", help="live | today | <N>h  (default live)")
    ap.add_argument("--raw", action="store_true", help="print the extracted digest only; no LLM call")
    ap.add_argument("--backend", choices=["auto", "claude", "agy"], default="auto", help="who writes the summary; auto = DIGEST_LLM_CMD if set, else agy when every session is agy, else claude")
    ap.add_argument("--llm-cmd", default="", help="custom summarizer command reading prompt on stdin (overrides DIGEST_LLM_CMD and --backend), e.g. 'claude-x -p'")
    ap.add_argument("--llm-cmd-b64", default="", help=argparse.SUPPRESS)
    ap.add_argument("--model", default="sonnet", help="model when claude writes the report, and {model} placeholder in --llm-cmd (default sonnet)")
    ap.add_argument("--agy-model", help="model when agy writes the report (default: agy's configured model; see `agy models`)")
    ap.add_argument("--tool", choices=["all", "claude", "agy"], default="all")
    ap.add_argument("--extra-cmds", default="", help="extra notable-command regex alternation (alternative to DIGEST_EXTRA_CMDS)")
    args = ap.parse_args()

    if args.extra_cmds:
        global NOTABLE_CMD
        extra = args.extra_cmds.strip("|")
        try:
            NOTABLE_CMD = re.compile(r"\b(git (commit|push|merge|rebase|worktree add)|gh pr|kubectl|helm|terraform apply|docker push|npm publish|deploy|rsync|" + extra + r")\b")
        except re.error as err:
            sys.exit(f"bad --extra-cmds regex: {err}")

    n = now()
    live = args.scope == "live"
    if live:
        since = n - dt.timedelta(hours=24)
    elif args.scope == "today":
        # workday starts 06:00; late-night work belongs to the previous day
        since = n.replace(hour=6, minute=0, second=0, microsecond=0)
        if n < since:
            since -= dt.timedelta(days=1)
    elif m := re.fullmatch(r"(\d+)h", args.scope):
        since = n - dt.timedelta(hours=int(m.group(1)))
    else:
        sys.exit(f"bad --scope: {args.scope}")

    procs = ps_table() if live else {}
    digests = []
    if args.tool in ("all", "claude"):
        digests += [cc_digest(t, since) for t in cc_targets(since, live, procs)]
    if args.tool in ("all", "agy"):
        digests += [agy_digest(t, since) for t in agy_targets(since, live, procs)]
    digests = [d for d in digests if keep(d)]
    digests.sort(key=lambda d: d["last"] or n, reverse=True)
    if not digests:
        print(f"No sessions with activity in scope {args.scope}.")
        return

    body = "\n\n".join(render(d) for d in digests)
    if args.raw:
        print(body)
        return

    cli_llm_cmd = decode_llm_cmd(args.llm_cmd, args.llm_cmd_b64)
    custom_cmd = cli_llm_cmd or (LLM_CMD if args.backend == "auto" else "")
    if custom_cmd:
        backend, model = "custom", args.model
        try:
            parts = split_cmd(custom_cmd)
            label = os.path.basename(parts[0]) if parts else "custom"
        except ValueError:
            label = "custom"
    else:
        backend = args.backend if args.backend != "auto" else pick_backend("agy" if all(d["tool"] == "agy" for d in digests) else "claude")
        model = args.model if backend == "claude" else args.agy_model
        label = backend + (f" {model}" if model else "")
    print(f"Summarizing {len(digests)} sessions ({args.scope}, {label})…", file=sys.stderr)
    ok, out = llm(PROMPT.format(body=body, lang=SUMMARY_LANG), backend, model, LLM_TIMEOUT_S, llm_cmd=custom_cmd)
    if not ok:
        print(f"{out}\n--- raw digest ---\n{body}")
        sys.exit(1)
    print(out)

if __name__ == "__main__":
    main()
