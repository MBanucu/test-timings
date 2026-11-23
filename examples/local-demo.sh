#!/usr/bin/env bash
# The whole flow, locally: seed history with two runs, then fail the third on a
# regression and leave a Markdown summary behind.
#
#   examples/local-demo.sh [WORKDIR]
#
# No GitHub runner and no network: the suite is fake-suite.sh, and the tool is
# the file in this checkout.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work=${1:-$(mktemp -d)}
tool=(python3 "$root/test_timings.py")
suite="$root/examples/fake-suite.sh"
log="$work/run.log"
db="$work/timings.db"
summary="$work/step-summary.md"
verdict="$work/verdict.json"

mkdir -p "$work"

# Two ordinary runs give every judged test a median. A first run alone has no
# baseline, which is why a new suite starts with --dry-run and a few records.
for run in 1 2; do
  bash "$suite" "$log"
  "${tool[@]}" record --log "$log" --run "$run" --db "$db" --quiet
done

# The third run is slow. Record it after the suite, as CI does, then gate the
# same log: `check` leaves the current run out of its own baseline.
bash "$suite" "$log" --slow
"${tool[@]}" record --log "$log" --run 3 --db "$db" --quiet

set +e
"${tool[@]}" check --log "$log" --run 3 --db "$db" \
  --expect 3 --min-judged 2 --json --summary "$summary" > "$verdict"
status=$?
set -e

if [[ $status -ne 1 ]]; then
  echo "local-demo: expected the gate to fail with exit 1, got $status" >&2
  exit 1
fi

python3 - "$verdict" <<'PY'
import json
import sys

with open(sys.argv[1]) as stream:
    verdict = json.load(stream)
slower = [row["test"] for row in verdict["checks"]
          if row.get("verdict") == "slower"]
if verdict["ok"] or slower != ["beta"]:
    raise SystemExit("local-demo: unexpected verdict: %r" % verdict)
print("local-demo: gate failed as expected; slower test(s): %s"
      % ", ".join(slower))
PY

echo
cat "$summary"
echo "local-demo: summary written to $summary"
