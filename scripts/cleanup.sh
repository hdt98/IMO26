#!/bin/bash
# List and optionally kill stale imo26 orchestrator processes.
# Usage: bash scripts/cleanup.sh              # List all active processes
# Usage: bash scripts/cleanup.sh <pid>        # Kill a specific PID
# Usage: bash scripts/cleanup.sh <run-dir>    # Kill processes for a specific run directory

TARGET="${1:-}"

if [ -z "$TARGET" ]; then
    echo "=== Active orchestrator processes ==="
    ps -eo pid,etime,command | grep 'orchestrator.py.*imo2026' | grep -v grep || echo "  (none)"
    echo ""
    echo "=== Active screen sessions (imo_) ==="
    screen -ls 2>/dev/null | grep 'imo_' || echo "  (none)"
    echo ""
    echo "=== Active monitoring loops ==="
    ps -eo pid,command | grep 'while true.*imo26' | grep -v grep || echo "  (none)"
    echo ""
    echo "To kill a specific process: bash scripts/cleanup.sh <pid>"
    echo "To kill processes for a run dir: bash scripts/cleanup.sh <run-dir>"
elif [[ "$TARGET" =~ ^[0-9]+$ ]]; then
    echo "Killing PID $TARGET..."
    kill "$TARGET" 2>/dev/null && echo "  Killed" || echo "  Failed or already gone"
else
    echo "Killing processes for run directory: $TARGET"
    pkill -f "orchestrator.py.*${TARGET}" 2>/dev/null && echo "  Killed orchestrator processes" || true
    pkill -f "while true.*${TARGET}" 2>/dev/null && echo "  Killed monitoring loops" || true
fi

echo "Done."
