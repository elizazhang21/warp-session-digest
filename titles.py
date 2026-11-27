#!/usr/bin/env python3
"""Make Warp's session list show a one-line summary of what each live Claude Code / agy session is doing.

Run periodically (launchd, see install-titles.sh). Each run finds live sessions, re-summarizes the ones that
changed (one batched call per tool: `claude -p` for Claude Code, `agy acp` for agy), and pushes the summary to Warp:
  agy          -> terminal title (OSC 0); Warp lists non-agent terminals by title.
  Claude Code  -> Warp lists it by the `query` of its warp://cli-agent events, not the title. While the session
                  is busy we send a `prompt_submit` event (keeps status "working", no notification). Idle sessions
                  can only be relabelled by a `stop` event, which notifies — so that is left to the forked plugin's
                  Stop hook (claude-plugin/), which reads the summary from STATE.
"""
import argparse
import ctypes
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import digest  # noqa: E402

IS_WIN = sys.platform == "win32"
STATE = os.environ.get("WARP_DIGEST_STATE") or os.path.join(os.path.expanduser("~"), ".cache", "warp-session-digest", "titles.json")
PIDFILE = os.path.join(os.path.dirname(STATE), "titles.pid")
REFRESH_S = 180          # min seconds between re-summaries of one session (a new prompt bypasses this)
BUSY_WINDOW_S = 60       # agy has no status file: busy = transcript written within this window
SETTLE_S = 5             # don't relabel a Claude session whose busy/idle status flipped this recently
TITLE_MAX = 48
LLM_TIMEOUT_S = 120

PROMPT = """Write a label for each AI coding session below: what it is doing **right now** and how far along it is.
Rules: in {lang}, at most 6 words / 40 characters, task + state, e.g. "Squash today's changes into PR · resolving conflicts",
"Grafana lag note · waiting on scan". Base it on the most recent user prompt; no repo name, no trailing punctuation.
Output only a JSON object {{"<key>": "<label>", ...}} using the key at the top of each section.

{body}
"""


def load_state():
    try:
        with open(STATE, encoding="utf-8", errors="replace") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE), prefix=".titles.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def live_sessions():
    """[{key, path, tty, d, target}] for every live session on a terminal, one per tty."""
    procs = digest.ps_table()
    since = digest.now() - dt.timedelta(hours=24)
    out = []
    for t in digest.cc_targets(since, True, procs):
        out.append({"key": "cc:" + t["session_id"], "path": t["path"], "tty": t["tty"], "d": digest.cc_digest(t, since), "t": t})
    for t in digest.agy_targets(since, True, procs):
        out.append({"key": "agy:" + t["id"], "path": digest.AGY_TRANSCRIPT.format(t["id"]), "tty": t["tty"], "d": digest.agy_digest(t, since), "t": t})
    # an agy process can hold several conversations; keep the most recently active one per tty
    by_tty, epoch = {}, digest.now() - dt.timedelta(days=365)
    for s in out:
        if s["d"]["prompts"] and not digest.is_summarizer(s["d"]) and (s["tty"] not in by_tty or (s["d"]["last"] or epoch) > (by_tty[s["tty"]]["d"]["last"] or epoch)):
            by_tty[s["tty"]] = s
    return list(by_tty.values())


def last_prompt_epoch(d):
    ts = d["prompts"][-1][0] if d["prompts"] else None
    return ts.timestamp() if ts else 0


def summarize(items, backend, model):
    """items: [(key, digest)] -> {key: title}. One batched call; {} on failure."""
    body = "\n\n".join(f"key: {k}\n" + digest.render(d, max_prompts=6) for k, d in items)
    ok, out = digest.llm(PROMPT.format(body=body, lang=digest.SUMMARY_LANG), backend, model, LLM_TIMEOUT_S)
    m = re.search(r"\{.*\}", out, re.S) if ok else None
    if not m:
        print(f"{time.ctime()} {out[:300] if not ok else backend + ' returned no JSON: ' + out[:300]}", file=sys.stderr)
        return {}
    try:
        res = json.loads(m.group(0))
    except ValueError:
        return {}
    return {k: v for k, v in res.items() if isinstance(v, str) and v.strip()} if isinstance(res, dict) else {}


def clean(title):
    title = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", title).replace(";", ",").strip()
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1] + "…"


def _win_write_console_inproc(pid, seq):
    """Attach to target process console on Windows and write an OSC sequence to CONOUT$."""
    import ctypes.wintypes as w

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.FreeConsole()
    if not k32.AttachConsole(w.DWORD(pid)):
        return False
    try:
        h = k32.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
        if h == w.HANDLE(-1).value:
            return False
        try:
            mode = w.DWORD()
            if k32.GetConsoleMode(h, ctypes.byref(mode)) and not (mode.value & 0x0004):
                k32.SetConsoleMode(h, mode.value | 0x0004)
            written = w.DWORD()
            ok = k32.WriteConsoleW(h, ctypes.c_wchar_p(seq), len(seq), ctypes.byref(written), None)
            return bool(ok)
        finally:
            k32.CloseHandle(h)
    finally:
        k32.FreeConsole()


def _win_has_console():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if k32.GetConsoleWindow():
        return True
    try:
        return bool((sys.stdout and sys.stdout.isatty()) or (sys.stderr and sys.stderr.isatty()))
    except Exception:
        return False


def write_tty(tty, seq):
    if IS_WIN:
        if not tty.startswith("pid:"):
            return False
        pid = int(tty.split(":", 1)[1])
        if _win_has_console():
            # Avoid detaching the user's interactive terminal when run manually.
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            r = subprocess.run([sys.executable, os.path.abspath(__file__), "--win-write-pid", str(pid)],
                               input=seq, text=True, encoding="utf-8", capture_output=True, creationflags=flags)
            return r.returncode == 0
        ok = _win_write_console_inproc(pid, seq)
        if not ok:
            print(f"{time.ctime()} write console {tty} failed", file=sys.stderr)
        return ok
    try:
        with open(f"/dev/{tty}", "w") as f:
            f.write(seq)
        return True
    except OSError as e:
        print(f"{time.ctime()} write /dev/{tty} failed: {e}", file=sys.stderr)
        return False


def warp_prompt_submit(tty, session_id, cwd, query):
    """Same payload Warp's Claude Code plugin sends on UserPromptSubmit (protocol v1)."""
    body = json.dumps({"v": 1, "agent": "claude", "event": "prompt_submit", "session_id": session_id, "cwd": cwd or "",
                       "project": os.path.basename(cwd or ""), "query": query}, ensure_ascii=False)
    return write_tty(tty, f"\033]777;notify;warp://cli-agent;{body}\007")


def cc_tail(path, nbytes=512 * 1024):
    """(epoch of the latest real user prompt, whether the latest assistant turn ended) from the transcript tail."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - nbytes))
            lines = f.read().decode("utf-8", "replace").splitlines()[1:]
    except OSError:
        return 0.0, False
    prompt_ts, ended, local = 0.0, False, False
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        prompt, local = digest.cc_prompt(e, local)
        if prompt:
            ts = digest.parse_ts((e.get("attachment") or {}).get("timestamp") or e.get("timestamp"))
            prompt_ts, ended = (ts.timestamp() if ts else prompt_ts), False
        elif e.get("type") == "assistant" and not e.get("isSidechain"):
            ended = (e.get("message") or {}).get("stop_reason") == "end_turn"
    return prompt_ts, ended


def cc_status_now(pid):
    """(status, seconds since it last changed) re-read right before sending, to avoid relabelling a session that just stopped."""
    try:
        with open(os.path.join(digest.CC_SESSIONS, f"{pid}.json"), encoding="utf-8", errors="replace") as f:
            m = json.load(f)
        return m.get("status"), time.time() - (m.get("statusUpdatedAt") or 0) / 1000
    except (OSError, ValueError):
        return None, 0


def run_once(model, agy_model, dry_run):
    state, now = load_state(), time.time()
    sessions = live_sessions()

    stale = []
    for s in sessions:
        st = state.get(s["key"], {})
        size = os.path.getsize(s["path"]) if os.path.exists(s["path"]) else 0
        new_prompt = last_prompt_epoch(s["d"]) > st.get("at", 0)
        grew = size != st.get("size") and now - st.get("at", 0) >= REFRESH_S
        if not st.get("title") or new_prompt or grew:
            stale.append((s, size))
    if stale:
        titles = {}
        for tool in ("cc", "agy"):  # each tool's sessions are summarized by that tool's own CLI
            items = [(s["key"], s["d"]) for s, _ in stale if s["key"].split(":")[0] == tool]
            if items:
                backend = digest.pick_backend("claude" if tool == "cc" else "agy")
                titles.update(summarize(items, backend, model if backend == "claude" else agy_model))
        for s, size in stale:
            if s["key"] in titles:
                # stamp with the snapshot time, not completion time: a prompt sent during the call must make this stale
                state[s["key"]] = {**state.get(s["key"], {}), "title": clean(titles[s["key"]]), "size": size, "at": now}

    for s in sessions:
        st = state.get(s["key"], {})
        title = st.get("title")
        if not title:
            continue
        if s["key"].startswith("agy:"):
            busy = time.time() - os.path.getmtime(s["path"]) < BUSY_WINDOW_S if os.path.exists(s["path"]) else False
            label = f"{'●' if busy else '○'} {title}"
            if dry_run:
                print(f"{s['tty']}\ttitle\t{label}")
            else:
                write_tty(s["tty"], f"\033]0;{label}\007")  # rewrite every run; harmless, survives title resets
            continue
        # Claude Code: only while busy, only if the summary is newer than the latest prompt, only when it changed.
        status, since_change = cc_status_now(s["t"]["pid"])
        prompt_ts, turn_ended = cc_tail(s["path"])
        fresh = st.get("at", 0) > max(prompt_ts, last_prompt_epoch(s["d"]))
        if status != "busy" or since_change < SETTLE_S or not fresh or turn_ended:
            if status != "busy":
                st.pop("sent", None)  # next busy period starts with the plugin's own prompt label
            continue
        if dry_run:
            print(f"{s['tty']}\tprompt_submit\t{title}")
        elif title != st.get("sent"):
            warp_prompt_submit(s["tty"], s["t"]["session_id"], s["t"].get("session_cwd"), title) and st.update(sent=title)

    live_keys = {s["key"] for s in sessions}
    state = {k: v for k, v in state.items() if k in live_keys or now - v.get("at", 0) < 86400}
    if not dry_run:
        save_state(state)


def _claim_pidfile():
    os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
    with open(PIDFILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="haiku", help="claude model for Claude Code sessions")
    ap.add_argument("--agy-model", default="gemini-3.8-flash-medium", help="agy model for agy sessions (see `agy models`)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent, don't write to ttys")
    ap.add_argument("--lang", default="", help="override DIGEST_LANG")
    ap.add_argument("--loop", type=float, default=0, help="run continuously every N seconds (used by Windows Scheduled Task)")
    ap.add_argument("--log", default="", help="append stdout/stderr to this log file")
    ap.add_argument("--win-write-pid", type=int, default=0, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.win_write_pid:
        seq = sys.stdin.read()
        sys.exit(0 if _win_write_console_inproc(args.win_write_pid, seq) else 1)

    if args.log:
        os.makedirs(os.path.dirname(args.log), exist_ok=True)
        logf = open(args.log, "a", encoding="utf-8", buffering=1)
        sys.stdout = logf
        sys.stderr = logf

    if args.lang:
        digest.SUMMARY_LANG = args.lang

    if args.loop > 0:
        _claim_pidfile()
        while True:
            try:
                run_once(args.model, args.agy_model, args.dry_run)
            except Exception as e:
                print(f"{time.ctime()} error: {e}", file=sys.stderr)
            time.sleep(args.loop)
    else:
        run_once(args.model, args.agy_model, args.dry_run)


if __name__ == "__main__":
    main()
