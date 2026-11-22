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
| `check --log F --run R` | the gate: exit 1 if a test was much slower than its median |

`--run` is any identifier the caller likes (an epoch second, a CI run id). Rows
are unique per `(run, test)`, so re-reading a log that grew is safe and repeats
nothing.

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

## The database

`.timings.db` in the working directory. `--db` on either side of the subcommand,
or `TEST_TIMINGS_DB` in the environment, names another file. It is plain SQLite
(`timings` and `steps` tables), so any client can query it and a CI job can cache
it between runs to keep the history that the gate compares against.

## Usage

Copy `timings.py` next to your test scripts, or fetch a pinned release:

```sh
curl -fsSLO https://raw.githubusercontent.com/MBanucu/test-timings/v0.1.0/timings.py
python3 timings.py history
```

Python 3.10 or newer; standard library only.

## Tests

```sh
python3 -m unittest discover -s tests
```

## Licence

GPL-3.0, the same as the suite this was extracted from. See `LICENSE`.
