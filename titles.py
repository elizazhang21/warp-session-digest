#!/usr/bin/env python3
"""Make Warp's session list show a one-line summary of what each live Claude Code / agy session is doing.

Run periodically (launchd, see install-titles.sh). Each run finds live sessions, re-summarizes the ones that
changed (one batched `claude -p` call), and pushes the summary to Warp:
  agy          -> terminal title (OSC 0); Warp lists non-agent terminals by title.
  Claude Code  -> Warp lists it by the `query` of its warp://cli-agent events, not the title. While the session
                  is busy we send a `prompt_submit` event (keeps status "working", no notification). Idle sessions
                  can only be relabelled by a `stop` event, which notifies — so that is left to the forked plugin's
                  Stop hook (claude-plugin/), which reads the summary from STATE.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import digest  # noqa: E402

STATE = os.environ.get("WARP_DIGEST_STATE") or os.path.expanduser("~/.cache/warp-session-digest/titles.json")
REFRESH_S = 180          # min seconds between re-summaries of one session (a new prompt bypasses this)
BUSY_WINDOW_S = 60       # agy has no status file: busy = transcript written within this window
SETTLE_S = 5             # don't relabel a Claude session whose busy/idle status flipped this recently
TITLE_MAX = 48
CLAUDE_TIMEOUT_S = 120

PROMPT = """Write a label for each AI coding session below: what it is doing **right now** and how far along it is.
Rules: in {lang}, at most 6 words / 40 characters, task + state, e.g. "Squash today's changes into PR · resolving conflicts",
"Grafana lag note · waiting on scan". Base it on the most recent user prompt; no repo name, no trailing punctuation.
Output only a JSON object {{"<key>": "<label>", ...}} using the key at the top of each section.

{body}
"""


def load_state():
    try:
        s = json.load(open(STATE))
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE), prefix=".titles.")
    with os.fdopen(fd, "w") as f:
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
        if s["d"]["prompts"] and (s["tty"] not in by_tty or (s["d"]["last"] or epoch) > (by_tty[s["tty"]]["d"]["last"] or epoch)):
            by_tty[s["tty"]] = s
    return list(by_tty.values())


def last_prompt_epoch(d):
    ts = d["prompts"][-1][0] if d["prompts"] else None
    return ts.timestamp() if ts else 0


def summarize(items, model):
    """items: [(key, digest)] -> {key: title}. One batched call; {} on failure."""
    claude = shutil.which("claude") or "/opt/homebrew/bin/claude"
    body = "\n\n".join(f"key: {k}\n" + digest.render(d, max_prompts=6) for k, d in items)
    cmd = [claude, "-p", "--model", model, "--no-session-persistence", "--tools", "", "--setting-sources", ""]
    try:
        r = subprocess.run(cmd, input=PROMPT.format(body=body, lang=digest.SUMMARY_LANG), capture_output=True, text=True, timeout=CLAUDE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(f"{time.ctime()} claude -p timed out", file=sys.stderr)
        return {}
    except OSError as e:
        print(f"{time.ctime()} cannot run claude: {e}", file=sys.stderr)
        return {}
    m = re.search(r"\{.*\}", r.stdout, re.S)
    if r.returncode != 0 or not m:
        print(f"{time.ctime()} claude -p failed exit={r.returncode}: {(r.stderr or r.stdout)[:300]}", file=sys.stderr)
        return {}
    try:
        res = json.loads(m.group(0))
    except ValueError:
        return {}
    return {k: v for k, v in res.items() if isinstance(v, str) and v.strip()} if isinstance(res, dict) else {}


def clean(title):
    title = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", title).replace(";", ",").strip()
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1] + "…"


def write_tty(tty, seq):
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
    prompt_ts, ended = 0.0, False
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("isSidechain") or e.get("isMeta") or e.get("isCompactSummary"):
            continue
        content = (e.get("message") or {}).get("content")
        if e.get("type") == "user":
            texts = [content] if isinstance(content, str) else [b.get("text") or "" for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
            if any(t.strip() and not t.lstrip().startswith(digest.NOISE_PREFIX) for t in texts):
                ts = digest.parse_ts(e.get("timestamp"))
                prompt_ts, ended = (ts.timestamp() if ts else prompt_ts), False
        elif e.get("type") == "assistant":
            ended = (e.get("message") or {}).get("stop_reason") == "end_turn"
    return prompt_ts, ended


def cc_status_now(pid):
    """(status, seconds since it last changed) re-read right before sending, to avoid relabelling a session that just stopped."""
    try:
        m = json.load(open(f"{digest.CC_SESSIONS}/{pid}.json"))
        return m.get("status"), time.time() - (m.get("statusUpdatedAt") or 0) / 1000
    except (OSError, ValueError):
        return None, 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent, don't write to ttys")
    args = ap.parse_args()

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
        titles = summarize([(s["key"], s["d"]) for s, _ in stale], args.model)
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
            if args.dry_run:
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
        if args.dry_run:
            print(f"{s['tty']}\tprompt_submit\t{title}")
        elif title != st.get("sent"):
            warp_prompt_submit(s["tty"], s["t"]["session_id"], s["t"].get("session_cwd"), title) and st.update(sent=title)

    live_keys = {s["key"] for s in sessions}
    state = {k: v for k, v in state.items() if k in live_keys or now - v.get("at", 0) < 86400}
    if not args.dry_run:
        save_state(state)


if __name__ == "__main__":
    main()
