# warp-session-digest

Make Warp's session list say what each AI coding session (Claude Code, agy / Antigravity CLI) is actually doing — instead of its last prompt — plus an on-demand report across all sessions.

## Session-list labels

```sh
./install-titles.sh
```

- Installs a launchd agent running `titles.py` every 30s: finds live sessions, re-summarizes changed ones (one batched `claude -p --model haiku` call), caches them in `~/.cache/warp-session-digest/titles.json`.
- **agy**: label = terminal title (`● busy` / `○ idle`).
- **Claude Code**: Warp labels it with the `query` of its `warp://cli-agent` events, not the title. While busy, `titles.py` sends a `prompt_submit` event with the summary (no notification). When a turn ends, the forked Warp plugin in `claude-plugin/` (fork of [warpdotdev/claude-code-warp](https://github.com/warpdotdev/claude-code-warp) 2.4.0, MIT; only `on-stop.sh` changed) puts the summary in the completion notification — but only if it was generated after the last prompt.
- The installer swaps the official `warp@claude-code-warp` plugin for `warp-digest@warp-session-digest`. Open Claude sessions need `/reload-plugins`.
- Uninstall: `launchctl bootout gui/$(id -u)/com.user.warp-session-titles`, delete `~/Library/LaunchAgents/com.user.warp-session-titles.plist`, `claude plugin uninstall warp-digest@warp-session-digest`, `claude plugin enable warp@claude-code-warp`.

## On-demand report

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
