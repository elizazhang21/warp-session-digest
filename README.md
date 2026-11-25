# warp-session-digest

Summarize what your terminal AI coding sessions (Claude Code and agy / Antigravity CLI) are doing — one Warp workflow, one report: what each session is working on, progress, status, and what needs you.

## Install

```sh
git clone https://github.com/elizazhang21/warp-session-digest && cd warp-session-digest && ./install.sh
```

Then in Warp: `Ctrl+R` (Command Search) or `Cmd+P` (Command Palette) → "总结 AI 会话". Restart Warp if it doesn't show up.

Requires Python 3.9+, the `claude` CLI (summarization step, `claude -p`), and `ps`/`lsof` (`live` scope). Tested on macOS.

## Usage

```sh
python3 digest.py --scope live     # sessions attached to a terminal right now (default)
python3 digest.py --scope today    # everything active since 06:00
python3 digest.py --scope 8h       # last 8 hours
python3 digest.py --raw            # print the extracted digest only, no LLM call
python3 digest.py --tool agy       # only agy (or: claude, all)
python3 digest.py --model haiku    # model for claude -p (default sonnet)
DIGEST_EXTRA_CMDS="mycli deploy" python3 digest.py   # extra commands to surface (regex); ./install.sh bakes it into the workflow
```

## How it works

Reads local session files only (read-only):

| Tool | Live session mapping | Transcript |
|---|---|---|
| Claude Code | `~/.claude/sessions/<pid>.json` (pid → sessionId) | `~/.claude/projects/*/<sessionId>.jsonl` |
| agy | `lsof` on the agy process → open `brain/<conversationId>` dirs | `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl` |

For each session it extracts user prompts, edited files, notable commands (git / MR / deploy), and the last replies, then asks `claude -p` for a summary. Skipped: the session that launched the script, headless SDK sessions, subagents, sessions with a single short prompt and no edits/commands (health-check probes), and — in `today`/`Nh` — Claude Code sessions whose cwd is under `/private/tmp` or `/private/var` (scratch worktrees).
