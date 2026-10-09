from __future__ import annotations

import contextlib
import io
import json
import os
import shutil

# Git and fixture CLIs run as argument lists without a shell.
import subprocess  # nosec B404
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar
from unittest.mock import patch

from scripts import publication_guard as guard

TAG = "saltydk/qbittorrent:libtorrent1"
ALIAS = "saltydk/qbittorrent:latest"
VERSIONED = "saltydk/qbittorrent:release-5.2.4_v1.2.20-2"
DIGEST = "sha256:" + "d" * 64
_git = shutil.which("git")
if _git is None:
    raise RuntimeError("publication guard tests require git")
GIT: str = _git


class PublicationGuardTests(unittest.TestCase):
    directory: ClassVar[TemporaryDirectory[str]]
    fixture: ClassVar[Path]
    baseline: ClassVar[str]
    source: ClassVar[str]
    later: ClassVar[str]
    unrelated: ClassVar[str]

    @classmethod
    def setUpClass(cls):
        cls.directory = TemporaryDirectory()
        cls.fixture = Path(cls.directory.name)
        cls.git("init", "-q", "-b", "main")
        cls.git("config", "user.email", "test@example.invalid")
        cls.git("config", "user.name", "publication test")
        cls.git("config", "commit.gpgsign", "false")
        commits = []
        for name in ("baseline", "candidate", "later"):
            (cls.fixture / "marker").write_text(name)
            cls.git("add", "marker")
            cls.git("commit", "-q", "-m", f"test(publication): create {name} fixture")
            commits.append(cls.git("rev-parse", "HEAD").strip())
        cls.baseline, cls.source, cls.later = commits
        cls.git("checkout", "-q", "-b", "unrelated", cls.baseline)
        (cls.fixture / "marker").write_text("unrelated")
        cls.git("commit", "-qam", "test(publication): create unrelated fixture")
        cls.unrelated = cls.git("rev-parse", "HEAD").strip()
        cls.git("checkout", "-q", "main")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    @classmethod
    def git(cls, *args):
        # Resolve Git once before fixture PATH wrappers and use local test arguments.
        return subprocess.check_output(  # nosec B603
            [GIT, *args],
            cwd=cls.fixture,
            text=True,
            shell=False,
        )

    def setUp(self):
        self.commands = []

    def runner(self, registry):
        def execute(command):
            self.commands.append(command)
            if command[0] == "git":
                # Guard-generated Git arguments operate only on the disposable fixture.
                return subprocess.run(  # nosec B603
                    [GIT, *command[1:]],
                    cwd=self.fixture,
                    text=True,
                    capture_output=True,
                    check=False,
                    shell=False,
                )
            self.assertEqual(command[:4], ["docker", "buildx", "imagetools", "inspect"])
            value = registry.get(command[4])
            if isinstance(value, subprocess.CompletedProcess):
                return value
            if value is None:
                return subprocess.CompletedProcess(command, 1, "", "manifest unknown")
            revisions = (
                value
                if isinstance(value, dict)
                else {platform: value for platform in guard.PLATFORMS}
            )
            metadata = {
                "manifest": {"digest": DIGEST},
                "image": {
                    platform: {
                        "config": {
                            "Labels": {"org.opencontainers.image.revision": revision}
                        }
                    }
                    for platform, revision in revisions.items()
                },
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(metadata), "")

        return execute

    def check(self, registry, tags=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return guard.check_publication(
                self.source, tags or [TAG], self.runner(registry)
            )

    def test_branch_advancement_without_new_publication_is_allowed(self):
        self.assertEqual(self.git("rev-parse", "main").strip(), self.later)
        self.assertTrue(self.check({TAG: self.baseline}))
        self.assertFalse(any("ls-remote" in command for command in self.commands))

    def test_same_source_is_allowed(self):
        self.assertTrue(self.check({TAG: self.source}))

    def test_published_descendant_is_a_successful_no_op(self):
        self.assertFalse(self.check({TAG: self.later}))

    def test_divergent_published_source_is_a_successful_no_op(self):
        self.assertFalse(self.check({TAG: self.unrelated}))

    def test_every_written_alias_and_versioned_tag_is_checked(self):
        for reference in (TAG, ALIAS, VERSIONED):
            with self.subTest(reference=reference):
                registry = {tag: self.baseline for tag in (TAG, ALIAS, VERSIONED)}
                registry[reference] = self.later
                self.assertFalse(self.check(registry, [TAG, ALIAS, VERSIONED]))

    def test_confirmed_manifest_absence_allows_bootstrap(self):
        for detail in (
            "manifest unknown",
            "MANIFEST_UNKNOWN",
            "name unknown",
            "no such manifest",
            f"ERROR: {TAG}: not found",
        ):
            with self.subTest(detail=detail):
                self.assertTrue(
                    self.check({TAG: subprocess.CompletedProcess([], 1, "", detail)})
                )

    def test_access_and_transport_failures_do_not_allow_bootstrap(self):
        for detail in (
            "ERROR: failed to authorize: credential helper: not found",
            "ERROR: resolving registry address: not found",
            "404 Not Found",
            "ERROR: unrelated/image:tag: not found",
            f"ERROR: failed to authorize: {TAG}: not found",
            "unauthorized: manifest unknown",
            "TLS connection timeout",
        ):
            with self.subTest(detail=detail), self.assertRaises(guard.PublicationError):
                self.check({TAG: subprocess.CompletedProcess([], 1, "", detail)})

    def test_superseded_tag_does_not_hide_later_registry_failure(self):
        failure = subprocess.CompletedProcess([], 1, "", "TLS connection timeout")
        with self.assertRaisesRegex(guard.PublicationError, "failed to inspect"):
            self.check({TAG: self.later, ALIAS: failure}, [TAG, ALIAS])

    def test_missing_or_mixed_platform_labels_are_rejected(self):
        for value in (
            "",
            {"linux/amd64": self.baseline},
            {"linux/amd64": self.baseline, "linux/arm64": self.source},
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(guard.PublicationError, "source metadata"),
            ):
                self.check({TAG: value})

    def test_malformed_successful_inspection_is_rejected(self):
        for payload in (
            "not json",
            "{}",
            '{"manifest":{"digest":"invalid"},"image":{}}',
        ):
            with (
                self.subTest(payload=payload),
                self.assertRaises(guard.PublicationError),
            ):
                self.check({TAG: subprocess.CompletedProcess([], 0, payload, "")})

    def test_unknown_published_history_is_rejected(self):
        with self.assertRaisesRegex(guard.PublicationError, "full Git history"):
            self.check({TAG: "f" * 40})

    def test_unknown_candidate_cannot_bootstrap(self):
        with self.assertRaisesRegex(
            guard.PublicationError, "source commit is unavailable"
        ):
            guard.check_publication("f" * 40, [TAG], self.runner({}))
        self.assertFalse(any(command[0] == "docker" for command in self.commands))

    def test_missing_docker_is_not_confirmed_manifest_absence(self):
        with (
            patch.object(guard.shutil, "which", return_value=None),
            patch.object(guard.subprocess, "run") as execute,
            self.assertRaisesRegex(guard.PublicationError, "required executable"),
        ):
            guard.published_revision(TAG, guard.run)
        execute.assert_not_called()

    def test_unsupported_executor_cannot_launch(self):
        with (
            patch.object(guard.subprocess, "run") as execute,
            self.assertRaisesRegex(guard.PublicationError, "requires Git or Docker"),
        ):
            guard.run(["sh", "-c", "exit 0"])
        execute.assert_not_called()

    def test_shallow_history_is_rejected_before_publication(self):
        runner = self.runner({TAG: self.baseline})

        def shallow(command):
            if command == ["git", "rev-parse", "--is-shallow-repository"]:
                return subprocess.CompletedProcess(command, 0, "true\n", "")
            return runner(command)

        with self.assertRaisesRegex(guard.PublicationError, "full Git history"):
            guard.check_publication(self.source, [TAG], shallow)

    def cli_eligibility(self, published, source_ref="refs/heads/main"):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            inspected = Path(directory) / "inspected"
            metadata = {
                "manifest": {"digest": DIGEST},
                "image": {
                    platform: {
                        "config": {
                            "Labels": {"org.opencontainers.image.revision": published}
                        }
                    }
                    for platform in guard.PLATFORMS
                },
            }
            docker = Path(directory) / "docker"
            docker.write_text(
                "#!/bin/sh\nprintf called > '" + str(inspected) + "'\n"
                "printf '%s\\n' '" + json.dumps(metadata) + "'\n"
            )
            docker.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": directory + os.pathsep + os.environ["PATH"],
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            }
            # The fixture interpreter/module and arguments are fixed by this test.
            result = subprocess.run(  # nosec B603
                [
                    sys.executable,
                    "-m",
                    "scripts.publication_guard",
                    "--source-sha",
                    self.source,
                    "--tags-json",
                    json.dumps([TAG]),
                    "--source-ref",
                    source_ref,
                    "--default-branch",
                    "main",
                    "--github-output",
                    str(output),
                ],
                cwd=self.fixture,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                shell=False,
            )
            return (
                result,
                output.read_text() if output.exists() else "",
                inspected.exists(),
            )

    def test_cli_exports_no_op_without_failure(self):
        result, output, inspected = self.cli_eligibility(self.later)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "eligible=false\n")
        self.assertTrue(inspected)

    def test_feature_dispatch_skips_alias_publication_without_registry_reads(self):
        result, output, inspected = self.cli_eligibility(
            self.baseline, "refs/heads/feature"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "eligible=false\n")
        self.assertFalse(inspected)

    def test_default_branch_updater_source_stays_eligible_after_branch_advancement(
        self,
    ):
        self.assertEqual(self.git("rev-parse", "main").strip(), self.later)
        result, output, inspected = self.cli_eligibility(self.baseline)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "eligible=true\n")
        self.assertTrue(inspected)


if __name__ == "__main__":
    unittest.main()
