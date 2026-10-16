#!/usr/bin/env bash
# Re-invokes Claude Code until PROGRESS.md contains "BUILD COMPLETE" (BUILD_PROMPT.md, Appendix A).
# Run from the repository root and review the commits afterwards. Running unattended
# means approving tool permissions up front; only do that in a dedicated folder or container.
set -u
MAX_ITER=${MAX_ITER:-40}
for i in $(seq 1 "$MAX_ITER"); do
  if grep -q "BUILD COMPLETE" PROGRESS.md 2>/dev/null; then
    echo "Build complete after $((i-1)) iterations."; exit 0
  fi
  echo "=== Iteration $i ==="
  claude -p "Read BUILD_PROMPT.md and PROGRESS.md. Resume the autonomous loop from the next unfinished item. Keep going until the Definition of Done is met; when it is, write BUILD COMPLETE at the top of PROGRESS.md." \
    --max-turns 200
done
echo "Stopped after $MAX_ITER iterations; check PROGRESS.md."; exit 1
