import hashlib
import json
import os

# Local fixture CLIs use argument lists with an explicit trusted interpreter.
import subprocess  # nosec B404
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.package_locks import PackageLock

ROOT = Path(__file__).resolve().parents[1]


class BuildWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / "github-output"
        self.summary = self.root / "summary"
        self.arguments = self.root / "manager-arguments.json"
        scripts = self.root / "scripts"
        scripts.mkdir()
        (scripts / "manage_builds.py").write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['MANAGER_ARGUMENTS']).write_text(json.dumps(sys.argv[1:]))\n"
            "status = int(os.environ['MANAGER_STATUS'])\n"
            "print('manager stdout')\n"
            "if status:\n"
            "    print('manager stderr', file=sys.stderr)\n"
            "    raise SystemExit(status)\n"
            "if sys.argv[1] == 'matrix':\n"
            "    output = pathlib.Path(sys.argv[sys.argv.index('--github-output') + 1])\n"
            "    with output.open('a') as stream:\n"
            "        stream.write('has-builds=true\\n')\n"
        )
        self.environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "MANAGER_ARGUMENTS": str(self.arguments),
            "MANAGER_STATUS": "0",
        }

    def execute(self, command, **arguments):
        options: list[str] = []
        for name, value in arguments.items():
            options.extend(("--" + name.replace("_", "-"), str(value)))
        # All arguments come from these tests; shell expansion is disabled.
        return subprocess.run(  # nosec B603
            [sys.executable, "-m", "scripts.build_workflow", command, *options],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )

    def verify(self, event="push", called_source=""):
        return self.execute(
            "verify",
            event_name=event,
            called_source_sha=called_source,
            github_output=self.output,
            summary=self.summary,
        )

    def test_successful_verification_preserves_child_output_and_allows_builds(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.arguments.read_text()), ["verify"])
        self.assertIn("manager stdout", result.stdout)
        self.assertEqual(self.output.read_text(), "skip-build=false\n")
        self.assertFalse(self.summary.exists())

    def test_obsolete_inputs_skip_only_an_ordinary_push(self):
        self.environment["MANAGER_STATUS"] = "3"
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "skip-build=true\n")
        self.assertIn("::notice::", result.stdout)
        self.assertIn(
            "version-check workflow can refresh the inputs", self.summary.read_text()
        )

    def test_obsolete_inputs_fail_for_called_pushes_and_other_events(self):
        self.environment["MANAGER_STATUS"] = "3"
        for event, called_source in (
            ("push", "a" * 40),
            ("pull_request", ""),
            ("workflow_dispatch", ""),
            ("schedule", ""),
        ):
            with self.subTest(event=event, called_source=called_source):
                result = self.verify(event, called_source)
                self.assertEqual(result.returncode, 3)
                self.assertIn("manager stderr", result.stderr)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.summary.exists())

    def test_other_verification_failures_are_not_skipped_on_push(self):
        for status in (1, 2, 4):
            with self.subTest(status=status):
                self.environment["MANAGER_STATUS"] = str(status)
                result = self.verify()
                self.assertEqual(result.returncode, status)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.summary.exists())

    def matrix(self, **overrides):
        values = {
            "event_name": "push",
            "before_sha": "before",
            "base_sha": "base",
            "head_sha": "head",
            "called_source_sha": "",
            "selected_variants_json": "",
            "github_output": self.output,
        }
        values.update(overrides)
        return self.execute("matrix", **values)

    def test_matrix_event_selection_preserves_the_existing_precedence(self):
        variants = '["libtorrent2", "legacy"]'
        cases: tuple[tuple[dict[str, str], list[str]], ...] = (
            (
                {
                    "called_source_sha": "source",
                    "selected_variants_json": variants,
                    "event_name": "pull_request",
                },
                ["--variants", variants],
            ),
            ({"called_source_sha": "source"}, ["--all"]),
            (
                {"event_name": "workflow_dispatch", "selected_variants_json": variants},
                ["--all"],
            ),
            ({"event_name": "pull_request"}, ["--before", "base", "--after", "head"]),
            ({}, ["--before", "before", "--after", "head"]),
            (
                {"event_name": "schedule", "selected_variants_json": variants},
                ["--before", "before", "--after", "head"],
            ),
        )
        for values, expected in cases:
            with self.subTest(values=values):
                result = self.matrix(**values)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(self.arguments.read_text()),
                    ["matrix", *expected, "--github-output", str(self.output)],
                )
                self.assertIn("has-builds=true\n", self.output.read_text())

    def test_matrix_child_failure_is_preserved(self):
        self.environment["MANAGER_STATUS"] = "7"
        result = self.matrix()
        self.assertEqual(result.returncode, 7)
        self.assertFalse(self.output.exists())

    def test_lock_digest_uses_all_validated_lock_files_from_the_explicit_root(self):
        source = self.root / "lock-source"
        packages = source / "packages/runtime"
        packages.mkdir(parents=True)
        parent = "alpine:3.24@sha256:" + "a" * 64
        contents = [
            PackageLock(architecture, parent, "b" * 64, {"xz": "5.8.4-r0"}).render()
            for architecture in ("aarch64", "x86_64")
        ]
        for architecture, content in zip(("aarch64", "x86_64"), contents):
            (packages / f"{architecture}.lock").write_text(content)
        self.output.write_text("existing=value\n")
        result = self.execute("locks", source_root=source, github_output=self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        digest = hashlib.sha256("".join(contents).encode()).hexdigest()
        self.assertEqual(
            self.output.read_text(), f"existing=value\nlocks-sha={digest}\n"
        )
        self.assertFalse(self.arguments.exists())

    def test_missing_or_invalid_lock_sets_fail_without_outputs(self):
        result = self.execute("locks", source_root=self.root, github_output=self.output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no package locks found", result.stderr)
        self.assertFalse(self.output.exists())
        packages = self.root / "packages"
        packages.mkdir()
        (packages / "invalid.lock").write_text("invalid\n")
        result = self.execute("locks", source_root=self.root, github_output=self.output)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def requirements(self, **overrides):
        values = {
            "static_result": "success",
            "prepare_result": "success",
            "has_builds": "true",
            "test_result": "success",
            "publish_result": "success",
            "should_publish": "true",
        }
        values.update(overrides)
        return self.execute("requirements", **values)

    def test_all_required_jobs_success_allows_the_workflow(self):
        result = self.requirements()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_required_jobs_reject_the_workflow(self):
        for option, status in (
            ("static_result", "failure"),
            ("prepare_result", "skipped"),
            ("test_result", "cancelled"),
            ("publish_result", "failure"),
        ):
            with self.subTest(option=option, status=status):
                result = self.requirements(**{option: status})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(status, result.stderr)

    def test_no_builds_require_only_static_and_prepare(self):
        result = self.requirements(
            has_builds="false", test_result="skipped", publish_result="failure"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.requirements(
            has_builds="false", static_result="failure", test_result="skipped"
        )
        self.assertNotEqual(result.returncode, 0)

    def test_candidate_only_builds_do_not_require_publication(self):
        result = self.requirements(should_publish="false", publish_result="skipped")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.requirements(
            should_publish="false", test_result="failure", publish_result="skipped"
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
