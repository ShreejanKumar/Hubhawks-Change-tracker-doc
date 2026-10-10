# PostToolUse(Edit|Write): compile the .py file just written, so a syntax
# error shows up at once instead of costing a failed run later. Files outside
# the repo (memory, scratchpad) are skipped. Never blocks the edit itself.
import json
import os
import sys
from pathlib import Path

data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
file = (data.get("tool_response") or {}).get("filePath") or (data.get("tool_input") or {}).get("file_path")
root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()

if file and file.endswith(".py"):
    path = Path(file).resolve()
    if root in path.parents and path.exists():
        try:
            # compile() in memory: nothing is written, so no stray __pycache__.
            compile(path.read_bytes(), str(path), "exec")
        except SyntaxError as exc:
            print(f"Syntax error in {path.relative_to(root)} line {exc.lineno}: {exc.msg}", file=sys.stderr)
            sys.exit(2)
