# test-timings

How long did each test take, how much longer will this run need, and did anything
get slower? One SQLite file, one script, no dependencies.

A test runner logs a line per finished test:

```
timing: <class>#<test> <millis>ms <passed|failed>
```

and, from a test that times its own parts:

```
step:<name> <millis>ms
```

`timings.py` turns those lines into three things a harness can act on: a progress
and ETA line it can print while it runs, a history of every test's durations, and
a regression gate that fails a run when a test took much longer than its own
recent median.

```
$ python3 timings.py eta --class suite.e2e --run 1699999999 --log run.log \
      --order "pty tui session" --started 1699999990
eta: 1/3 done, 1m02s spent, 2m05s left, 3m07s total, now tui, finish 14:32:05, 8 run(s) of history
```

## Why a log line and not a test-framework plugin

Every runner can print a line, and a line is the only interface this needs: no
imports, no conftest, no adapter per framework. The Android suite that this came
from writes the line from a shell function and from a JUnit `SuiteRunner` alike,
and both end up in the same database. `step:` lines are optional and only make
`steps` more useful.

Lines are matched anywhere in a line of the log, so the runner may prefix them
with its own indentation or timestamps. A `step:` line belongs to the test whose
`timing:` line follows it.

## Commands

| command | what it answers |
| --- | --- |
| `record --log F --run R` | store this run's lines; the suite calls it after every phase, so the file is always current |
| `eta --class C --run R` | how much of the run is done, what is left, when it should finish |
| `history [--class C]` | every test's run count, median, p90, min, max and last outcome |
| `steps [--run R]` | where the time inside each test of one run went |
| `check --log F --run R` | the gate: exit 1 on a failed, missing or much slower test |

`--run` is any identifier the caller likes (an epoch second, a CI run id). Rows
are unique per `(run, test)`, so re-reading a log that grew is safe and repeats
nothing. It must be **new for every run and every attempt**, and the same value
on `record` and `check`; see [The database](#the-database).

### The estimate

What is left is the sum of the medians of each remaining test's **last five
runs** — all of history would make the estimate lag behind a suite that got
faster by as many runs as it has ever had. A test with no runs yet is estimated
at the class median, or 10 s if it is the class's only test.

What has been spent is the run's wall clock (`--started`, epoch seconds, or
`--elapsed`, seconds) when the caller knows it, so an install or a setup the run
pays before its first test is in the estimate instead of showing up at the end;
without either flag it is the sum of the tests already recorded.

`--order` is the run's list of tests in the order it will run them. That is the
only way to name the test in progress, to count a test the history has never
seen, and to keep the remaining tests in the order they will actually run.

### The gate

```
$ python3 timings.py check --log run.log --run 1699999999
regression: tui-symbols 14.2s, median 9.8s, allowed 13.8s (+45%)
```

A test is judged only after it has run at least once before, and only if its
median is at least `--min-ms` (3 s by default): a two-second test moving by four
hundred milliseconds is the machine, not the code. The allowance is
`median * (1 + --max-regression/100) + --slack-ms`, 20% and 2 s by default —
percent alone is a bad gate on a shared runner, where a four-second test's 20%
is less than its own jitter.

The log is read, not ingested: the run being judged has to be compared with what
the suite cost *before* it, and a database that already held this run would drag
the median towards the very number under test.

`check` gates on the whole run, not only on its times:

- A `timing:` line whose outcome is `failed` fails the command. Failed tests are
  printed before the regressions, because a failure usually explains a test that
  is missing from the rest of the log. `--ignore-failures` turns this off for a
  suite that only wants timings.
- `--expect N` is the number of `timing:` lines the run should have; fewer fails
  and names how many are missing. With `--class` it counts that class's lines.
- `--require NAME` (repeatable, a comma list is accepted) names tests that must
  appear in the log. `--expect` and `--require` close the partial-run cases
  without the suite having to maintain a baseline.
- `--min-judged N` fails when fewer than N tests had a median tall enough to
  judge. It defaults to 0, so an existing invocation is unchanged; an e2e suite
  sets it.
- `--dry-run` prints the same verdicts and exits 0, for a first look at a new
  suite.

Exit codes are fixed:

| code | meaning |
| --- | --- |
| 0 | pass |
| 1 | failed, missing or not enough judged, or a timing regression |
| 2 | usage error |
| 3 | no log, or no parseable `timing:` lines |

Invocations without the new flags behave as before, except that a `failed` line
now fails the gate and an empty or missing log is exit 3 rather than 1 — both
intended fixes. `--min-judged` defaults to 0, so "nothing judged" still passes
unless a suite asks otherwise.

### A config file

`--config PATH` reads the gate's defaults from one JSON object, with the standard
library only:

```json
{
  "db": ".timings/timings.db",
  "min_ms": 3000,
  "slack_ms": 2000,
  "max_regression": 20,
  "min_judged": 0,
  "require": ["pty", "tui-symbols", "diag", "redaction"]
}
```

Every key is optional, and a missing key keeps the built-in default. Unknown
keys, wrong types, a missing file and invalid JSON are errors, with exit 2 and a
message naming the file and the key, so a typo cannot silently turn a threshold
off. `require` takes a list of names (a comma string is accepted too), and
`--require` on the command line replaces the file's list rather than adding to
it. The precedence is CLI flag > config > default; for the database that means
`--db` > config `db` > `TEST_TIMINGS_DB` > `.timings.db`. Only `check` uses the
gate keys; every command honours `db`.

### A Markdown summary

`check --summary PATH` appends a short table, which is what a CI job needs
instead of grepping the log:

```markdown
## test-timings: failed

| judged | slower | failed | missing |
| --- | --- | --- | --- |
| 2 | 1 | 0 | 0 |

| test | now | median | allowed | change |
| --- | --- | --- | --- | --- |
| beta | 12.0s | 6.0s | 9.2s | +100% |
```

`judged`, `slower`, `failed` and `missing` are counts, and the second table names
the three tests furthest over their own median. `missing` counts both `--expect`
lines that did not arrive and `--require` tests that are absent. The section is
appended, so several checks in one job all reach the file.

`$GITHUB_STEP_SUMMARY` is **not** read automatically: pass it explicitly with
`--summary "$GITHUB_STEP_SUMMARY"` so a local run can never write into a CI
summary by accident. A summary that cannot be written is exit 2.

## Machine-readable output

`check`, `history`, `eta` and `steps` take `--json`. The numbers are the ones
the tables print; the human output is unchanged when the flag is absent. Every
object has `"schema": 1`. New keys may appear in a later release, so a consumer
should ignore the ones it does not know; `schema` changes only when an existing
key changes meaning.

`check --json` prints one object, even when the gate fails, and exits the same
way the human path does, so a shell `if` still works:

```
$ test-timings check --class suite.e2e --log run.log --run 1699999999 \
      --expect 4 --require pty,tui-symbols,diag,redaction --json
{"schema": 1, "run": "1699999999", "ok": false, "judged": 1, "checks": [{"test": "pty", "millis": 400, "outcome": "passed", "verdict": "ok"}, {"test": "tui-symbols", "millis": 14200, "outcome": "passed", "median": 9800, "allowed": 13760, "verdict": "slower"}, {"test": "diag", "millis": 1200, "outcome": "failed", "verdict": "ok"}], "failed": ["diag"], "missing": ["redaction"], "reasons": ["failed: diag", "missing: expected 4 timing(s), found 3 (1 missing)", "missing: required test(s) not in the log: redaction", "slower: tui-symbols"]}
```

`checks` has one entry per timing line, in log order. `median` and `allowed`
are present only when the test was judged; an unjudged test still has
`"verdict": "ok"` and no baseline. `failed` names the tests whose outcome was
`failed` (empty under `--ignore-failures`), `missing` names the `--require`
tests that did not appear, and `reasons` says why `ok` is false. `--dry-run`
sets `ok` from the verdicts but exits 0.

`history --json` prints one object per line, so it streams and needs no array:

```
$ test-timings history --json
{"schema": 1, "class": "suite.e2e", "test": "tui-bg", "runs": 2, "median": 7000.0, "p90": 8000, "min": 6000, "max": 8000, "last_outcome": "passed", "last_millis": 8000}
{"schema": 1, "class": "suite.e2e", "test": "tui-echo", "runs": 2, "median": 3000.0, "p90": 4000, "min": 2000, "max": 4000, "last_outcome": "passed", "last_millis": 4000}
```

The durations are milliseconds, and `median` is the same number the table
formats, so `seconds(median)` is the table's cell. An empty database prints
nothing.

`eta --json` prints one object with whole seconds:

```
$ test-timings eta --class suite.e2e --run 1699999999 \
      --order "pty tui session" --started 1699999937 --json
{"schema": 1, "done": 1, "total": 3, "spent_s": 62, "left_s": 125, "total_s": 187, "current": "tui", "finish_epoch": 1700000124, "history_runs": 8}
```

`current` and `finish_epoch` are `null` when there is no such test or no clock
(`--started`/`--elapsed`); `total_s` is `spent_s + left_s`.

`steps --json` prints one object per run:

```
$ test-timings steps --run 1699999999 --json
{"schema": 1, "run": "1699999999", "tests": [{"test": "tui", "millis": 8200, "steps": [{"name": "install", "millis": 1200}, {"name": "launch", "millis": 900}], "outside_millis": 6100}]}
```

`outside_millis` is the part of the test's duration that no `step:` line
covered. `--quiet` silences the human output, which is what a `record` in a
loop wants; with `--json` the JSON is still printed.

Exit codes, whether the output is prose or JSON:

| code | meaning |
| --- | --- |
| 0 | pass |
| 1 | failed, missing or not enough judged, or a timing regression |
| 2 | usage error |
| 3 | no log, or no parseable `timing:` lines |

## The database

`.timings.db` in the working directory. `--db` on either side of the subcommand,
config `db`, or `TEST_TIMINGS_DB` in the environment names another file (in that
order). It is plain SQLite (`timings` and `steps` tables), so any client can
query it and a CI job can cache it between runs to keep the history that the gate
compares against.

A CI cache is part of the baseline, so its key has to identify the history the
run belongs to — branch (or the pull request's base branch) and suite:

```text
timings-<os>-<suite>-<branch>-<run_id>-<run_attempt>   # saved entry
timings-<os>-<suite>-<branch>-                          # restore-keys prefix
timings-<os>-<suite>-                                   # fallback for a new branch
```

The run id and attempt make each saved entry new; `restore-keys` finds the most
recent earlier one, because a cache key cannot be overwritten. A miss is not an
error, but it means **no history**: `check` judges nothing, and `--min-judged`
fails. That is the intended signal that a suite has not been seeded yet, not a
regression.

`--run` must be **unique for every run** (and every attempt of a run). Rows are
unique per `(run, test)` and `record` ignores a repeated one, so reusing an id
silently drops the new sample that would have updated the median. It also has to
match between `record` and `check`, and the log has to be the current run's file:
truncate it before the suite, do not append to the previous attempt's log.

## CI

Copy-pasteable snippets live in [`examples/`](examples/): a GitHub Actions
workflow (`github-actions.yml`), a plain-shell script (`shell.sh`), a sample
config (`timings.json`), and `local-demo.sh`, which runs a fake suite twice,
then fails the third run on a regression and writes the Markdown summary — no
GitHub runner needed. Both CI snippets use a `timings.json` like the one above;
drop the `--config` if you would rather keep the flags on the command line.

### GitHub Actions

```yaml
env:
  TIMINGS_DB: .timings/timings.db
  TIMINGS_LOG: run.log

steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with:
      python-version: "3.12"
  - uses: actions/cache@v4
    with:
      path: ${{ env.TIMINGS_DB }}
      key: timings-${{ runner.os }}-e2e-${{ github.ref_name }}-${{ github.run_id }}-${{ github.run_attempt }}
      restore-keys: |
        timings-${{ runner.os }}-e2e-${{ github.ref_name }}-
        timings-${{ runner.os }}-e2e-
  - run: |
      python3 -m pip install test-timings==0.2.0
      mkdir -p "$(dirname "$TIMINGS_DB")"
      ./test/run-suite.sh > "$TIMINGS_LOG" 2>&1
  - if: always()
    run: |
      test-timings record --log "$TIMINGS_LOG" \
        --run "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" \
        --db "$TIMINGS_DB" --quiet
      set +e
      test-timings check --log "$TIMINGS_LOG" \
        --run "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" \
        --db "$TIMINGS_DB" --expect 12 --config timings.json \
        --json --summary "$GITHUB_STEP_SUMMARY" > verdict.json
      status=$?
      set -e
      cat verdict.json
      exit "$status"
  - if: failure()
    uses: actions/upload-artifact@v4
    with:
      name: test-timings-${{ github.run_id }}-${{ github.run_attempt }}
      path: |
        run.log
        verdict.json
```

`if: always()` keeps the gate running when the suite itself failed (its log is
still worth recording), and `if: failure()` uploads the log and JSON verdict.
The cache is saved only when the job succeeds, so a failing run does not become
the next baseline. `examples/github-actions.yml` is the same flow with comments
and the suite command left as a placeholder.

### Plain shell

```sh
export EXPECT=12
export TIMINGS_DB=.timings/timings.db
export TIMINGS_RUN="$(date -u +%Y%m%dT%H%M%S)-$$"   # unique per run

./test/run-suite.sh > run.log 2>&1                  # this run's own log

test-timings record --log run.log --run "$TIMINGS_RUN" --db "$TIMINGS_DB" --quiet

summary=()
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  summary=(--summary "$GITHUB_STEP_SUMMARY")
fi
test-timings check --log run.log --run "$TIMINGS_RUN" --db "$TIMINGS_DB" \
  --expect "$EXPECT" --config timings.json --json "${summary[@]}" > verdict.json
```

`examples/shell.sh` wraps this as a script, including the installed-command /
single-file fallback and preserving the exit code.

### Seeding a new suite

A gate with no history is either vacuous or noisy, so seed it deliberately:

1. Run once with `check --dry-run` (and `--json` if a script reads it). Nothing
   has a median yet, so nothing is judged; the output names the tests that will
   be judged once they do.
2. Record a few normal runs — three to five is enough for the median of each
   test to be its usual self, which is the number the gate compares against.
3. Then turn on `--min-judged N` (or `min_judged` in the config). From then on a
   cache miss or a truncated suite fails instead of passing silently.

`examples/local-demo.sh` is this flow in miniature: two records, a third run
with one test twice as slow, exit 1, and a Markdown summary.

The hand-rolled version of this gate lives in
[`android-accessible-terminal`'s `test/timings.sh`](https://github.com/MBanucu/android-accessible-terminal/blob/main/test/timings.sh);
[`wireless-adb-keeper` #56](https://github.com/MBanucu/wireless-adb-keeper/issues/56)
plans the same logic in bash. Both can use this instead of maintaining it twice.

## Install

```sh
pip install test-timings

test-timings --version      # test-timings 0.2.0
test-timings history
```

Python 3.10 or newer; standard library only, no dependencies. The distribution is
`test-timings`, the module is `test_timings`, and the command is `test-timings`,
so nothing shadows the `timings` name that another package already has.

Without pip, the file on its own is still the whole tool — copy `test_timings.py`
next to your test scripts and call it:

```sh
python3 test_timings.py history
```

or, in CI, fetch the file from a pinned release and check it:

```sh
curl -fsSLO https://raw.githubusercontent.com/MBanucu/test-timings/v0.2.0/test_timings.py
python3 test_timings.py check --log run.log --run "$CI_RUN_ID"
```

## Tests

```sh
python3 -m unittest discover -s tests
```

CI runs them on 3.10, 3.12 and 3.13, then builds the sdist and the wheel, installs
each into its own virtualenv and runs the installed command, because the tests
against the checkout would not notice packaging that ships the wrong thing.

## Releasing

The version is in `test_timings.__version__`, and `pyproject.toml` takes it from
there, so there is one number to change. Tag that number:

```sh
git commit -am "0.2.0" && git tag -a v0.2.0 -m "0.2.0" && git push origin main v0.2.0
```

The tag runs `.github/workflows/release.yml`, which refuses a tag that disagrees
with the module, tests, builds, installs the wheel, and uploads to PyPI with
[trusted publishing](https://docs.pypi.org/trusted-publishers/) — no token is
stored in this repository and none has to be rotated. PyPI trusts this workflow
as `MBanucu/test-timings`, workflow `release.yml`, environment `pypi`; that entry
started as a pending publisher (which is what created the project) and is now an
ordinary trusted publisher on the project's Publishing page.

0.2.0 was the first release through it, and `pip install test-timings` from the
public index gives that wheel.

## Licence

GPL-3.0, the same as the suite this was extracted from. See `LICENSE`.
