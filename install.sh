#!/bin/sh
# Install the Warp workflow, pointing it at this checkout.
# Optional: DIGEST_EXTRA_CMDS='mycli deploy|ship\.sh' ./install.sh  bakes extra notable-command patterns into the workflow.
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.warp/workflows"
REPO="$REPO" python3 - <<'PY'
import json, os, re, shlex
repo, extra = os.environ["REPO"], os.environ.get("DIGEST_EXTRA_CMDS", "")
if extra:
    re.compile(extra)  # fail at install time, not inside Warp
cmd = (f"DIGEST_EXTRA_CMDS={shlex.quote(extra)} " if extra else "") + f"python3 {shlex.quote(repo + '/digest.py')} --scope {{{{scope}}}}"
src = open(f"{repo}/workflows/ai-session-digest.yaml").read()
src = re.sub(r"(?m)^command: .*$", lambda _: "command: " + json.dumps(cmd, ensure_ascii=False), src)
dst = os.path.expanduser("~/.warp/workflows/ai-session-digest.yaml")
open(dst, "w").write(src)
print(f"installed: {dst} -> {repo}/digest.py")
PY
