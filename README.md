# warp-session-digest

Make Warp's session list say what each AI coding session (Claude Code, agy / Antigravity CLI) is actually doing, instead of its last prompt. Also includes an on-demand report across all sessions.

## Install

### macOS

```sh
git clone https://github.com/elizazhang21/warp-session-digest
cd warp-session-digest
./install-titles.sh   # session-list labels (launchd agent + Claude Code plugin)
./install.sh          # optional: on-demand report as a Warp workflow
```

### Windows (PowerShell)

```powershell
git clone https://github.com/elizazhang21/warp-session-digest
cd warp-session-digest
.\install-titles.ps1  # session-list labels (Scheduled Task + Claude Code plugin)
.\install.ps1         # optional: on-demand report as a Warp workflow
```

Requires macOS or Windows, Python 3.9+, and the CLI of each tool you use: Claude Code sessions are summarized with `claude -p`, agy sessions with `agy acp` (if only one CLI is installed, it summarizes both). On macOS, `ps` and `lsof` are used for live session discovery; on Windows, Win32 Toolhelp, Restart Manager, and ConPTY `AttachConsole` are used natively (no extra tools required).

Summaries are in English by default. For another language, run `DIGEST_LANG=Japanese ./install-titles.sh` (or `$env:DIGEST_LANG="Japanese"; .\install-titles.ps1` on Windows) and set `DIGEST_LANG` in your shell for `digest.py`. Then remove `~/.cache/warp-session-digest/titles.json` so existing labels are regenerated.

## Session-list labels

`install-titles.sh` installs a launchd agent that runs `titles.py` every 30s. Each run finds live sessions, re-summarizes the ones that changed (one batched call per tool: `claude -p --model haiku` for Claude Code, `agy acp` with `gemini-3.8-flash-medium` for agy; change with `--model` / `--agy-model`) and caches the labels in `~/.cache/warp-session-digest/titles.json`.

How the label reaches Warp depends on the tool:

| Session | Warp shows | How it's set |
|---|---|---|
| agy | the terminal title | `titles.py` writes an OSC 0 title: `● label` (busy) / `○ label` (idle) |
| Claude Code, busy | the `query` of its latest `warp://cli-agent` event | `titles.py` sends a `prompt_submit` event with the label (no notification, status stays "working") |
| Claude Code, idle | same | the forked Warp plugin's Stop hook puts the label in the completion notification, if the label is newer than the last prompt |

The plugin in `claude-plugin/` is a fork of [warpdotdev/claude-code-warp](https://github.com/warpdotdev/claude-code-warp) 2.4.0 (MIT); only `scripts/on-stop.sh` is changed. The installer replaces the official `warp@claude-code-warp` plugin with `warp-digest@warp-session-digest`. Sessions that are already open need `/reload-plugins`.

A new prompt shows as-is first; the label replaces it once a fresh summary is ready (usually within a minute). Turns shorter than that end with the prompt as the label.

Uninstall (macOS):

```sh
launchctl bootout gui/$(id -u)/com.user.warp-session-titles
rm ~/Library/LaunchAgents/com.user.warp-session-titles.plist
claude plugin uninstall warp-digest@warp-session-digest
claude plugin enable warp@claude-code-warp
```

Uninstall (Windows):

```powershell
.\uninstall-titles.ps1
```

## On-demand report

After `./install.sh`, open Warp's Command Search (`Ctrl+R`) and search for "Summarize AI sessions" (also in the Command Palette, `Cmd+P`; restart Warp if it doesn't show up). Or run it directly:

```sh
python3 digest.py                  # sessions attached to a terminal right now (default scope: live)
python3 digest.py --scope today    # everything active since 06:00
python3 digest.py --scope 8h       # last 8 hours
python3 digest.py --raw            # print the extracted digest only, no LLM call
python3 digest.py --tool agy       # only agy (or: claude, all)
python3 digest.py --backend agy    # who writes the report: auto (default; agy if every session is agy, else claude), claude, agy
python3 digest.py --model haiku    # model when claude writes the report (default sonnet); --agy-model for agy (default: agy's configured model)
DIGEST_EXTRA_CMDS="mycli deploy" python3 digest.py   # extra commands to surface (regex); ./install.sh bakes it into the workflow
```

The report covers, per session: goal, progress, status, plus a final "Needs me" list.

## How it works

Reads local session files only:

| Tool | Live session mapping | Transcript |
|---|---|---|
| Claude Code | `~/.claude/sessions/<pid>.json` (pid → sessionId, status) | `~/.claude/projects/*/<sessionId>.jsonl` |
| agy | `lsof` on the agy process → open `brain/<conversationId>` dirs | `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl` |

For each session it extracts user prompts, edited files, notable commands (git / PR / deploy) and the last replies, then asks `claude -p` or `agy acp` for a summary. `agy acp` runs with an isolated `--gemini-dir` / `--app-data-dir` under `~/.cache/warp-session-digest/agy` that is deleted after the call, so summarizer turns never touch `~/.gemini/antigravity-cli` or show up in agy's conversation history, and any tool permission request is cancelled.

Skipped: the session that launched the script, headless SDK sessions, subagents, sessions with a single short prompt and no edits or commands (health-check probes), and, in `today` / `Nh` scope, Claude Code sessions whose cwd is under `/private/tmp` or `/private/var` (scratch worktrees).
