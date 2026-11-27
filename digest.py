#!/usr/bin/env python3
"""Summarize what Claude Code / agy (Antigravity CLI) sessions have been doing.

Sources (all local, read-only):
  Claude Code: ~/.claude/sessions/<pid>.json (live pid -> sessionId), ~/.claude/projects/*/<sessionId>.jsonl
  agy:         ~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl (+ cache/conversation_metadata.json for titles)

Scopes: live = sessions attached to a terminal right now (what Warp's switcher shows), last 24h of each;
today = everything active since 06:00 (previous day before 06:00); <N>h = everything active in the last N hours.
"""
import argparse
import ctypes
import datetime as dt
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

IS_WIN = sys.platform == "win32"
HOME = os.path.expanduser("~")
CC_SESSIONS = os.path.join(HOME, ".claude", "sessions")
CC_PROJECTS = os.path.join(HOME, ".claude", "projects")
AGY = os.path.join(HOME, ".gemini", "antigravity-cli")
AGY_TRANSCRIPT = os.path.join(AGY, "brain", "{}", ".system_generated", "logs", "transcript.jsonl")

# Commands worth surfacing in the digest; extend with DIGEST_EXTRA_CMDS (a regex alternation, e.g. "mycli deploy|ship\.sh").
_EXTRA_CMDS = os.environ.get("DIGEST_EXTRA_CMDS", "").strip("|")
try:
    NOTABLE_CMD = re.compile(r"\b(git (commit|push|merge|rebase|worktree add)|gh pr|kubectl|helm|terraform apply|docker push|npm publish|deploy|rsync" + (f"|{_EXTRA_CMDS}" if _EXTRA_CMDS else "") + r")\b")
except re.error as err:
    sys.exit(f"bad DIGEST_EXTRA_CMDS regex: {err}")
EDIT_TOOLS_CC = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
NOISE_PREFIX = ("<local-command", "<task-notification", "<system-reminder", "<bash-input", "<bash-stdout", "<bash-stderr", "Caveat: The messages below", "[Request interrupted", "This session is being continued")
CLAUDE_TIMEOUT_S = 300
SUMMARY_LANG = os.environ.get("DIGEST_LANG", "English")  # language of generated summaries and labels
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


def claude_bin():
    found = shutil.which("claude")
    if found:
        return found
    return "claude" if IS_WIN else "/opt/homebrew/bin/claude"


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
            if dt.datetime.fromtimestamp(os.path.getmtime(path)).astimezone() >= scope_since:
                cid = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))
                ids.append(cid)
        return [{"id": cid, "meta": meta.get(cid)} for cid in ids if cid not in own]
    # live: each agy process on a tty holds brain/<conversationId> dirs (or presence/<cid>.lock on Windows).
    targets, seen = [], set()
    for pid, (tty, _, cmd) in procs.items():
        if tty in NO_TTY or not re.match(r"(\S*[/\\])?agy(\.exe)?(\s|$)", cmd, re.I) or pid in ancestors():
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
    with open(AGY_TRANSCRIPT.format(t["id"]), encoding="utf-8", errors="replace") as fh:
        lines = list(fh)
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


# ---------------- render ----------------

def is_probe(d):
    """Health-check one-shots like 'Reply with just: ok' — one short prompt, no edits, no commands."""
    return len(d["prompts"]) <= 1 and sum(len(p) for _, p in d["prompts"]) < 40 and not d["edits"] and not d["cmds"]


def keep(d):
    if not d["prompts"]:
        return False  # agy subagents and SDK warm-ups have no human input
    if (d.get("entrypoint") or "").startswith("sdk"):
        return False  # headless claude -p / orchestrator sessions
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


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scope", default="live", help="live | today | <N>h  (default live)")
    ap.add_argument("--raw", action="store_true", help="print the extracted digest only; no LLM call")
    ap.add_argument("--model", default="sonnet", help="model for claude -p (default sonnet)")
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

    print(f"Summarizing {len(digests)} sessions ({args.scope}, {args.model})…", file=sys.stderr)
    cmd = [claude_bin(), "-p", "--model", args.model, "--no-session-persistence", "--tools", "", "--setting-sources", ""]
    try:
        r = subprocess.run(cmd, input=PROMPT.format(body=body, lang=SUMMARY_LANG), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=CLAUDE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(f"claude -p timed out ({CLAUDE_TIMEOUT_S}s)\n--- raw digest ---\n{body}")
        sys.exit(1)
    if r.returncode != 0 or not r.stdout.strip():
        print(f"claude -p failed (exit={r.returncode}): {r.stderr.strip()[:500]}\n--- raw digest ---\n{body}")
        sys.exit(1)
    print(r.stdout.strip())


if __name__ == "__main__":
    main()
