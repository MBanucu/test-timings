#!/usr/bin/env bash
# A plain-shell CI gate: run the suite, record the log, then check it.
#
# Keep TIMINGS_DB between runs (cache it in CI), and set EXPECT to the number of
# timing lines the suite writes. TIMINGS_RUN must be unique for every run.
set -euo pipefail

# Use the installed command when it is there, the single file otherwise.
test_timings() {
  if command -v test-timings >/dev/null 2>&1; then
    test-timings "$@"
  else
    python3 test_timings.py "$@"
  fi
}

: "${EXPECT:?set EXPECT to the number of timing lines the suite logs}"
db=${TIMINGS_DB:-.timings/timings.db}
log=${TIMINGS_LOG:-run.log}
# Copy examples/timings.json here, or point TIMINGS_CONFIG at your own file.
config=${TIMINGS_CONFIG:-timings.json}
run=${TIMINGS_RUN:-$(date -u +%Y%m%dT%H%M%S)-$$}
summary=${GITHUB_STEP_SUMMARY:-}

mkdir -p "$(dirname "$db")"

# This run's own log: `>` truncates the previous attempt's file.
set +e
./test/run-suite.sh > "$log" 2>&1
suite_status=$?
set -e

# Record after the suite, then gate the same log. The two --run values match,
# and no earlier run may have used this one.
test_timings record --log "$log" --run "$run" --db "$db" --quiet

args=(check --log "$log" --run "$run" --db "$db" --expect "$EXPECT"
      --config "$config" --json)
if [[ -n $summary ]]; then
  args+=(--summary "$summary")
fi

set +e
test_timings "${args[@]}" > verdict.json
status=$?
set -e

cat verdict.json
if [[ $status -ne 0 ]]; then
  exit "$status"
fi
exit "$suite_status"
