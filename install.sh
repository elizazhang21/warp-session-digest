#!/bin/sh
# Install the Warp workflow, pointing it at this checkout.
# Optional: DIGEST_EXTRA_CMDS='mycli deploy|ship\.sh' ./install.sh  bakes extra notable-command patterns into the workflow.
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.warp/workflows"
REPO="$REPO" python3 - <<'PY'
import json, os, re, shlex
repo, extra, llm_cmd = os.environ["REPO"], os.environ.get("DIGEST_EXTRA_CMDS", ""), os.environ.get("DIGEST_LLM_CMD", "").strip()
if extra:
    re.compile(extra)  # fail at install time, not inside Warp
prefix = (f"DIGEST_EXTRA_CMDS={shlex.quote(extra)} " if extra else "") + (f"DIGEST_LLM_CMD={shlex.quote(llm_cmd)} " if llm_cmd else "")
cmd = prefix + f"python3 {shlex.quote(repo + '/digest.py')} --scope {{{{scope}}}}"
src = open(f"{repo}/workflows/ai-session-digest.yaml").read()
src = re.sub(r"(?m)^command: .*$", lambda _: "command: " + json.dumps(cmd, ensure_ascii=False), src)
dst = os.path.expanduser("~/.warp/workflows/ai-session-digest.yaml")
open(dst, "w").write(src)
print(f"installed: {dst} -> {repo}/digest.py")
PY
