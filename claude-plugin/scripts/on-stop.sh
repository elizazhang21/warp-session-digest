#!/bin/bash
# Hook script for Claude Code Stop event
# Sends a structured Warp notification when Claude completes a task

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/should-use-structured.sh"

# Legacy fallback for old Warp versions
if ! should_use_structured; then
    [ "$TERM_PROGRAM" = "WarpTerminal" ] && exec "$SCRIPT_DIR/legacy/on-stop.sh"
    exit 0
fi

source "$SCRIPT_DIR/build-payload.sh"
source "$SCRIPT_DIR/digest-label.sh"

# Read hook input from stdin
INPUT=$(cat)

# Skip if a stop hook is already active (prevents double-notification)
STOP_HOOK_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null)
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
    exit 0
fi

# Extract the last user prompt and assistant response from the transcript.
# Small delay to allow Claude Code to flush the current turn to the transcript file.
# The Stop hook fires before the transcript is fully written.
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)
sleep 0.3
QUERY=""
RESPONSE=""
if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
    # [warp-digest] Last prompt the user actually typed (upstream took the last "user" turn, which is
    # often an injected <task-notification> / <system-reminder>), plus the last reply, in one pass.
    FACTS=$(transcript_facts "$TRANSCRIPT_PATH")
    QUERY=$(echo "$FACTS" | jq -r '.text // empty' 2>/dev/null)
    RESPONSE=$(echo "$FACTS" | jq -r '.response // empty' 2>/dev/null)
    LAST_PROMPT_TS=$(echo "$FACTS" | jq -r '.ts // empty' 2>/dev/null)

    # Truncate for notification display
    if [ -n "$QUERY" ] && [ ${#QUERY} -gt 200 ]; then
        QUERY="${QUERY:0:197}..."
    fi
    if [ -n "$RESPONSE" ] && [ ${#RESPONSE} -gt 200 ]; then
        RESPONSE="${RESPONSE:0:197}..."
    fi

    # [warp-digest] Show the session summary (written by titles.py) instead of the prompt,
    # but only if it was generated after the last prompt — otherwise it describes an older task.
    SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
    if [ -n "$LAST_PROMPT_TS" ]; then
        SUMMARY=$(cached_summary "$SESSION_ID" "$LAST_PROMPT_TS")
        [ -n "$SUMMARY" ] && QUERY="$SUMMARY"
    fi
fi

# Claude Code wakes the agent again when a background task finishes or a session cron fires,
# so a Stop with either pending is a pause rather than the end of the session. Default to 0
# so --argjson always receives a number and the notification is never dropped.
BACKGROUND_TASK_COUNT=$(echo "$INPUT" | jq -r '.background_tasks // [] | length' 2>/dev/null)
SESSION_CRON_COUNT=$(echo "$INPUT" | jq -r '.session_crons // [] | length' 2>/dev/null)

BODY=$(build_payload "$INPUT" "stop" \
    --arg query "$QUERY" \
    --arg response "$RESPONSE" \
    --arg transcript_path "$TRANSCRIPT_PATH" \
    --argjson background_task_count "${BACKGROUND_TASK_COUNT:-0}" \
    --argjson session_cron_count "${SESSION_CRON_COUNT:-0}")

"$SCRIPT_DIR/warp-notify.sh" "warp://cli-agent" "$BODY"
