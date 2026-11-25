#!/bin/bash
# [warp-digest] Helpers for choosing the label Warp shows for a Claude Code session.
# Source this file. Requires jq.

DIGEST_STATE_FILE="${WARP_DIGEST_STATE:-$HOME/.cache/warp-session-digest/titles.json}"

# Messages Claude Code injects as "user" turns (background-task results, reminders, ! shell output, ...).
# Keep in sync with NOISE_PREFIX in digest.py.
DIGEST_NOISE_RE='^\s*(<task-notification|<system-reminder|<local-command|<bash-input|<bash-stdout|<bash-stderr|Caveat: The messages below|\[Request interrupted|This session is being continued)'

# jq defs shared by the helpers below.
#   slash_cmd: "<command-message>..<command-name>/x</command-name><command-args>a</command-args>" -> "/x a"
#   prompt_text: the text a user turn carries, or "" if it is injected noise or a local command (/model, /clear, ...)
#   epoch: ISO timestamp -> epoch seconds with fraction, null if unparseable
DIGEST_JQ_DEFS='
def slash_cmd:
  if test("<command-name>") then
    ([capture("<command-name>(?<n>[^<]*)</command-name>")][0].n + " "
     + ([capture("<command-args>(?<a>(.|\\n)*?)</command-args>")][0].a // "")) | sub("\\s+$"; "")
  else . end;
def prompt_text($re):
  [ if type == "string" then . else (.[]? | select(.type == "text") | .text // "") end ]
  | map(select(length > 0 and (test($re) | not)
               and ((test("<command-name>") and (test("<command-message>") | not)) | not))
        | slash_cmd)
  | join(" ");
def epoch:
  try ((capture("^(?<s>[^.Z+]+)(?<f>\\.[0-9]+)?") as $m
        | ($m.s + "Z" | fromdateiso8601) + (($m.f // ".0") | tonumber))) catch null;
'

# readable_prompt TEXT -> TEXT with slash-command markup collapsed
readable_prompt() {
    printf '%s' "$1" | jq -Rsr "$DIGEST_JQ_DEFS"' slash_cmd' 2>/dev/null || printf '%s' "$1"
}

# is_noise_prompt TEXT -> exit 0 if TEXT is an injected message rather than something the user typed
is_noise_prompt() {
    printf '%s' "$1" | jq -Rse --arg re "$DIGEST_NOISE_RE" 'test($re)' >/dev/null 2>&1
}

# transcript_facts TRANSCRIPT -> JSON {"ts": <epoch|null>, "text": "<last typed prompt>", "response": "<last reply>"}
# One streaming pass; unparseable lines are skipped. Prompts typed mid-turn are only recorded as
# queued_command attachments, so those count too.
transcript_facts() {
    jq -cRn --arg re "$DIGEST_NOISE_RE" "$DIGEST_JQ_DEFS"'
        reduce (inputs | fromjson? | select(type == "object")) as $e ({ts: null, text: "", response: "", local: false};
          if $e.type == "user" and $e.isMeta and ($e.message.content | tostring | test("^\\s*<local-command-caveat")) then
            # a local command (/model, /clear, /reload-plugins, ...) follows: it is not a task
            .local = true
          elif $e.type == "user" and ($e.isMeta | not) and ($e.isSidechain | not) and ($e.isCompactSummary | not) then
            ($e.message.content | prompt_text($re)) as $t
            | ($e.message.content | tostring | test("<command-name>")) as $is_cmd
            | if .local and $is_cmd then .local = false
              elif ($t | length) > 0 then .text = $t | .ts = ($e.timestamp | epoch) | .local = false
              else .local = false end
          elif $e.type == "attachment" and $e.attachment.type == "queued_command" and $e.attachment.commandMode == "prompt" then
            ($e.attachment.prompt | prompt_text($re)) as $t
            | if ($t | length) > 0 then .text = $t | .ts = (($e.attachment.timestamp // $e.timestamp) | epoch) else . end
          elif $e.type == "assistant" then
            ([$e.message.content[]? | select(.type == "text") | .text] | join(" ")) as $r
            | if ($r | length) > 0 then .response = $r else . end
          else . end) | del(.local)
    ' "$1" 2>/dev/null
}

# cached_summary SESSION_ID [NEWER_THAN_EPOCH] -> the titles.py label, if any (and newer than the given time)
cached_summary() {
    [ -n "$1" ] && [ -f "$DIGEST_STATE_FILE" ] || return 0
    jq -r --arg k "cc:$1" --argjson t "${2:-0}" \
        'if type == "object" then .[$k] | select(. != null and (.at // 0) > $t) | .title // empty else empty end' \
        "$DIGEST_STATE_FILE" 2>/dev/null
}
