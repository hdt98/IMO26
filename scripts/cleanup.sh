#!/bin/bash
# List or terminate this user's IMO26 orchestrators.
# Usage:
#   bash scripts/cleanup.sh
#   bash scripts/cleanup.sh <pid>
#   bash scripts/cleanup.sh <exact-run-dir>

set -u

TARGET="${1:-}"
CURRENT_UID="$(id -u)"

is_owned_orchestrator() {
    local pid="$1"
    local process_uid
    local command

    process_uid="$(ps -p "$pid" -o uid= 2>/dev/null | tr -d '[:space:]')"
    [ "$process_uid" = "$CURRENT_UID" ] || return 1

    command="$(ps -p "$pid" -o command= 2>/dev/null)"
    case "$command" in
        *code/orchestrator.py*) return 0 ;;
        *) return 1 ;;
    esac
}

list_orchestrators() {
    local found=0
    local pid

    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        if is_owned_orchestrator "$pid"; then
            ps -p "$pid" -o pid=,etime=,command=
            found=1
        fi
    done < <(pgrep -f 'code/orchestrator\.py' 2>/dev/null || true)

    if [ "$found" -eq 0 ]; then
        echo "  (none)"
    fi
}

terminate_pid() {
    local pid="$1"

    if ! is_owned_orchestrator "$pid"; then
        echo "Refusing: PID $pid is not this user's IMO26 orchestrator." >&2
        return 1
    fi

    kill -TERM "$pid"
    echo "Sent TERM to orchestrator PID $pid."
}

if [ -z "$TARGET" ]; then
    echo "Active IMO26 orchestrators:"
    list_orchestrators
    exit 0
fi

if [[ "$TARGET" =~ ^[0-9]+$ ]]; then
    terminate_pid "$TARGET"
    exit $?
fi

matched=0
while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    if ! is_owned_orchestrator "$pid"; then
        continue
    fi
    command="$(ps -p "$pid" -o command= 2>/dev/null)"
    if [[ " $command " == *" --run-dir $TARGET "* ]] \
        || [[ " $command " == *" --run-dir=$TARGET "* ]]; then
        terminate_pid "$pid"
        matched=1
    fi
done < <(pgrep -f 'code/orchestrator\.py' 2>/dev/null || true)

if [ "$matched" -eq 0 ]; then
    echo "No owned orchestrator found for exact run directory: $TARGET"
fi
