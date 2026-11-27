# Install the Warp workflow on Windows, pointing it at this checkout.
# Optional: $env:DIGEST_EXTRA_CMDS='mycli deploy|ship\.sh'; .\install.ps1
$ErrorActionPreference = "Stop"

$Repo = $PSScriptRoot
$PyCmd = Get-Command python3 -ErrorAction SilentlyContinue
if (-not $PyCmd) { $PyCmd = Get-Command python -ErrorAction SilentlyContinue }
if (-not $PyCmd) { throw "Python 3.9+ not found in PATH." }

$WorkflowDir = Join-Path $env:APPDATA "warp\Warp\data\workflows"
New-Item -ItemType Directory -Force -Path $WorkflowDir | Out-Null

$env:REPO = $Repo
$env:WORKFLOW_DST = Join-Path $WorkflowDir "ai-session-digest.yaml"
@'
import json, os, re

repo = os.environ["REPO"]
dst = os.environ["WORKFLOW_DST"]
extra = os.environ.get("DIGEST_EXTRA_CMDS", "")
if extra:
    re.compile(extra)  # fail at install time, not inside Warp
digest_py = os.path.join(repo, "digest.py")
extra_arg = f' --extra-cmds "{extra}"' if extra else ""
cmd = f'python3 "{digest_py}"{extra_arg} --scope {{{{scope}}}}'
with open(os.path.join(repo, "workflows", "ai-session-digest.yaml"), encoding="utf-8") as fh:
    src = fh.read()
src = re.sub(r"(?m)^command: .*$", lambda _: "command: " + json.dumps(cmd, ensure_ascii=False), src)
with open(dst, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(src)
print(f"installed: {dst} -> {digest_py}")
'@ | & $PyCmd.Source -
