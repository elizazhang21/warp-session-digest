#!/usr/bin/env python3
"""Summarize what Claude Code / agy (Antigravity CLI) sessions have been doing.

Sources (all local, read-only):
  Claude Code: ~/.claude/sessions/<pid>.json (live pid -> sessionId), ~/.claude/projects/*/<sessionId>.jsonl
  agy:         ~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl (+ cache/conversation_metadata.json for titles)

Scopes: live = sessions attached to a terminal right now (what Warp's switcher shows), last 24h of each;
today = everything active since 06:00 (previous day before 06:00); <N>h = everything active in the last N hours.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys

HOME = os.path.expanduser("~")
CC_SESSIONS = f"{HOME}/.claude/sessions"
CC_PROJECTS = f"{HOME}/.claude/projects"
AGY = f"{HOME}/.gemini/antigravity-cli"
AGY_TRANSCRIPT = AGY + "/brain/{}/.system_generated/logs/transcript.jsonl"

# Commands worth surfacing in the digest; extend with DIGEST_EXTRA_CMDS (a regex alternation, e.g. "mycli deploy|ship\.sh").
_EXTRA_CMDS = os.environ.get("DIGEST_EXTRA_CMDS", "").strip("|")
try:
    NOTABLE_CMD = re.compile(r"\b(git (commit|push|merge|rebase|worktree add)|gh pr|kubectl|helm|terraform apply|docker push|npm publish|deploy|rsync" + (f"|{_EXTRA_CMDS}" if _EXTRA_CMDS else "") + r")\b")
except re.error as err:
    sys.exit(f"bad DIGEST_EXTRA_CMDS regex: {err}")
EDIT_TOOLS_CC = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
NOISE_PREFIX = ("<local-command", "<task-notification", "<system-reminder", "<bash-input", "<bash-stdout", "<bash-stderr", "Caveat:", "[Request interrupted", "This session is being continued")
CLAUDE_TIMEOUT_S = 300
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


def ps_table():
    """pid -> (tty, start datetime or None, command)."""
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
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
        parent = {int(a): int(b) for a, b in (l.split() for l in out.splitlines() if l.strip())}
        while pid in parent and pid > 1:
            _ANC.add(pid)
            pid = parent[pid]
    return _ANC


# ---------------- Claude Code ----------------

def cc_session_registry():
    out = []
    for f in glob.glob(f"{CC_SESSIONS}/*.json"):
        try:
            meta = json.load(open(f))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("pid") and meta.get("sessionId"):
            out.append(meta)
    return out


def cc_transcript_path(session_id):
    hits = glob.glob(f"{CC_PROJECTS}/*/{session_id}.jsonl")
    return max(hits, key=os.path.getmtime) if hits else None


def cc_targets(scope_since, live, procs):
    registry = cc_session_registry()
    own = {m["sessionId"] for m in registry if m["pid"] in ancestors()}
    targets = []
    if live:
        for meta in registry:
            pid = meta["pid"]
            if not on_tty(procs, pid) or meta.get("kind") != "interactive" or "claude" not in procs[pid][2]:
                continue
            if meta["sessionId"] in own:
                continue
            path = cc_transcript_path(meta["sessionId"])
            if path:
                targets.append({"path": path, "status": meta.get("status"), "pid": pid, "tty": procs[pid][0]})
    else:
        for path in glob.glob(f"{CC_PROJECTS}/*/*.jsonl"):
            if "-private-tmp-" in path or "-private-var-" in path:
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
    custom_title = None
    for line in open(t["path"], errors="replace"):
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
        if typ not in ("user", "assistant") or e.get("isSidechain") or e.get("isMeta") or e.get("isCompactSummary"):
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
        content = (e.get("message") or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if typ == "user" and b.get("type") == "text":
                txt = b.get("text") or ""
                name = re.search(r"<command-name>(.*?)</command-name>", txt, re.S)
                if name:
                    cargs = re.search(r"<command-args>(.*?)</command-args>", txt, re.S)
                    txt = f"{name.group(1)} {cargs.group(1) if cargs else ''}"
                elif txt.lstrip().startswith(NOISE_PREFIX):
                    continue
                txt = strip_tags(txt)
                if txt:
                    d["prompts"].append((ts, txt))
            elif typ == "assistant" and b.get("type") == "text" and (b.get("text") or "").strip():
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

def agy_meta():
    """conversation id -> (title, workspace). The cache lags and misses new conversations; titles only."""
    try:
        convs = json.load(open(f"{AGY}/cache/conversation_metadata.json"))["conversations"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    out = {}
    for cid, v in convs.items():
        s = (v or {}).get("summary") or {}
        ws = [u.removeprefix("file://") for u in s.get("WorkspaceURIs") or []]
        out[cid] = (s.get("Title") or None, ws[0] if ws else None)
    return out


def agy_open_convs(pid):
    out = subprocess.run(["lsof", "-p", str(pid), "-Fn"], capture_output=True, text=True).stdout
    return sorted(set(re.findall(r"antigravity-cli/brain/([0-9a-f-]{36})", out)))


def agy_own_convs():
    """Conversations held by an agy ancestor of this script (the session that launched it)."""
    out = subprocess.run(["ps", "-o", "pid=,command=", "-p", ",".join(map(str, ancestors()))], capture_output=True, text=True).stdout
    own = set()
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if re.match(r"(\S*/)?agy(\s|$)", cmd.strip()):
            own.update(agy_open_convs(pid))
    return own


def agy_targets(scope_since, live, procs):
    meta, own = agy_meta(), agy_own_convs()
    if not live:
        ids = []
        for path in glob.glob(AGY_TRANSCRIPT.format("*")):
            if dt.datetime.fromtimestamp(os.path.getmtime(path)).astimezone() >= scope_since:
                ids.append(path.split("/brain/")[1].split("/")[0])
        return [{"id": cid, "meta": meta.get(cid)} for cid in ids if cid not in own]
    # live: each agy process on a tty holds brain/<conversationId> dirs open; that is the authoritative mapping.
    targets, seen = [], set()
    for pid, (tty, _, cmd) in procs.items():
        if tty in NO_TTY or not re.match(r"(\S*/)?agy(\s|$)", cmd) or pid in ancestors():
            continue
        for cid in agy_open_convs(pid):
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
    for line in open(AGY_TRANSCRIPT.format(t["id"]), errors="replace"):
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
    lines = [f"### [{d['tool']}] {d.get('title') or '(无标题)'}"]
    meta = f"cwd: {home(d['cwd'])} | 活跃: {hm(d['first'])} → {hm(d['last'])}"
    if d.get("status"):
        meta += f" | 状态: {d['status']}"
    if d.get("tty"):
        meta += f" | {d['tty']}"
    lines.append(meta)
    prompts = d["prompts"]
    if len(prompts) > max_prompts:
        lines.append(f"用户指令（共 {len(prompts)} 条，首条 + 最近 {max_prompts - 1} 条）:")
        prompts = prompts[:1] + prompts[-(max_prompts - 1):]
    else:
        lines.append("用户指令:")
    lines += [f"- {hm(ts)} {clip(p, 300)}" for ts, p in prompts]
    edits = uniq(home(p) for p in d["edits"])
    if edits:
        lines.append(f"改过的文件（{len(edits)}）: " + ", ".join(edits[:20]) + (" …" if len(edits) > 20 else ""))
    cmds = uniq(cmd_snip(c) for c in d["cmds"])
    if cmds:
        lines.append("关键命令:")
        lines += [f"- `{c}`" for c in cmds[-10:]]
    if d["replies"]:
        lines.append("最后的回复:")
        lines += [f"> {clip(r, 700)}" for r in d["replies"][-2:]]
    return "\n".join(lines)


PROMPT = """下面是我在终端里同时开着的若干 AI 编码会话（Claude Code / agy）的原始摘录。
请用中文写一份简洁的工作总结，markdown 格式：

1. 开头 2–3 句总览：今天主要在推进哪几件事。
2. 每个会话一节，标题用「会话标题 · 仓库名」，下面三行：
   - **在做**：一句话说目标。
   - **进展**：做了哪些实质改动/结论（改了什么、提交/MR/发布到哪一步），不罗列文件。
   - **状态**：完成 / 进行中 / 卡住（卡在哪）/ 等我决定（要我决定什么）。
3. 最后「需要我处理」列表：只列真正需要人介入的事项；没有就写「无」。

只根据摘录说话，不确定就写「从记录看不出」。不要重复原始指令原文，不要客套。

---
{body}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scope", default="live", help="live | today | <N>h  (default live)")
    ap.add_argument("--raw", action="store_true", help="只打印抽取的原始摘录，不调用 LLM")
    ap.add_argument("--model", default="sonnet", help="claude -p 用的模型 (default sonnet)")
    ap.add_argument("--tool", choices=["all", "claude", "agy"], default="all")
    args = ap.parse_args()

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
        print(f"没有找到 {args.scope} 范围内有活动的会话。")
        return

    body = "\n\n".join(render(d) for d in digests)
    if args.raw:
        print(body)
        return

    print(f"总结 {len(digests)} 个会话（{args.scope}，{args.model}）…", file=sys.stderr)
    cmd = ["claude", "-p", "--model", args.model, "--no-session-persistence", "--tools", "", "--setting-sources", ""]
    try:
        r = subprocess.run(cmd, input=PROMPT.format(body=body), capture_output=True, text=True, timeout=CLAUDE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(f"claude -p 超时（{CLAUDE_TIMEOUT_S}s）\n--- 原始摘录 ---\n{body}")
        sys.exit(1)
    if r.returncode != 0 or not r.stdout.strip():
        print(f"claude -p 失败 (exit={r.returncode}): {r.stderr.strip()[:500]}\n--- 原始摘录 ---\n{body}")
        sys.exit(1)
    print(r.stdout.strip())


if __name__ == "__main__":
    main()
