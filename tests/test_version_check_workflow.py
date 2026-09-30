import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/version-check.yml"


def run_script(name: str) -> str:
    step = WORKFLOW.read_text().split(f"      - name: {name}\n", 1)[1]
    lines = []
    for line in step.split("        run: |\n", 1)[1].splitlines():
        if line and not line.startswith("          "):
            break
        lines.append(line)
    return textwrap.dedent("\n".join(lines))


class BaseRefreshWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.summary = self.root / "summary"
        self.gh_log = self.root / "gh-arguments"
        self.environment = {
            **os.environ,
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "GH_TOKEN": "test-token",
            "GITHUB_OUTPUT": str(self.root / "output"),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "FAKE_GH_LOG": str(self.gh_log),
            "FAKE_GH_EXIT": "0",
        }
        for name, source in {
            "python3": '#!/bin/sh\nexit "$FAKE_UPDATE_EXIT"\n',
            "gh": '#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_GH_LOG"\nexit "$FAKE_GH_EXIT"\n',
        }.items():
            path = self.root / name
            path.write_text(source)
            path.chmod(0o755)

    def run_step(self, name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", run_script(name)],
            cwd=self.root, env=self.environment, capture_output=True, text=True,
        )

    def test_resolution_defers_only_a_required_base_refresh(self) -> None:
        for code, expected in ((0, 0), (4, 0), (1, 1), (2, 2), (3, 3), (137, 137)):
            with self.subTest(code=code):
                self.environment["FAKE_UPDATE_EXIT"] = str(code)
                result = self.run_step("Resolve image inputs")
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_dispatch_requests_package_refresh_on_the_base_default_branch(self) -> None:
        result = self.run_step("Request base image refresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.gh_log.read_text().splitlines(), [
            "workflow", "run", "request-refresh.yml", "--repo", "saltydk/docker-alpine-s6overlay",
            "--ref", "master",
        ])
        self.assertIn("next scheduled or manual image update will retry", self.summary.read_text())

    def test_dispatch_failure_does_not_report_a_queued_refresh(self) -> None:
        self.environment["FAKE_GH_EXIT"] = "1"
        result = self.run_step("Request base image refresh")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.gh_log.exists())
        self.assertFalse(self.summary.exists())
        self.assertNotIn("::notice::", result.stdout)

    def test_missing_cross_repository_token_fails_without_dispatch(self) -> None:
        self.environment["GH_TOKEN"] = ""
        result = self.run_step("Request base image refresh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GH_TOKEN must have Actions write access", result.stdout)
        self.assertFalse(self.gh_log.exists())
        self.assertFalse(self.summary.exists())


if __name__ == "__main__":
    unittest.main()
