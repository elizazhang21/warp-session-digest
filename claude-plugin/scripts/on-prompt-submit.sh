#!/bin/bash
# Hook script for Claude Code UserPromptSubmit event
# Sends a structured Warp notification when the user submits a prompt,
# transitioning the session status from idle/blocked back to running.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/should-use-structured.sh"

# No legacy equivalent for this hook
if ! should_use_structured; then
    exit 0
fi

source "$SCRIPT_DIR/build-payload.sh"
source "$SCRIPT_DIR/digest-label.sh"

# Read hook input from stdin
INPUT=$(cat)

# Extract the user's prompt
QUERY=$(echo "$INPUT" | jq -r '.prompt // empty' 2>/dev/null)

# [warp-digest] Injected turns (background-task results etc.) also fire this hook. Don't let them become
# the label: keep the session summary, or fall back to the last prompt the user actually typed.
if is_noise_prompt "$QUERY"; then
    SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
    TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)
    FACTS=""
    [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ] && FACTS=$(transcript_facts "$TRANSCRIPT_PATH")
    LAST_PROMPT_TS=$(echo "$FACTS" | jq -r '.ts // empty' 2>/dev/null)
    LABEL=""
    [ -n "$LAST_PROMPT_TS" ] && LABEL=$(cached_summary "$SESSION_ID" "$LAST_PROMPT_TS")
    [ -z "$LABEL" ] && LABEL=$(echo "$FACTS" | jq -r '.text // empty' 2>/dev/null)
    # nothing better known: keep upstream behaviour rather than sending an empty label
    QUERY="${LABEL:-$(readable_prompt "$QUERY")}"
else
    QUERY=$(readable_prompt "$QUERY")
fi

if [ -n "$QUERY" ] && [ ${#QUERY} -gt 200 ]; then
    QUERY="${QUERY:0:197}..."
fi

BODY=$(build_payload "$INPUT" "prompt_submit" \
    --arg query "$QUERY")

"$SCRIPT_DIR/warp-notify.sh" "warp://cli-agent" "$BODY"
