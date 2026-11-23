#!/usr/bin/env bash
# A stand-in for a real test suite: it writes the timing lines test-timings reads.
#
#   fake-suite.sh run.log          # a normal run
#   fake-suite.sh run.log --slow   # the same suite with one test much slower
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: fake-suite.sh LOG [--slow]" >&2
  exit 2
fi

log=$1
mode=${2:-}
case $mode in
  ""|--slow) ;;
  *)
    echo "fake-suite: unknown option: $mode" >&2
    exit 2
    ;;
esac

beta=6000
if [[ $mode == --slow ]]; then
  beta=12000
fi

{
  printf 'timing: example.suite#alpha 4000ms passed\n'
  printf 'timing: example.suite#beta %dms passed\n' "$beta"
  # Below the default --min-ms, so it is recorded but not judged.
  printf 'timing: example.suite#gamma 2500ms passed\n'
} > "$log"

echo "fake-suite: wrote 3 timings to $log"
