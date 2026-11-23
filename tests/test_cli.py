"""Tests for the timing store: ingest, estimate, history, gate, steps.

Run with `python3 -m unittest discover -s tests`.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_timings as timings  # noqa: E402


def run_output(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = timings.main(argv)
    return code, out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "timings.db")
        self.log = os.path.join(self.tmp.name, "run.log")

    def write_log(self, *lines, mode="w"):
        with open(self.log, mode) as stream:
            for line in lines:
                stream.write(line + "\n")

    def write_db(self, *rows):
        """Rows as (run, class, test, millis), in the order they arrive."""
        db = timings.connect(self.db)
        for run, room, test, millis in rows:
            db.execute(
                "INSERT INTO timings (run, class, test, outcome, millis, recorded_at)"
                " VALUES (?, ?, ?, 'passed', ?, '2026-01-01T00:00:00+00:00')",
                (run, room, test, millis),
            )
        db.commit()
        db.close()

    def command(self, *argv):
        return run_output([*argv, "--db", self.db])


class IngestTest(Base):
    def test_records_each_timing_line_once(self):
        self.write_log(
            "  tui-echo: 2s",
            "timing: suite.e2e#tui-echo 2100ms passed",
            "timing: suite.e2e#tui-bg 7400ms failed",
        )
        self.assertEqual(timings.ingest(timings.connect(self.db), self.log, "1"), 2)
        # Reading the log again after another phase must not double the rows.
        self.assertEqual(timings.ingest(timings.connect(self.db), self.log, "1"), 0)
        db = timings.connect(self.db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM timings").fetchone()[0], 2)

    def test_step_belongs_to_the_test_after_it(self):
        self.write_log(
            "step:install 1200ms",
            "step:launch 900ms",
            "timing: suite.e2e#tui-echo 2100ms passed",
        )
        db = timings.connect(self.db)
        timings.ingest(db, self.log, "1")
        timings.ingest(db, self.log, "1")
        steps = db.execute("SELECT test, step, millis FROM steps ORDER BY id").fetchall()
        self.assertEqual(
            steps,
            [("tui-echo", "install", 1200), ("tui-echo", "launch", 900)],
        )

    def test_ignores_a_log_that_is_not_there(self):
        db = timings.connect(self.db)
        self.assertEqual(timings.ingest(db, os.path.join(self.tmp.name, "no.log"), "1"), 0)


class EtaTest(Base):
    def test_the_logs_own_test_counts_as_done(self):
        self.write_log("timing: suite.e2e#tui-echo 2100ms passed")
        code, out = self.command("eta", "--class", "suite.e2e", "--run", "1", "--log", self.log)
        self.assertEqual(code, 0)
        # Ingested from the log, so the run is seen to have finished that test.
        self.assertIn("eta: 1/1 done", out)
        self.assertIn("2.1s spent", out)

    def test_unknown_class_has_no_estimate(self):
        code, out = self.command("eta", "--class", "nothing.here", "--run", "1")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "eta: no timing history yet for nothing.here")

    def test_counting_the_run_in_progress(self):
        # Two runs of history, then a third in progress with one test logged.
        self.write_db(
            ("1", "suite.e2e", "tui-echo", 2000),
            ("1", "suite.e2e", "tui-bg", 6000),
            ("2", "suite.e2e", "tui-echo", 3000),
            ("2", "suite.e2e", "tui-bg", 8000),
        )
        self.write_log("timing: suite.e2e#tui-echo 2500ms passed")
        code, out = self.command(
            "eta", "--class", "suite.e2e", "--run", "3", "--log", self.log,
            "--order", "tui-echo tui-bg", "--elapsed", "10",
        )
        self.assertEqual(code, 0)
        # tui-bg's median is 7.0s, spent is the 10s the caller reported.
        self.assertIn("eta: 1/2 done", out)
        self.assertIn("10.0s spent", out)
        self.assertIn("7.0s left", out)
        self.assertIn("17.0s total", out)
        self.assertIn("now tui-bg", out)
        self.assertIn("finish ", out)
        # The log line above is ingested, so this run is one of the three as well.
        self.assertIn("3 run(s) of history", out)

    def test_order_names_the_test_in_progress_and_counts_an_unknown_one(self):
        self.write_db(("1", "suite.e2e", "tui-echo", 2000))
        code, out = self.command(
            "eta", "--class", "suite.e2e", "--run", "2",
            "--order", "tui-echo new-check", "--elapsed", "1",
        )
        self.assertEqual(code, 0)
        self.assertIn("eta: 0/2 done", out)
        self.assertIn("now tui-echo", out)

    def test_without_a_clock_there_is_no_finish_time(self):
        self.write_db(("1", "suite.e2e", "tui-echo", 2000))
        code, out = self.command("eta", "--class", "suite.e2e", "--run", "2")
        self.assertEqual(code, 0)
        self.assertNotIn("finish", out)

    def test_the_estimate_follows_recent_runs_and_not_all_history(self):
        # Six runs: the oldest five are slow, the newest is fast. The estimate is
        # the median of the last five, so it must be near the fast one.
        self.write_db(
            ("1", "suite.e2e", "tui-echo", 30000),
            ("2", "suite.e2e", "tui-echo", 30000),
            ("3", "suite.e2e", "tui-echo", 30000),
            ("4", "suite.e2e", "tui-echo", 30000),
            ("5", "suite.e2e", "tui-echo", 30000),
            ("6", "suite.e2e", "tui-echo", 1000),
        )
        code, out = self.command("eta", "--class", "suite.e2e", "--run", "7",
                                 "--elapsed", "0")
        self.assertEqual(code, 0)
        self.assertIn("30.0s left", out)


class HistoryTest(Base):
    def test_one_row_per_test_with_its_median(self):
        self.write_db(
            ("1", "suite.e2e", "tui-echo", 2000),
            ("2", "suite.e2e", "tui-echo", 4000),
            ("1", "suite.e2e", "tui-bg", 8000),
        )
        code, out = self.command("history")
        self.assertEqual(code, 0)
        line = next(row for row in out.splitlines() if row.startswith("tui-echo"))
        self.assertIn("3.0s", line)  # median of 2 and 4
        self.assertIn("2", line.split()[1])  # two runs
        self.assertIn("passed", line)

    def test_empty_database(self):
        code, out = self.command("history")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "no timings recorded yet")


class CheckTest(Base):
    def gate(self, *extra):
        return self.command("check", "--class", "suite.e2e", "--run", "9",
                            "--log", self.log, *extra)

    def setUp(self):
        super().setUp()
        self.write_db(("1", "suite.e2e", "tui-echo", 4000))

    def test_within_the_allowance_passes(self):
        self.write_log("timing: suite.e2e#tui-echo 4500ms passed")
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertIn("regression: 1 check(s) within 20% of their median (0 not judged)", out)

    def test_slower_than_the_allowance_fails_and_names_it(self):
        self.write_log("timing: suite.e2e#tui-echo 12000ms passed")
        code, out = self.gate()
        self.assertEqual(code, 1)
        self.assertIn("regression: tui-echo 12.0s, median 4.0s", out)
        self.assertIn("+200%", out)

    def test_a_short_median_is_not_judged(self):
        self.write_db(("1", "suite.e2e", "quick", 2000))
        self.write_log("timing: suite.e2e#quick 9000ms passed")
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertIn("0 check(s) within", out)
        self.assertIn("(1 not judged)", out)

    def test_a_test_with_no_history_is_passed_over(self):
        self.write_log("timing: suite.e2e#brand-new 30000ms passed")
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertIn("(1 not judged)", out)

    def test_the_slack_is_part_of_the_allowance(self):
        self.write_log("timing: suite.e2e#tui-echo 7000ms passed")
        self.assertEqual(self.gate()[0], 1)  # 4s + 20% = 4.8s, +2s slack = 6.8s
        code, out = self.gate("--slack-ms", "3000")
        self.assertEqual(code, 0, out)

    def test_the_run_under_test_does_not_move_its_own_median(self):
        # This run is already in the database, as a re-check would leave it.
        self.write_db(("9", "suite.e2e", "tui-echo", 30000))
        self.write_log("timing: suite.e2e#tui-echo 12000ms passed")
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn("median 4.0s", out)

    def test_a_log_without_timings_is_exit_3(self):
        open(self.log, "w").close()
        code, out = self.gate()
        self.assertEqual(code, 3)
        self.assertIn("regression: no timings", out)

    def test_a_missing_log_is_exit_3(self):
        code, out = self.gate()
        self.assertEqual(code, 3)
        self.assertIn("regression: no timings", out)

    def test_a_failed_line_fails_even_when_the_timing_is_normal(self):
        self.write_log("timing: suite.e2e#tui-echo 4500ms failed")
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn("failed: tui-echo 4.5s", out)
        self.assertNotIn("regression: tui-echo", out)

    def test_ignore_failures_keeps_the_timing_gate(self):
        self.write_log("timing: suite.e2e#tui-echo 4500ms failed")
        code, out = self.gate("--ignore-failures")
        self.assertEqual(code, 0, out)
        self.assertIn("1 check(s) within", out)

    def test_a_failed_test_is_printed_before_a_regression(self):
        # A failure usually explains the test after it that never ran.
        self.write_log(
            "timing: suite.e2e#tui-bg 500ms failed",
            "timing: suite.e2e#tui-echo 12000ms passed",
        )
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertLess(out.index("failed: tui-bg"), out.index("regression: tui-echo"))

    def test_expect_names_how_many_lines_are_missing(self):
        self.write_log(*["timing: suite.e2e#t%d 1000ms passed" % n for n in range(19)])
        code, out = self.gate("--expect", "20")
        self.assertEqual(code, 1, out)
        self.assertIn("expected 20 timing(s), found 19 (1 missing)", out)
        self.write_log("timing: suite.e2e#t19 1000ms passed", mode="a")
        code, out = self.gate("--expect", "20")
        self.assertEqual(code, 0, out)

    def test_require_names_the_absent_tests(self):
        self.write_log("timing: suite.e2e#diag 1000ms passed")
        code, out = self.gate("--require", "diag,redaction")
        self.assertEqual(code, 1, out)
        self.assertIn("required test(s) not in the log: redaction", out)
        # Repeated flags work the same way as the comma list.
        self.write_log("timing: suite.e2e#redaction 1000ms passed", mode="a")
        code, out = self.gate("--require", "diag", "--require", "redaction")
        self.assertEqual(code, 0, out)

    def test_min_judged_fails_a_fresh_database(self):
        os.remove(self.db)
        self.write_log("timing: suite.e2e#tui-echo 4500ms passed")
        # Without the flag, a run with nothing to judge still passes, as before.
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertIn("0 check(s) within", out)
        code, out = self.gate("--min-judged", "5")
        self.assertEqual(code, 1, out)
        self.assertIn("judged: 0 test(s) judged, --min-judged requires 5", out)

    def test_dry_run_reports_without_failing(self):
        self.write_log("timing: suite.e2e#tui-echo 4500ms failed")
        code, out = self.gate("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("failed: tui-echo", out)
        self.assertIn("dry-run: would fail (exit 1)", out)


class StepsTest(Base):
    def test_steps_for_the_newest_run_when_none_is_given(self):
        self.write_log(
            "step:install 1000ms",
            "step:launch 500ms",
            "timing: suite.e2e#tui-echo 2000ms passed",
        )
        self.command("record", "--log", self.log, "--run", "4")
        code, out = self.command("steps")
        self.assertEqual(code, 0)
        self.assertIn("run 4: 2.0s in 1 test(s)", out)
        self.assertIn("1.5s in 2 step(s), 0.5s outside them", out)
        self.assertIn("install", out)
        self.assertIn("launch", out)


class InterfaceTest(Base):
    def test_db_is_accepted_on_either_side_of_the_subcommand(self):
        self.write_log("timing: suite.e2e#tui-echo 2000ms passed")
        before, out_before = run_output(
            ["--db", self.db, "record", "--log", self.log, "--run", "1"])
        after, out_after = run_output(
            ["record", "--db", self.db, "--log", self.log, "--run", "2"])
        self.assertEqual((before, after), (0, 0))
        self.assertEqual(out_before.strip(), "recorded 1 timing(s) for run 1")
        self.assertEqual(out_after.strip(), "recorded 1 timing(s) for run 2")
        db = timings.connect(self.db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM timings").fetchone()[0], 2)

    def test_the_environment_names_the_database(self):
        previous = os.environ.get("TEST_TIMINGS_DB")
        os.environ["TEST_TIMINGS_DB"] = self.db
        try:
            self.assertEqual(timings.default_db(), self.db)
        finally:
            if previous is None:
                del os.environ["TEST_TIMINGS_DB"]
            else:
                os.environ["TEST_TIMINGS_DB"] = previous

    def test_the_default_database_sits_in_the_working_directory(self):
        previous = os.environ.pop("TEST_TIMINGS_DB", None)
        try:
            self.assertEqual(timings.default_db(), os.path.join(os.getcwd(), ".timings.db"))
        finally:
            if previous is not None:
                os.environ["TEST_TIMINGS_DB"] = previous

    def test_check_help_documents_the_exit_codes_and_flags(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            timings.main(["check", "--help"])
        self.assertEqual(raised.exception.code, 0)
        text = out.getvalue()
        for word in ("--expect", "--require", "--min-judged", "--ignore-failures",
                     "--dry-run", "exit codes:", "3  no log"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
