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


class VersionCheckDeferralTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.summary = self.root / "summary"
        self.environment = {
            **os.environ,
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "GITHUB_OUTPUT": str(self.root / "output"),
            "GITHUB_STEP_SUMMARY": str(self.summary),
        }
        path = self.root / "python3"
        path.write_text('#!/bin/sh\nexit "$FAKE_UPDATE_EXIT"\n')
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

    def test_waiting_for_base_reports_the_scheduled_retry(self) -> None:
        self.environment["FAKE_UPDATE_EXIT"] = "4"
        self.environment.pop("GH_TOKEN", None)
        result = self.run_step("Resolve image inputs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Waiting for the scheduled base refresh", result.stdout)
        self.assertIn("next scheduled or manual version check will retry", self.summary.read_text())


if __name__ == "__main__":
    unittest.main()
