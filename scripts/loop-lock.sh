#!/bin/sh
# Single-session lock for the improvement loop.
# Usage: scripts/loop-lock.sh acquire|release|status
# The lock is an atomic mkdir inside .git (never committed). A lock older than
# STALE_SECONDS is treated as abandoned (a crashed session) and taken over.
set -u
LOCK="$(git -C "$(dirname "$0")/.." rev-parse --absolute-git-dir)/db4ai-loop.lock"
STALE_SECONDS=${STALE_SECONDS:-14400}   # 4 hours

take() {
  mkdir "$LOCK" 2>/dev/null || return 1
  date +%s > "$LOCK/since"
  echo "${1:-session} pid=$$ at $(date)" > "$LOCK/owner"
}

case "${1:-status}" in
  acquire)
    if take "${2:-}"; then echo "acquired"; exit 0; fi
    since=$(cat "$LOCK/since" 2>/dev/null || echo 0)
    age=$(( $(date +%s) - since ))
    if [ "$age" -gt "$STALE_SECONDS" ]; then
      rm -rf "$LOCK"
      if take "${2:-}"; then echo "acquired (replaced stale lock, ${age}s old)"; exit 0; fi
    fi
    echo "held: $(cat "$LOCK/owner" 2>/dev/null) (${age}s ago)"; exit 1 ;;
  release) rm -rf "$LOCK"; echo "released" ;;
  status)  if [ -d "$LOCK" ]; then echo "held: $(cat "$LOCK/owner" 2>/dev/null)"; else echo "free"; fi ;;
  *) echo "usage: $0 acquire|release|status" >&2; exit 2 ;;
esac
