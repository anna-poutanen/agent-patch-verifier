from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

import patch_verifier.runner as runner_module
from patch_verifier.runner import (
    CommandStatus,
    OUTPUT_LIMIT_BYTES,
    PYTHONHASHSEED,
    Revision,
    RevisionWorkspace,
    RunOptions,
    RunStability,
    TestCommand as Command,
    classify_exit_code,
    classify_junit_result,
    junit_failure_signature,
    run_dual,
    run_revision,
)


class RunnerTests(unittest.TestCase):
    def test_capture_finishing_within_shared_drain_budget_is_not_truncated(
        self,
    ) -> None:
        capture = runner_module._OutputCapture()
        thread = threading.Thread(
            target=lambda: time.sleep(
                runner_module._PIPE_DRAIN_SECONDS * 0.6
            ),
            daemon=True,
        )
        thread.start()

        runner_module._finish_captures(((None, thread, capture),))

        self.assertFalse(thread.is_alive())
        self.assertFalse(capture.truncated)

    def test_captures_command_evidence_and_appends_test_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            script = (
                "import os, sys; "
                "print(os.getcwd()); "
                "print(os.environ['PYTHONHASHSEED']); "
                "print(sys.argv[1]); "
                "print('diagnostic', file=sys.stderr)"
            )
            command = Command(
                (sys.executable, "-c", script),
                test_path=Path("tests/generated_case.py"),
            )

            result = run_revision(
                RevisionWorkspace(Revision.BASE, workspace),
                command,
                RunOptions(environment={"PYTHONHASHSEED": "random"}),
            )

            attempt = result.attempts[0]
            self.assertEqual(attempt.status, CommandStatus.PASS)
            self.assertEqual(attempt.exit_code, 0)
            self.assertEqual(
                attempt.stdout.splitlines(),
                [str(workspace.resolve()), PYTHONHASHSEED, "tests/generated_case.py"],
            )
            self.assertEqual(attempt.stderr, "diagnostic\n")
            self.assertGreaterEqual(attempt.duration_seconds, 0)
            self.assertEqual(attempt.argv, command.argv)

    def test_classifies_pytest_style_exit_codes(self) -> None:
        self.assertEqual(classify_exit_code(0), CommandStatus.PASS)
        self.assertEqual(classify_exit_code(1), CommandStatus.FAIL)
        for exit_code in (2, 3, 4, 5, -9):
            with self.subTest(exit_code=exit_code):
                self.assertEqual(classify_exit_code(exit_code), CommandStatus.ERROR)

    def test_timeout_is_captured_and_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            command = Command(
                (
                    sys.executable,
                    "-c",
                    "import time; print('started', flush=True); time.sleep(30)",
                )
            )

            result = run_revision(
                RevisionWorkspace(Revision.BASE, Path(temporary_directory)),
                command,
                RunOptions(timeout_seconds=0.1),
            )

            attempt = result.attempts[0]
            self.assertEqual(attempt.status, CommandStatus.TIMEOUT)
            self.assertIsNone(attempt.exit_code)
            self.assertEqual(attempt.stdout, "started\n")
            self.assertLess(attempt.duration_seconds, 5)

    def test_launch_failure_is_an_error_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = os.path.join(temporary_directory, "missing-python")
            result = run_revision(
                RevisionWorkspace(Revision.BASE, Path(temporary_directory)),
                Command((missing,)),
            )

            attempt = result.attempts[0]
            self.assertEqual(attempt.status, CommandStatus.ERROR)
            self.assertIsNone(attempt.exit_code)
            self.assertIn("FileNotFoundError", attempt.stderr)

    def test_subprocess_output_is_bounded_and_marked_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = run_revision(
                RevisionWorkspace(Revision.BASE, Path(temporary_directory)),
                Command(
                    (
                        sys.executable,
                        "-c",
                        f"import sys; sys.stdout.write('x' * {OUTPUT_LIMIT_BYTES + 1000})",
                    )
                ),
            )

            attempt = result.attempts[0]
            self.assertEqual(len(attempt.stdout.encode("utf-8")), OUTPUT_LIMIT_BYTES)
            self.assertTrue(attempt.stdout_truncated)
            self.assertFalse(attempt.stderr_truncated)

    def test_repeated_disagreement_is_unstable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            script = (
                "from pathlib import Path; "
                "import sys; "
                "p=Path('attempt'); "
                "n=int(p.read_text()) if p.exists() else 0; "
                "p.write_text(str(n+1)); "
                "sys.exit(n % 2)"
            )

            result = run_revision(
                RevisionWorkspace(Revision.PATCHED, workspace),
                Command((sys.executable, "-c", script)),
                RunOptions(repeat=3),
            )

            self.assertEqual(
                [attempt.status for attempt in result.attempts],
                [CommandStatus.PASS, CommandStatus.FAIL, CommandStatus.PASS],
            )
            self.assertEqual(result.stability, RunStability.UNSTABLE)
            self.assertFalse(result.is_stable)
            self.assertIsNone(result.status)

    def test_repeated_agreement_has_a_stable_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = run_revision(
                RevisionWorkspace(Revision.BASE, Path(temporary_directory)),
                Command((sys.executable, "-c", "raise SystemExit(1)")),
                RunOptions(repeat=2),
            )

            self.assertEqual(result.stability, RunStability.STABLE)
            self.assertTrue(result.is_stable)
            self.assertEqual(result.status, CommandStatus.FAIL)

    def test_dual_run_uses_each_workspace_and_preserves_both_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            base = root / "base"
            patched = root / "patched"
            base.mkdir()
            patched.mkdir()
            (base / "exit-code").write_text("0", encoding="utf-8")
            (patched / "exit-code").write_text("1", encoding="utf-8")
            script = (
                "from pathlib import Path; "
                "raise SystemExit(int(Path('exit-code').read_text()))"
            )

            result = run_dual(
                base,
                patched,
                Command((sys.executable, "-c", script)),
            )

            self.assertEqual(result.base.status, CommandStatus.PASS)
            self.assertEqual(result.patched.status, CommandStatus.FAIL)
            self.assertEqual(result.base.attempts[0].workspace, base)
            self.assertEqual(result.patched.attempts[0].workspace, patched)
            self.assertIs(result.for_revision(Revision.BASE), result.base)
            self.assertIs(result.for_revision(Revision.PATCHED), result.patched)

    def test_dual_run_alternates_revision_order_to_expose_shared_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            base = root / "base"
            patched = root / "patched"
            base.mkdir()
            patched.mkdir()
            counter = root / "counter"
            script = (
                "from pathlib import Path; import sys; "
                f"p=Path({str(counter)!r}); "
                "n=int(p.read_text()) if p.exists() else 0; "
                "p.write_text(str(n+1)); sys.exit(0 if n < 2 else 1)"
            )

            result = run_dual(
                base,
                patched,
                Command((sys.executable, "-c", script)),
                RunOptions(repeat=2),
            )

            self.assertEqual(result.base.stability, RunStability.UNSTABLE)
            self.assertEqual(result.patched.stability, RunStability.UNSTABLE)

    def test_workspace_pythonpath_precedes_inherited_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            shadow = root / "shadow"
            (workspace / "src" / "samplepkg").mkdir(parents=True)
            (shadow / "samplepkg").mkdir(parents=True)
            (workspace / "src" / "samplepkg" / "__init__.py").write_text(
                "VALUE = 'workspace'\n", encoding="utf-8"
            )
            (shadow / "samplepkg" / "__init__.py").write_text(
                "VALUE = 'shadow'\n", encoding="utf-8"
            )

            result = run_revision(
                RevisionWorkspace(Revision.BASE, workspace),
                Command(
                    (
                        sys.executable,
                        "-c",
                        "import samplepkg; print(samplepkg.VALUE)",
                    )
                ),
                RunOptions(environment={"PYTHONPATH": str(shadow)}),
            )

            self.assertEqual(result.attempts[0].stdout, "workspace\n")

    def test_workspace_environment_cannot_select_the_callers_path_or_git_repo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            script = (
                "import os; print(os.getcwd()); print(os.environ['PWD']); "
                "print(os.environ.get('GIT_DIR', '<unset>')); "
                "print(os.environ.get('OLDPWD', '<unset>'))"
            )

            result = run_revision(
                RevisionWorkspace(Revision.BASE, workspace),
                Command((sys.executable, "-c", script)),
                RunOptions(
                    environment={
                        "PWD": "/wrong",
                        "OLDPWD": "/caller",
                        "GIT_DIR": "/wrong/.git",
                    }
                ),
            )

            lines = result.attempts[0].stdout.splitlines()
            assert lines == [
                str(workspace.resolve()),
                str(workspace.resolve()),
                "<unset>",
                "<unset>",
            ]

    def test_junit_setup_error_is_not_an_assertion_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            test_file = workspace / "test_case.py"
            test_file.write_text(
                "import pytest\n\n"
                "@pytest.fixture\n"
                "def broken():\n    raise RuntimeError('setup failed')\n\n"
                "def test_value(broken):\n    assert True\n",
                encoding="utf-8",
            )
            command = Command(
                (sys.executable, "-m", "pytest", "-q", "test_case.py::test_value"),
                junit_xml_path=Path("result.xml"),
            )

            result = run_revision(
                RevisionWorkspace(Revision.BASE, workspace), command
            )

            self.assertEqual(result.status, CommandStatus.ERROR)
            self.assertIn("1 error", result.attempts[0].stdout)

    def test_junit_assertion_failure_remains_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "test_case.py").write_text(
                "def test_value():\n    assert 1 == 2\n", encoding="utf-8"
            )
            command = Command(
                (sys.executable, "-m", "pytest", "-q", "test_case.py::test_value"),
                junit_xml_path=Path("result.xml"),
            )

            result = run_revision(
                RevisionWorkspace(Revision.BASE, workspace), command
            )

            self.assertEqual(result.status, CommandStatus.FAIL)
            self.assertIsNotNone(result.attempts[0].failure_signature)

    def test_junit_test_code_exception_is_a_harness_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "test_case.py").write_text(
                "def test_value():\n    missing_name()\n    assert True\n",
                encoding="utf-8",
            )
            command = Command(
                (sys.executable, "-m", "pytest", "-q", "test_case.py::test_value"),
                junit_xml_path=Path("result.xml"),
                expected_node="test_value",
            )

            result = run_revision(
                RevisionWorkspace(Revision.BASE, workspace), command
            )

            self.assertEqual(result.status, CommandStatus.ERROR)

    def test_junit_item_must_match_expected_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "test_case.py").write_text(
                "def test_value():\n    assert True\n", encoding="utf-8"
            )
            command = Command(
                (sys.executable, "-m", "pytest", "-q", "test_case.py::test_value"),
                junit_xml_path=Path("result.xml"),
                expected_node="test_other",
            )

            result = run_revision(
                RevisionWorkspace(Revision.BASE, workspace), command
            )

            self.assertEqual(result.status, CommandStatus.ERROR)

    def test_inherited_pytest_options_do_not_expand_candidate_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "test_case.py").write_text(
                "def test_value():\n    assert True\n", encoding="utf-8"
            )
            command = Command(
                (sys.executable, "-m", "pytest", "-q", "test_case.py::test_value"),
                junit_xml_path=Path("result.xml"),
                expected_node="test_value",
            )
            previous = os.environ.get("PYTEST_ADDOPTS")
            os.environ["PYTEST_ADDOPTS"] = "--collect-only"
            try:
                result = run_revision(
                    RevisionWorkspace(Revision.BASE, workspace), command
                )
            finally:
                if previous is None:
                    os.environ.pop("PYTEST_ADDOPTS", None)
                else:
                    os.environ["PYTEST_ADDOPTS"] = previous

            self.assertEqual(result.status, CommandStatus.PASS)

    def test_timeout_does_not_wait_for_escaped_child_pipe(self) -> None:
        if os.name != "posix":
            self.skipTest("process-session behavior is POSIX-specific")
        with tempfile.TemporaryDirectory() as temporary_directory:
            child = (
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1)'], "
                "start_new_session=True); "
                "print('started', flush=True); time.sleep(30)"
            )
            started = __import__("time").perf_counter()
            result = run_revision(
                RevisionWorkspace(Revision.BASE, Path(temporary_directory)),
                Command((sys.executable, "-c", child)),
                RunOptions(timeout_seconds=0.1),
            )
            elapsed = __import__("time").perf_counter() - started

            self.assertEqual(result.status, CommandStatus.TIMEOUT)
            self.assertLess(elapsed, 0.8)

    def test_classify_junit_requires_machine_readable_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing.xml"
            self.assertEqual(
                classify_junit_result(1, missing), CommandStatus.ERROR
            )

    def test_oversized_junit_is_rejected_without_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            oversized = Path(temporary_directory) / "oversized.xml"
            oversized.write_bytes(b"x" * (OUTPUT_LIMIT_BYTES + 1))

            self.assertEqual(
                classify_junit_result(1, oversized), CommandStatus.ERROR
            )
            self.assertIsNone(junit_failure_signature(oversized))

    def test_malformed_junit_counts_are_harness_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            malformed = Path(temporary_directory) / "malformed.xml"
            malformed.write_text(
                '<testsuite tests="not-a-number">'
                '<testcase name="test_x" />'
                '</testsuite>',
                encoding="utf-8",
            )

            self.assertEqual(
                classify_junit_result(0, malformed), CommandStatus.ERROR
            )

    def test_junit_requires_exactly_one_non_skipped_item(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "test_case.py").write_text(
                "import pytest\n\n"
                "@pytest.mark.parametrize('value', [1, 2])\n"
                "def test_values(value):\n    assert value\n\n"
                "@pytest.mark.skip(reason='not evidence')\n"
                "def test_skipped():\n    assert True\n",
                encoding="utf-8",
            )
            many = Command(
                (sys.executable, "-m", "pytest", "-q", "test_case.py::test_values"),
                junit_xml_path=Path("many.xml"),
            )
            skipped = Command(
                (sys.executable, "-m", "pytest", "-q", "test_case.py::test_skipped"),
                junit_xml_path=Path("skipped.xml"),
            )

            many_result = run_revision(
                RevisionWorkspace(Revision.BASE, workspace), many
            )
            skipped_result = run_revision(
                RevisionWorkspace(Revision.BASE, workspace), skipped
            )

            self.assertEqual(many_result.status, CommandStatus.ERROR)
            self.assertEqual(skipped_result.status, CommandStatus.ERROR)

    def test_options_and_empty_commands_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one"):
            Command(())
        with self.assertRaisesRegex(ValueError, "repeat"):
            RunOptions(repeat=0)
        with self.assertRaisesRegex(ValueError, "timeout"):
            RunOptions(timeout_seconds=0)
        for invalid_timeout in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=invalid_timeout):
                with self.assertRaisesRegex(ValueError, "finite"):
                    RunOptions(timeout_seconds=invalid_timeout)
        with self.assertRaisesRegex(TypeError, "environment"):
            RunOptions(environment={"COUNT": 1})  # type: ignore[dict-item]


if __name__ == "__main__":
    unittest.main()
