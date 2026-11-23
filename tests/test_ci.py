"""Tests for the CI-facing flags: --config, --summary and the example flow.

Run with `python3 -m unittest discover -s tests`.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_cli import Base, run_output  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_timings as timings  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigTest(Base):
    def setUp(self):
        super().setUp()
        self.write_db(("1", "suite.e2e", "tui-echo", 4000))
        self.config = os.path.join(self.tmp.name, "timings.json")

    def write_config(self, values, path=None):
        path = path or self.config
        with open(path, "w") as stream:
            json.dump(values, stream)
        return path

    def gate(self, *extra):
        return self.command("check", "--class", "suite.e2e", "--run", "9",
                            "--log", self.log, *extra)

    def config_error(self, path=None, text=None):
        path = path or self.write_config({"db": self.db})
        if text is not None:
            with open(path, "w") as stream:
                stream.write(text)
        error = io.StringIO()
        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            self.gate("--config", path)
        self.assertEqual(raised.exception.code, 2)
        return error.getvalue()

    def test_config_sets_thresholds_and_a_flag_wins(self):
        self.write_log("timing: suite.e2e#tui-echo 4500ms passed")
        self.write_config({"max_regression": 10, "slack_ms": 0})
        code, out = self.gate("--config", self.config)
        self.assertEqual(code, 1, out)  # 4.0s + 10% = 4.4s, and 4.5s is slower
        self.assertIn("median 4.0s", out)
        code, out = self.gate("--config", self.config,
                              "--max-regression", "20", "--slack-ms", "2000")
        self.assertEqual(code, 0, out)  # the flags replace both file values
        self.assertIn("within 20%", out)

    def test_config_requires_tests_and_a_flag_replaces_the_list(self):
        self.write_log("timing: suite.e2e#tui-echo 4500ms passed")
        self.write_config({"require": ["diag", "redaction"]})
        code, out = self.gate("--config", self.config)
        self.assertEqual(code, 1, out)
        self.assertIn("required test(s) not in the log: diag, redaction", out)
        code, out = self.gate("--config", self.config, "--require", "tui-echo")
        self.assertEqual(code, 0, out)

    def test_config_accepts_a_comma_string_for_require(self):
        self.write_log("timing: suite.e2e#tui-echo 4500ms passed")
        self.write_config({"require": "diag, redaction"})
        code, out = self.gate("--config", self.config)
        self.assertEqual(code, 1, out)
        self.assertIn("required test(s) not in the log: diag, redaction", out)

    def test_config_sets_min_ms_and_min_judged(self):
        self.write_db(("1", "suite.e2e", "quick", 2000))
        self.write_log("timing: suite.e2e#quick 9000ms passed")
        self.write_config({"min_ms": 1000, "slack_ms": 0})
        code, out = self.gate("--config", self.config)
        self.assertEqual(code, 1, out)  # judged, and 9.0s is over 2.2s
        self.assertIn("quick 9.0s, median 2.0s", out)
        code, out = self.gate("--config", self.config, "--min-ms", "3000")
        self.assertEqual(code, 0, out)  # the flag makes the median too short
        self.assertIn("0 check(s) within", out)

        self.write_db(("2", "suite.e2e", "tui-echo", 4000))
        self.write_log("timing: suite.e2e#tui-echo 4500ms passed")
        self.write_config({"min_judged": 2})
        code, out = self.gate("--config", self.config)
        self.assertEqual(code, 1, out)
        self.assertIn("judged: 1 test(s) judged, --min-judged requires 2", out)

    def test_the_config_names_the_database_and_the_flag_wins(self):
        self.write_log("timing: suite.e2e#tui-echo 12000ms passed")
        other = os.path.join(self.tmp.name, "other.db")
        self.write_config({"db": self.db})
        code, out = run_output(
            ["check", "--class", "suite.e2e", "--run", "9",
             "--log", self.log, "--config", self.config])
        self.assertEqual(code, 1, out)  # the config's database has the baseline
        self.assertIn("median 4.0s", out)
        code, out = run_output(
            ["check", "--class", "suite.e2e", "--run", "9",
             "--log", self.log, "--config", self.config, "--db", other])
        self.assertEqual(code, 0, out)  # an empty database has no baseline
        self.assertIn("(1 not judged)", out)

    def test_missing_keys_keep_the_defaults(self):
        self.write_log("timing: suite.e2e#tui-echo 4500ms passed")
        self.write_config({"db": self.db})
        code, out = self.gate("--config", self.config)
        self.assertEqual(code, 0, out)
        self.assertIn("within 20%", out)  # the built-in max regression
        self.assertIn("(0 not judged)", out)

    def test_config_is_accepted_on_either_side_of_the_subcommand(self):
        self.write_log("timing: suite.e2e#tui-echo 4500ms passed")
        self.write_config({"max_regression": 10, "slack_ms": 0})
        code, out = run_output(
            ["--config", self.config, "check", "--class", "suite.e2e",
             "--run", "9", "--log", self.log, "--db", self.db])
        self.assertEqual(code, 1, out)
        self.assertIn("median 4.0s", out)

    def test_config_errors_are_clear_and_exit_2(self):
        missing = os.path.join(self.tmp.name, "no.json")
        error = self.config_error(path=missing)
        self.assertIn("--config", error)
        self.assertIn("no.json", error)

        error = self.config_error(text="{not json")
        self.assertIn("not valid JSON", error)

        error = self.config_error(text="[]")
        self.assertIn("must hold one JSON object", error)

        error = self.config_error(text='{"max_judged": 5}')
        self.assertIn("unknown key(s): max_judged", error)
        self.assertIn("max_regression", error)  # the allowed keys are listed

        error = self.config_error(text='{"max_regression": "20"}')
        self.assertIn("max_regression must be a number", error)

        error = self.config_error(text='{"min_ms": true}')
        self.assertIn("min_ms must be an integer", error)

        error = self.config_error(text='{"require": [1]}')
        self.assertIn("require must be a string or a list of strings", error)

        error = self.config_error(text='{"db": ""}')
        self.assertIn("db must be a non-empty string", error)


class SummaryTest(Base):
    def setUp(self):
        super().setUp()
        self.summary = os.path.join(self.tmp.name, "summary.md")

    def test_summary_has_counts_and_the_three_worst_tests(self):
        self.write_db(
            ("1", "suite.e2e", "slow-a", 4000),
            ("1", "suite.e2e", "slow-b", 4000),
            ("1", "suite.e2e", "slow-c", 4000),
            ("1", "suite.e2e", "slow-d", 4000),
        )
        self.write_log(
            "timing: suite.e2e#slow-a 12000ms passed",
            "timing: suite.e2e#slow-b 9000ms passed",
            "timing: suite.e2e#slow-c 8000ms passed",
            "timing: suite.e2e#slow-d 7000ms passed",
            "timing: suite.e2e#flaky 1000ms failed",
        )
        code, out = self.command(
            "check", "--class", "suite.e2e", "--run", "9", "--log", self.log,
            "--expect", "6", "--require", "absent", "--json",
            "--summary", self.summary)
        self.assertEqual(code, 1, out)
        verdict = json.loads(out)
        self.assertFalse(verdict["ok"])

        with open(self.summary) as stream:
            text = stream.read()
        self.assertIn("## test-timings: failed", text)
        self.assertIn("| judged | slower | failed | missing |", text)
        self.assertIn("| --- | --- | --- | --- |", text)
        self.assertIn("| 4 | 4 | 1 | 2 |", text)  # 1 expected + 1 required missing
        self.assertIn("| slow-a | 12.0s | 4.0s | 6.8s | +200% |", text)
        self.assertIn("| slow-b | 9.0s | 4.0s | 6.8s | +125% |", text)
        self.assertIn("| slow-c | 8.0s | 4.0s | 6.8s | +100% |", text)
        self.assertNotIn("slow-d", text)  # only the worst three
        self.assertNotIn("| flaky |", text)

    def test_a_passing_run_still_writes_a_table(self):
        self.write_db(("1", "suite.e2e", "tui-echo", 4000))
        self.write_log("timing: suite.e2e#tui-echo 4500ms passed")
        code, out = self.command(
            "check", "--class", "suite.e2e", "--run", "9", "--log", self.log,
            "--summary", self.summary)
        self.assertEqual(code, 0, out)
        with open(self.summary) as stream:
            text = stream.read()
        self.assertIn("## test-timings: passed", text)
        self.assertIn("| 1 | 0 | 0 | 0 |", text)

    def test_a_dry_run_summary_says_what_would_happen(self):
        self.write_db(("1", "suite.e2e", "tui-echo", 4000))
        self.write_log("timing: suite.e2e#tui-echo 12000ms passed")
        code, out = self.command(
            "check", "--class", "suite.e2e", "--run", "9", "--log", self.log,
            "--dry-run", "--summary", self.summary)
        self.assertEqual(code, 0, out)
        with open(self.summary) as stream:
            text = stream.read()
        self.assertIn("## test-timings: would fail (dry-run)", text)
        self.assertIn("| 1 | 1 | 0 | 0 |", text)

    def test_the_summary_is_appended(self):
        self.write_db(("1", "suite.e2e", "tui-echo", 4000))
        self.write_log("timing: suite.e2e#tui-echo 4500ms passed")
        with open(self.summary, "w") as stream:
            stream.write("# earlier output\n")
        for _ in range(2):
            code, out = self.command(
                "check", "--class", "suite.e2e", "--run", "9", "--log", self.log,
                "--summary", self.summary)
            self.assertEqual(code, 0, out)
        with open(self.summary) as stream:
            text = stream.read()
        self.assertTrue(text.startswith("# earlier output\n\n## test-timings: passed"))
        self.assertEqual(text.count("## test-timings: passed"), 2)

    def test_the_environment_is_not_read_on_its_own(self):
        # The env var is passed explicitly in the examples; --summary is the only
        # thing that writes, so a local run cannot leak into a CI summary.
        previous = os.environ.get("GITHUB_STEP_SUMMARY")
        os.environ["GITHUB_STEP_SUMMARY"] = self.summary
        try:
            self.write_db(("1", "suite.e2e", "tui-echo", 4000))
            self.write_log("timing: suite.e2e#tui-echo 4500ms passed")
            code, out = self.command(
                "check", "--class", "suite.e2e", "--run", "9", "--log", self.log)
            self.assertEqual(code, 0, out)
        finally:
            if previous is None:
                os.environ.pop("GITHUB_STEP_SUMMARY", None)
            else:
                os.environ["GITHUB_STEP_SUMMARY"] = previous
        self.assertFalse(os.path.exists(self.summary))

    def test_an_empty_log_writes_a_failed_summary_and_stays_exit_3(self):
        open(self.log, "w").close()
        code, out = self.command(
            "check", "--class", "suite.e2e", "--run", "9", "--log", self.log,
            "--summary", self.summary)
        self.assertEqual(code, 3, out)
        with open(self.summary) as stream:
            text = stream.read()
        self.assertIn("## test-timings: failed", text)
        self.assertIn("| 0 | 0 | 0 | 0 |", text)

    def test_a_summary_that_cannot_be_written_is_exit_2(self):
        self.write_db(("1", "suite.e2e", "tui-echo", 4000))
        self.write_log("timing: suite.e2e#tui-echo 4500ms passed")
        bad = os.path.join(self.tmp.name, "missing-dir", "summary.md")
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            code, out = self.command(
                "check", "--class", "suite.e2e", "--run", "9", "--log", self.log,
                "--summary", bad)
        self.assertEqual(code, 2, out)
        self.assertIn("summary: cannot write", error.getvalue())


class ExampleFlowTest(unittest.TestCase):
    def test_local_demo_seeds_history_then_fails_with_a_summary(self):
        with tempfile.TemporaryDirectory() as work:
            process = subprocess.run(
                ["bash", os.path.join(ROOT, "examples", "local-demo.sh"), work],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0,
                             process.stdout + process.stderr)
            with open(os.path.join(work, "verdict.json")) as stream:
                verdict = json.load(stream)
            self.assertFalse(verdict["ok"])
            self.assertEqual(
                [row["test"] for row in verdict["checks"]
                 if row.get("verdict") == "slower"],
                ["beta"])
            with open(os.path.join(work, "step-summary.md")) as stream:
                summary = stream.read()
            self.assertIn("## test-timings: failed", summary)
            self.assertIn("| 2 | 1 | 0 | 0 |", summary)
            self.assertIn("| beta | 12.0s | 6.0s | 9.2s | +100% |", summary)

    def test_the_example_shell_script_parses(self):
        for name in ("fake-suite.sh", "local-demo.sh", "shell.sh"):
            path = os.path.join(ROOT, "examples", name)
            process = subprocess.run(["bash", "-n", path],
                                     capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)


class HelpTest(Base):
    def test_help_mentions_config_and_summary(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            timings.main(["check", "--help"])
        self.assertEqual(raised.exception.code, 0)
        text = out.getvalue()
        self.assertIn("--config", text)
        self.assertIn("--summary", text)


if __name__ == "__main__":
    unittest.main()
