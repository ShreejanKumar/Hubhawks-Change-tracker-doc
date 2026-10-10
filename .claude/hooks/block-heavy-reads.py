# PreToolUse(Read): refuse files that waste tokens or must stay private.
# PDFs are the original style guides (the .txt files are the source of truth),
# .docx is zipped XML (use the proofread-run skill's changes.py), and the
# secrets must never enter the conversation.
import json
import sys
from pathlib import PurePath

data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
path = PurePath(str((data.get("tool_input") or {}).get("file_path") or ""))
name = path.name.lower()

reason = None
if path.suffix.lower() == ".pdf":
    reason = "PDFs are huge to read. The style guide source of truth is style_guides/*.txt; grep that instead."
elif path.suffix.lower() in (".docx", ".doc"):
    reason = ("A .docx is zipped XML. Use: conda run -n book python "
              ".claude/skills/proofread-run/scripts/changes.py <file> [--para/--grep/--all]")
elif name in ("secrets.toml", "gcp-service-account.json") or name.endswith(".json.key"):
    reason = "Secrets must not be read into the conversation. Check keys exist without printing values."

if reason:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Blocked by a local hook: {reason}",
        }
    }))
