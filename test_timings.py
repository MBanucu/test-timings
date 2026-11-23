#!/usr/bin/env python3
"""Test timings in SQLite, with an estimate for the run in progress.

A test runner logs one line per finished test, with whatever tag it likes:

    timing: <class>#<test> <millis>ms <passed|failed>

and, from the tests that time their own parts:

    step:<name> <millis>ms

This script keeps both in a SQLite file, which answers the questions a harness
has: how these tests have behaved over time (`history`), how much longer the run
in progress needs (`eta`), and where the time inside one test went (`steps`).

The estimate for what is left is the median of each remaining test's past
durations. What has been spent comes from the wall clock when the caller knows
when it started, so an install or a device setup that a run pays before its first
test is part of the estimate instead of being discovered at the end; when it does
not, the spent time is the sum of the tests already recorded. `--order` puts the
remaining tests in the order the run will run them, which is what names the test
in progress and what counts a test the history has not seen yet.

    test-timings record  --log run.log --run 1699999999
    test-timings eta     --class suite.e2e --run 1699999999 --log run.log
    test-timings eta     --class suite.e2e --run 1699999999 \
        --log run.log --order "pty tui session" --started 1699999990
    test-timings history [--class suite.e2e]
    test-timings steps   [--run 1699999999]
    test-timings check   --log run.log --run 1699999999

It is also a single file with nothing but the standard library behind it, so
`python3 test_timings.py <command>` works from a checkout with no install.

`check` is the gate: every test in the log whose median is long enough to judge
has to have finished within `--max-regression` percent of it, a `failed` line
fails the run unless `--ignore-failures`, and `--expect`, `--require` and
`--min-judged` say how complete a run has to be. It exits 0 on a pass, 1 on a
failed, missing, regressed or too sparsely judged run, 2 on a usage error, and
3 when the log is missing or holds no timings. Tests under `--min-ms` are not
judged - a two-second test moving by four hundred milliseconds is the machine,
not the code - and tests with no history are passed over.

The database is `.timings.db` in the working directory, or the file named by
`--db` or by `TEST_TIMINGS_DB`, and can be queried with any SQLite client.

`--json` on `check`, `history`, `eta` and `steps` prints the same numbers for a
machine, each object carrying a `schema` version, and leaves the human output
byte for byte as it was. `--quiet` silences the human output, which is what a
`record` in a loop wants.

`--config PATH` reads gate defaults from a JSON object, and `--summary PATH`
appends a short Markdown table for a CI job summary. A command-line flag wins
over the file, and the file wins over the built-in default.
"""

import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

__version__ = "0.2.0"

# The version of the objects `--json` prints. It only changes when a key's
# meaning changes; new keys are additive, so a consumer may ignore what it does
# not know.
OUTPUT_SCHEMA = 1

LINE = re.compile(r"timing: (\S+)#(\S+) (\d+)ms (passed|failed)")
STEP = re.compile(r"step:(.+?) (\d+)ms\b")

EXIT_CODES = """exit codes:
  0  passed: no failed, missing or regressed test, and enough judged
  1  failed, missing or not enough judged, or a timing regression
  2  usage error
  3  no log, or no parseable timings in it
"""


def default_db():
    """`TEST_TIMINGS_DB` when it is set, else `.timings.db` here."""
    return os.environ.get("TEST_TIMINGS_DB") or os.path.join(os.getcwd(), ".timings.db")


DEFAULT_DB = default_db()

# `--config` may set any of these. The gate flags are resolved in `main`, after
# argparse has said which of them the caller gave, so a flag always wins.
CONFIG_KEYS = ("db", "max_regression", "min_judged", "min_ms", "require", "slack_ms")
GATE_DEFAULTS = {
    "min_ms": 3000,
    "slack_ms": 2000,
    "max_regression": 20.0,
    "min_judged": 0,
    "require": [],
}


class ConfigError(Exception):
    """A `--config` file that cannot be used, with the reason in the message."""


def load_config(path):
    """The settings in `path`, or an empty dict when there is no `--config`."""
    if not path:
        return {}
    try:
        with open(path) as stream:
            values = json.load(stream)
    except OSError as error:
        reason = error.strerror or str(error)
        raise ConfigError("--config %s: %s" % (path, reason))
    except json.JSONDecodeError as error:
        raise ConfigError("--config %s: not valid JSON: %s" % (path, error))
    if not isinstance(values, dict):
        raise ConfigError("--config %s: must hold one JSON object" % path)
    unknown = sorted(set(values) - set(CONFIG_KEYS))
    if unknown:
        raise ConfigError(
            "--config %s: unknown key(s): %s (allowed: %s)"
            % (path, ", ".join(unknown), ", ".join(CONFIG_KEYS)))
    settings = {}
    if "db" in values:
        if not isinstance(values["db"], str) or not values["db"]:
            raise ConfigError("--config %s: db must be a non-empty string" % path)
        settings["db"] = values["db"]
    for key in ("min_ms", "slack_ms", "min_judged"):
        if key in values:
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError("--config %s: %s must be an integer" % (path, key))
            settings[key] = value
    if "max_regression" in values:
        value = values["max_regression"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(
                "--config %s: max_regression must be a number" % path)
        settings["max_regression"] = float(value)
    if "require" in values:
        value = values["require"]
        if isinstance(value, str):
            names = [value]
        elif isinstance(value, list) and all(isinstance(name, str) for name in value):
            names = value
        else:
            raise ConfigError(
                "--config %s: require must be a string or a list of strings" % path)
        settings["require"] = names
    return settings


def apply_config(args, settings):
    """Fills in what the command line did not say, from `settings` or defaults."""
    if not hasattr(args, "db"):
        args.db = settings.get("db", DEFAULT_DB)
    # Only `check` has these flags; setting them elsewhere does no harm.
    for key, default in GATE_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, settings.get(key, default))
    return args


SCHEMA = """
CREATE TABLE IF NOT EXISTS timings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run TEXT NOT NULL,
    class TEXT NOT NULL,
    test TEXT NOT NULL,
    outcome TEXT NOT NULL,
    millis INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (run, test)
);
CREATE INDEX IF NOT EXISTS timings_by_test ON timings (class, test);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run TEXT NOT NULL,
    class TEXT NOT NULL,
    test TEXT NOT NULL,
    step TEXT NOT NULL,
    millis INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS steps_by_test ON steps (run, class, test);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def ingest(db, log, run):
    """Adds the timings in `log` that this database has not seen yet."""
    if not log or not os.path.isfile(log):
        return 0
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = 0
    pending = []
    with open(log, errors="replace") as stream:
        for line in stream:
            step = STEP.search(line)
            if step:
                pending.append((step.group(1).strip(), int(step.group(2))))
                continue
            found = LINE.search(line)
            if not found:
                continue
            row = db.execute(
                "INSERT OR IGNORE INTO timings"
                " (run, class, test, outcome, millis, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run, found.group(1), found.group(2), found.group(4),
                 int(found.group(3)), stamp),
            )
            added += row.rowcount
            if row.rowcount:
                # A step belongs to the test whose timing line follows it. The
                # log is re-read after every phase, so steps are only stored when
                # their test is new - otherwise each re-read would repeat them.
                for name, millis in pending:
                    db.execute(
                        "INSERT INTO steps (run, class, test, step, millis, recorded_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (run, found.group(1), found.group(2), name, millis, stamp),
                    )
            pending.clear()
    db.commit()
    return added


def median_of(values):
    return int(statistics.median(values)) if values else None


def seconds(millis):
    return "%.1fs" % (millis / 1000.0)


def whole_seconds(millis):
    """The millisecond count in whole seconds, for the machine objects."""
    return int(millis / 1000.0 + 0.5)


def p90_of(values):
    """The p90 the history table has always shown, from sorted values."""
    return values[min(len(values) - 1, round(0.9 * (len(values) - 1)))]


def emit_json(payload):
    print(json.dumps(payload))


# What a test with no history at all is assumed to cost. It is the median of the
# class when there is one; this is only for a class with a single recorded test.
DEFAULT_ESTIMATE = 10_000
# How many of a test's most recent runs its estimate is the median of. All of
# history would make the estimate lag behind a suite that got faster (or a
# machine that got slower) by as many runs as it has ever had; the last few
# follow it.
RECENT_RUNS = 5


def split_order(order):
    """`--order` as a list, however the caller wrote the separator."""
    return [piece for piece in re.split(r"[,\s]+", order.strip()) if piece]


def plan(db, name, run, order=None):
    """The tests of this class in run order, what is done, and what is left.

    `order` is the run's own list of tests. It is the only way to know which test
    is in progress, to count a test that has no history yet, and to put the
    remaining ones in the order they will actually run; without it the tests are
    sorted by name, which is right for a `history` read and wrong for an estimate.
    """
    known = {row[0] for row in db.execute(
        "SELECT DISTINCT test FROM timings WHERE class = ?", (name,))}
    done = {row[0]: row[1] for row in db.execute(
        "SELECT test, millis FROM timings WHERE class = ? AND run = ?", (name, run))}
    if order:
        # The run's own list is authoritative. A test it will run and the history
        # has never seen still costs time, so it is counted and estimated from the
        # class; dropping it would make the estimate short by however long it takes.
        tests = split_order(order)
    else:
        tests = sorted(known | set(done))
    overall = median_of([row[0] for row in db.execute(
        "SELECT millis FROM timings WHERE class = ?", (name,))])
    estimates = {}
    for test in tests:
        if test in done:
            continue
        # `id` is insertion order, and runs are recorded as they finish, so the
        # highest ids are the most recent runs of this test.
        past = [row[0] for row in db.execute(
            "SELECT millis FROM timings WHERE class = ? AND test = ? AND run <> ?"
            " ORDER BY id DESC LIMIT ?", (name, test, run, RECENT_RUNS))]
        estimates[test] = median_of(past) or overall or DEFAULT_ESTIMATE
    runs = db.execute(
        "SELECT COUNT(DISTINCT run) FROM timings WHERE class = ?", (name,)).fetchone()[0]
    return tests, done, estimates, runs


def report_record(args):
    db = connect(args.db)
    added = ingest(db, args.log, args.run)
    if not args.quiet:
        print("recorded %d timing(s) for run %s" % (added, args.run))
    return 0


def report_eta(args):
    db = connect(args.db)
    ingest(db, args.log, args.run)
    tests, done, estimates, runs = plan(db, args.class_, args.run, args.order)
    left = sum(estimates.values())
    # Wall clock when the caller has it: a run pays for its install and its
    # setup before the first test, and an estimate built only from test
    # durations leaves that out until the run is over. The sum is the fallback
    # for a caller that only knows what the log says.
    elapsed = sum(done.values())
    if args.started is not None:
        elapsed = max(0, int(time.time() - args.started)) * 1000
    elif args.elapsed is not None:
        elapsed = max(0, args.elapsed) * 1000
    current = next((test for test in tests if test not in done), None)
    clock_known = args.started is not None or args.elapsed is not None
    if args.json:
        spent_s = whole_seconds(elapsed)
        left_s = whole_seconds(left)
        emit_json({
            "schema": OUTPUT_SCHEMA,
            "done": len(done),
            "total": len(tests),
            "spent_s": spent_s,
            "left_s": left_s,
            "total_s": spent_s + left_s,
            "current": current,
            "finish_epoch": (int(time.time() + left / 1000.0)
                             if left and clock_known else None),
            "history_runs": runs,
        })
        return 0
    if not tests:
        if not args.quiet:
            print("eta: no timing history yet for %s" % args.class_)
        return 0
    line = ("eta: %d/%d done, %s spent, %s left, %s total"
            % (len(done), len(tests), seconds(elapsed), seconds(left),
               seconds(elapsed + left)))
    if current:
        line += ", now %s" % current
    if left and clock_known:
        line += ", finish %s" % (datetime.now()
                                  + timedelta(milliseconds=left)).strftime("%H:%M:%S")
    line += ", %d run(s) of history" % runs
    if not args.quiet:
        print(line)
    return 0


def report_history(args):
    db = connect(args.db)
    query = ("SELECT class, test, millis, outcome FROM timings"
             + (" WHERE class = ?" if args.class_ else "")
             + " ORDER BY class, test, id")
    rows = db.execute(query, (args.class_,) if args.class_ else ())
    grouped = {}
    for room, test, millis, outcome in rows:
        entry = grouped.setdefault((room, test), {"millis": [], "outcome": outcome})
        entry["millis"].append(millis)
        # The table's "last" column has always shown the first result in
        # insertion order; keep that byte for byte. The JSON says what the
        # column means, so it carries the actual last run and its duration.
        entry["last_outcome"] = outcome
        entry["last_millis"] = millis
    if not grouped:
        if not args.json and not args.quiet:
            print("no timings recorded yet")
        return 0
    if args.json:
        for (room, test), entry in grouped.items():
            values = sorted(entry["millis"])
            emit_json({
                "schema": OUTPUT_SCHEMA,
                "class": room,
                "test": test,
                "runs": len(values),
                "median": statistics.median(values),
                "p90": p90_of(values),
                "min": values[0],
                "max": values[-1],
                "last_outcome": entry["last_outcome"],
                "last_millis": entry["last_millis"],
            })
        return 0
    if args.quiet:
        return 0
    print("%-44s %4s %8s %8s %8s %8s  %s"
          % ("test", "runs", "median", "p90", "min", "max", "last"))
    for (room, test), entry in grouped.items():
        values = sorted(entry["millis"])
        print("%-44s %4d %8s %8s %8s %8s  %s"
              % (test, len(values), seconds(statistics.median(values)),
                 seconds(p90_of(values)), seconds(values[0]), seconds(values[-1]),
                 entry["outcome"]))
    return 0


def log_timings(path):
    """The (class, test, millis, outcome) rows a log holds, in order."""
    found = []
    if not path or not os.path.isfile(path):
        return found
    with open(path, errors="replace") as stream:
        for line in stream:
            match = LINE.search(line)
            if match:
                found.append((match.group(1), match.group(2),
                              int(match.group(3)), match.group(4)))
    return found


def required_tests(values):
    """`--require` as a list: repeated flags and comma or space lists, deduplicated."""
    names = []
    for value in values:
        for name in re.split(r"[,\s]+", value):
            if name and name not in names:
                names.append(name)
    return names


def markdown_summary(ok, judged, slower, failed, missing_required, missing_expected,
                     dry_run=False):
    """A short table for `$GITHUB_STEP_SUMMARY`, with the three worst timings."""
    if ok:
        status = "passed"
    elif dry_run:
        status = "would fail (dry-run)"
    else:
        status = "failed"
    lines = [
        "## test-timings: %s" % status,
        "",
        "| judged | slower | failed | missing |",
        "| --- | --- | --- | --- |",
        "| %d | %d | %d | %d |"
        % (judged, len(slower), len(failed), len(missing_required) + missing_expected),
        "",
    ]
    # The worst regressions, not the first three in the log. A test's overage
    # relative to its own baseline is what makes one worse than another.
    worst = sorted(slower, key=lambda row: row[1] / row[2], reverse=True)[:3]
    if worst:
        lines.extend([
            "| test | now | median | allowed | change |",
            "| --- | --- | --- | --- | --- |",
        ])
        for test, millis, baseline, allowed in worst:
            lines.append("| %s | %s | %s | %s | %+.0f%% |"
                         % (test.replace("|", "\\|"), seconds(millis),
                            seconds(baseline), seconds(allowed),
                            100.0 * (millis - baseline) / baseline))
        lines.append("")
    return "\n".join(lines)


def append_summary(path, markdown):
    """Appends one section, so several checks in a job all reach the summary."""
    prefix = "\n" if os.path.exists(path) and os.path.getsize(path) else ""
    with open(path, "a") as stream:
        stream.write(prefix + markdown)


def report_check(args):
    """Fails when a test failed, is missing, or took much longer than its median.

    The log is read, not ingested: the run being judged has to be compared with
    what the suite cost before it, and a database that already held this run
    would move the median towards the very number under test.

    A failed test is printed first, because it usually explains a test that is
    missing from the rest of the log.
    """
    db = connect(args.db)
    current = [row for row in log_timings(args.log)
               if not args.class_ or row[0] == args.class_]
    if not current:
        # A class filter that matches nothing is not an empty log; say which one.
        scope = " for class %s" % args.class_ if args.class_ else ""
        message = "regression: no timings in %s%s" % (args.log or "the log", scope)
        if args.json:
            emit_json({
                "schema": OUTPUT_SCHEMA,
                "run": args.run,
                "ok": False,
                "judged": 0,
                "checks": [],
                "failed": [],
                "missing": [],
                "reasons": [message],
            })
        elif not args.quiet:
            print(message)
        if args.summary:
            try:
                append_summary(args.summary, markdown_summary(
                    False, 0, [], [], [], 0))
            except OSError as error:
                print("summary: cannot write %s: %s" % (args.summary, error),
                      file=sys.stderr)
                return 2
        return 3
    failed = [row for row in current if row[3] == "failed"]
    present = set()
    for room, test, _millis, _outcome in current:
        present.add(test)
        present.add("%s#%s" % (room, test))
    missing_expected = 0
    if args.expect is not None:
        missing_expected = max(0, args.expect - len(current))
    missing_required = [name for name in required_tests(args.require)
                        if name not in present]
    judged = 0
    slower = []
    checks = []
    for room, test, millis, outcome in current:
        past = [row[0] for row in db.execute(
            "SELECT millis FROM timings WHERE class = ? AND test = ? AND run <> ?"
            " ORDER BY id DESC LIMIT ?", (room, test, args.run, RECENT_RUNS))]
        baseline = median_of(past)
        check = {"test": test, "millis": millis, "outcome": outcome}
        if not baseline or baseline < args.min_ms:
            # Not judged is still a passing timing; the example in the issue
            # shows exactly this shape for a test below --min-ms.
            check["verdict"] = "ok"
            checks.append(check)
            continue
        judged += 1
        # Percent alone is not a gate on a shared runner: a suite's own history
        # can have a test between 2 s and 6 s, where 20% of a four-second test is
        # less than the test's jitter. The slack is added to the allowance and is
        # loud in the message.
        allowed = baseline * (100 + args.max_regression) / 100.0 + args.slack_ms
        check["median"] = baseline
        check["allowed"] = int(round(allowed))
        check["verdict"] = "slower" if millis > allowed else "ok"
        checks.append(check)
        if millis > allowed:
            slower.append((test, millis, baseline, allowed))
    short_judged = judged < args.min_judged
    reasons = []
    lines = []
    failed_names = []
    if not args.ignore_failures:
        for _room, test, millis, _outcome in failed:
            lines.append("failed: %s %s" % (test, seconds(millis)))
            reasons.append("failed: %s" % test)
            if test not in failed_names:
                failed_names.append(test)
    if missing_expected:
        line = ("missing: expected %d timing(s), found %d (%d missing)"
                % (args.expect, len(current), missing_expected))
        lines.append(line)
        reasons.append(line)
    if missing_required:
        line = ("missing: required test(s) not in the log: %s"
                % ", ".join(missing_required))
        lines.append(line)
        reasons.append(line)
    for test, millis, baseline, allowed in slower:
        lines.append("regression: %s %s, median %s, allowed %s (%+.0f%%)"
                     % (test, seconds(millis), seconds(baseline), seconds(allowed),
                        100.0 * (millis - baseline) / baseline))
        reasons.append("slower: %s" % test)
    if short_judged:
        line = ("judged: %d test(s) judged, --min-judged requires %d"
                % (judged, args.min_judged))
        lines.append(line)
        reasons.append(line)
    ok = not lines
    if ok:
        lines.append("regression: %d check(s) within %d%% of their median"
                     " (%d not judged)"
                     % (judged, args.max_regression, len(current) - judged))
    elif args.dry_run:
        lines.append("dry-run: would fail (exit 1)")
    if args.json:
        emit_json({
            "schema": OUTPUT_SCHEMA,
            "run": args.run,
            "ok": ok,
            "judged": judged,
            "checks": checks,
            "failed": failed_names,
            "missing": missing_required,
            "reasons": reasons,
        })
    elif not args.quiet:
        for line in lines:
            print(line)
    if args.summary:
        try:
            append_summary(args.summary, markdown_summary(
                ok, judged, slower, failed_names, missing_required,
                missing_expected, args.dry_run))
        except OSError as error:
            print("summary: cannot write %s: %s" % (args.summary, error),
                  file=sys.stderr)
            return 2
    if not ok:
        return 0 if args.dry_run else 1
    return 0


def report_steps(args):
    """Where the time inside each test of one run went."""
    db = connect(args.db)
    run = args.run
    if not run:
        found = db.execute(
            "SELECT run FROM timings ORDER BY CAST(run AS INTEGER) DESC LIMIT 1"
        ).fetchone()
        if not found:
            if args.json:
                emit_json({"schema": OUTPUT_SCHEMA, "run": None, "tests": []})
            elif not args.quiet:
                print("no timings recorded yet")
            return 0
        run = found[0]
    rows = list(db.execute(
        "SELECT class, test, millis FROM timings WHERE run = ? ORDER BY millis DESC",
        (run,)))
    if not rows:
        if args.json:
            emit_json({"schema": OUTPUT_SCHEMA, "run": run, "tests": []})
        elif not args.quiet:
            print("no timings for run %s" % run)
        return 0
    if args.json:
        tests = []
        for room, test, millis in rows:
            steps = list(db.execute(
                "SELECT step, millis FROM steps"
                " WHERE run = ? AND class = ? AND test = ? ORDER BY id",
                (run, room, test)))
            covered = sum(step[1] for step in steps)
            tests.append({
                "test": test,
                "millis": millis,
                "steps": [{"name": name, "millis": took} for name, took in steps],
                "outside_millis": max(0, millis - covered),
            })
        emit_json({"schema": OUTPUT_SCHEMA, "run": run, "tests": tests})
        return 0
    if args.quiet:
        return 0
    print("run %s: %s in %d test(s)" % (run, seconds(sum(row[2] for row in rows)), len(rows)))
    for room, test, millis in rows:
        steps = list(db.execute(
            "SELECT step, millis FROM steps WHERE run = ? AND class = ? AND test = ?"
            " ORDER BY id", (run, room, test)))
        if not steps:
            print("  %-50s %6s" % (test, seconds(millis)))
            continue
        covered = sum(step[1] for step in steps)
        print("  %-50s %6s  (%s in %d step(s), %s outside them)"
              % (test, seconds(millis), seconds(covered), len(steps),
                 seconds(max(0, millis - covered))))
        for name, took in steps:
            print("      %-46s %6s" % (name, seconds(took)))
    return 0


def main(argv):
    # --db is accepted on either side of the subcommand: before it here, after it
    # through `common`, where SUPPRESS keeps the inner default from wiping the
    # outer one when the flag is only given once.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS)
    common.add_argument("--config", default=argparse.SUPPRESS, metavar="PATH")
    common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="silence human output")

    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument("--db", default=argparse.SUPPRESS, help="SQLite file to use")
    parser.add_argument("--config", default=argparse.SUPPRESS, metavar="PATH",
                        help="JSON file with db and gate defaults")
    parser.add_argument("--quiet", action="store_true", help="silence human output")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", parents=[common],
                            help="store the timings found in a log")
    record.add_argument("--log", required=True)
    record.add_argument("--run", required=True)
    record.set_defaults(handler=report_record)

    eta = sub.add_parser("eta", parents=[common],
                         help="estimate how long the run in progress needs")
    eta.add_argument("--class", dest="class_", required=True)
    eta.add_argument("--run", required=True)
    eta.add_argument("--log", default="")
    eta.add_argument("--order", default="",
                     help="the run's tests, in the order it will run them")
    eta.add_argument("--started", type=int, default=None,
                     help="epoch seconds the run began, for wall-clock spent")
    eta.add_argument("--elapsed", type=int, default=None,
                     help="seconds the run has spent so far")
    eta.add_argument("--json", action="store_true",
                     help="print the estimate as one JSON object")
    eta.set_defaults(handler=report_eta)

    check = sub.add_parser("check", parents=[common],
                           help="fail when a test is failed, missing or slower than its median",
                           epilog=EXIT_CODES,
                           formatter_class=argparse.RawDescriptionHelpFormatter)
    check.add_argument("--class", dest="class_", default="")
    check.add_argument("--log", required=True)
    check.add_argument("--run", required=True)
    check.add_argument("--max-regression", type=float, default=argparse.SUPPRESS,
                       help="percent slower than the median that fails (default 20)")
    check.add_argument("--min-ms", type=int, default=argparse.SUPPRESS,
                       help="medians below this are too short to judge (default 3000)")
    check.add_argument("--slack-ms", type=int, default=argparse.SUPPRESS,
                       help="added to the allowance, so jitter does not fail"
                            " (default 2000)")
    check.add_argument("--expect", type=int, default=None, metavar="N",
                       help="number of timing lines the run should have; fewer fails")
    check.add_argument("--require", action="append", default=argparse.SUPPRESS,
                       metavar="NAME",
                       help="test that must appear; repeatable, comma list accepted")
    check.add_argument("--min-judged", type=int, default=argparse.SUPPRESS, metavar="N",
                       help="fail when fewer than N tests have a baseline to judge"
                            " (default 0)")
    check.add_argument("--ignore-failures", action="store_true",
                       help="do not fail on timing lines whose outcome is failed")
    check.add_argument("--dry-run", action="store_true",
                       help="print the verdicts without failing the process")
    check.add_argument("--json", action="store_true",
                       help="print the verdict as one JSON object, even on failure")
    check.add_argument("--summary", default=None, metavar="PATH",
                       help="append a Markdown table to PATH, for a CI job summary")
    check.set_defaults(handler=report_check)

    history = sub.add_parser("history", parents=[common],
                             help="per-test durations recorded so far")
    history.add_argument("--class", dest="class_", default="")
    history.add_argument("--json", action="store_true",
                         help="print one JSON object per test")
    history.set_defaults(handler=report_history)

    steps = sub.add_parser("steps", parents=[common],
                           help="where the time inside each test went")
    steps.add_argument("--run", default="")
    steps.add_argument("--json", action="store_true",
                       help="print the run's steps as one JSON object")
    steps.set_defaults(handler=report_steps)

    args = parser.parse_args(argv)
    try:
        settings = load_config(getattr(args, "config", None))
    except ConfigError as error:
        parser.error(str(error))
    apply_config(args, settings)
    return args.handler(args)


def cli():
    """The `test-timings` command installed by pip."""
    raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
