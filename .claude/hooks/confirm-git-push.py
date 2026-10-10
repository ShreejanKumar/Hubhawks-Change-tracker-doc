# PreToolUse(Bash|PowerShell): never push without the user's say-so.
# Returns "ask" rather than "deny", so every push stops at a permission prompt
# the user approves or rejects. Pushing to main also redeploys Streamlit Cloud.
import json
import re
import sys

data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
command = str((data.get("tool_input") or {}).get("command") or "")

# `git`, then any global options (`-C dir`, `-c key=val`, `--no-pager`), then `push`.
push = re.compile(r"(^|[^\w-])git(\.exe)?(\s+(-[Cc]\s+\S+|--?[\w-]+(=\S+)?))*\s+push(\s|$|;|&|\|)", re.M)

if push.search(command):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "git push needs the user's explicit approval every time (pushing main redeploys the app).",
        }
    }))
