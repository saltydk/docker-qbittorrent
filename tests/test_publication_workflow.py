import json
import os

# Local fixture CLIs use argument lists with an explicit trusted interpreter.
import subprocess  # nosec B404
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".github/workflows/build.yml"
DIGEST = "sha256:" + "d" * 64
VARIANTS = ("libtorrent1", "libtorrent2", "legacy")


def tags(variant):
    return [
        f"saltydk/qbittorrent:release-{variant}-fixture",
        f"saltydk/qbittorrent:{variant}",
    ]


class PublicationWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for dockerfile in ROOT.glob("Dockerfile.*"):
            (self.root / dockerfile.name).symlink_to(dockerfile)
        self.environment = {**os.environ, "PYTHONPATH": str(ROOT)}
        self.report_values = {
            "repository": "saltydk/docker-qbittorrent",
            "source-sha": "a" * 40,
            "static-result": "success",
            "prepare-result": "success",
            "test-result": "success",
            "publish-result": "success",
            "has-builds": "true",
            "should-publish": "true",
            "github-output": str(self.root / "output"),
            "summary": str(self.root / "summary"),
        }

    def cli(self, *arguments, module="scripts.publication"):
        # Only test-selected modules and fixture arguments run; no shell is used.
        return subprocess.run(  # nosec B603
            [sys.executable, "-m", module, *arguments],
            cwd=self.root,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
            shell=False,
        )

    def report(self, variants):
        values = {
            **self.report_values,
            "publish-matrix-json": json.dumps(
                {
                    "include": [
                        {"variant": variant, "tags": tags(variant)}
                        for variant in variants
                    ],
                }
            ),
        }
        arguments = ["report"]
        for name, value in values.items():
            arguments.extend((f"--{name}", value))
        return self.cli(*arguments)

    def outputs(self):
        return dict(
            line.split("=", 1)
            for line in (self.root / "output").read_text().splitlines()
        )

    def outcome(self, variant, eligible, build_result, record_result, digest=""):
        result = self.cli(
            "outcomes",
            "--variant",
            variant,
            "--source-guard",
            "success",
            "--eligible",
            str(eligible).lower(),
            "--build-result",
            build_result,
            "--digest",
            digest,
            "--tags-json",
            json.dumps(tags(variant)),
            "--record-result",
            record_result,
            "--output-directory",
            "published-tags",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def publication(self, variant, eligible):
        outcome = "success" if eligible else "skipped"
        self.outcome(variant, eligible, outcome, outcome, DIGEST if eligible else "")
        if eligible:
            result = self.cli(
                "record",
                "--variant",
                variant,
                "--digest",
                DIGEST,
                "--tags-json",
                json.dumps(tags(variant)),
                "--output-directory",
                "published-tags",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_publication_checkout_has_full_git_history(self):
        publish = (
            BUILD.read_text()
            .split("\n  publish:\n", 1)[1]
            .split("\n  acceptance:\n", 1)[0]
        )
        self.assertIn("        with:\n          fetch-depth: 0\n", publish)

    def test_changed_workflow_steps_call_file_clis(self):
        workflow = BUILD.read_text()
        for command in (
            "scripts.publication_guard",
            "scripts.publication record",
            "scripts.publication outcomes",
            "scripts.publication report",
            "scripts.notification",
        ):
            self.assertIn(f"python3 -m {command}", workflow)
        self.assertNotIn("PY_OUTPUT", workflow)
        self.assertNotIn(".include |= map(", workflow)

    def test_all_superseded_variants_report_no_publication(self):
        for variant in VARIANTS:
            self.publication(variant, False)
        result = self.report(VARIANTS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["has-publications"], "false")
        self.assertEqual(json.loads(self.outputs()["published-images"]), {})
        self.assertEqual(
            json.loads((self.root / "build-report.json").read_text())["status"],
            "superseded",
        )

    def test_mixed_results_record_only_actually_published_variants(self):
        self.publication("libtorrent1", False)
        self.publication("libtorrent2", True)
        result = self.report(("libtorrent1", "libtorrent2"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(self.outputs()["published-images"]),
            {"libtorrent2": f"saltydk/qbittorrent@{DIGEST}"},
        )
        self.assertEqual(
            json.loads(self.outputs()["published-variants"]), ["libtorrent2"]
        )
        self.assertEqual(
            json.loads((self.root / "build-report.json").read_text())["status"],
            "published",
        )

    def test_missing_success_receipts_are_not_mislabeled_as_superseded(self):
        result = self.report(("libtorrent2",))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            json.loads((self.root / "build-report.json").read_text())["status"],
            "failed",
        )
        self.assertFalse((self.root / "output").exists())

    def test_failed_publication_retains_attempted_write_evidence(self):
        self.outcome("libtorrent2", True, "success", "failure", DIGEST)
        self.report_values["publish-result"] = "failure"
        result = self.report(("libtorrent2",))
        self.assertNotEqual(result.returncode, 0)
        report = json.loads((self.root / "build-report.json").read_text())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["outcomes"]["publication-libtorrent2-digest"], DIGEST)
        self.assertEqual(
            report["outcomes"]["publication-libtorrent2-record"], "failure"
        )
        self.assertEqual(
            report["outcomes"]["publication-libtorrent2-tags"],
            ",".join(tags("libtorrent2")),
        )

    def test_partial_receipt_cannot_claim_all_planned_tags_published(self):
        self.publication("libtorrent2", True)
        path = self.root / "published-tags/published-libtorrent2.txt"
        path.write_text(f"saltydk/qbittorrent:libtorrent2@{DIGEST}\n")
        result = self.report(("libtorrent2",))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not cover all selected tags", result.stderr)
        report = json.loads((self.root / "build-report.json").read_text())
        self.assertEqual(report["outcomes"]["publication-libtorrent2-digest"], DIGEST)

    def test_record_requires_a_valid_digest(self):
        result = self.cli(
            "record",
            "--variant",
            "libtorrent2",
            "--digest",
            "invalid",
            "--tags-json",
            json.dumps(tags("libtorrent2")),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "published-libtorrent2.txt").exists())

    def test_notification_cli_filters_mixed_publications(self):
        event = self.root / "event.json"
        event.write_text("{}")
        self.environment.update(
            {
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1",
            }
        )
        result = self.cli(
            "--publish-matrix-json",
            json.dumps({"include": [{"variant": variant} for variant in VARIANTS]}),
            "--published-variants-json",
            '["libtorrent2"]',
            "--publish-result",
            "success",
            module="scripts.notification",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads((self.root / "notification.json").read_text())
        self.assertEqual(
            [field["name"] for field in payload["fields"]], ["libtorrent2"]
        )

    def test_failed_publication_notification_preserves_attempted_variants(self):
        event = self.root / "event.json"
        event.write_text("{}")
        self.environment.update(
            {
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1",
            }
        )
        result = self.cli(
            "--publish-matrix-json",
            json.dumps({"include": [{"variant": variant} for variant in VARIANTS]}),
            "--published-variants-json",
            "",
            "--publish-result",
            "failure",
            module="scripts.notification",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads((self.root / "notification.json").read_text())
        self.assertEqual([field["name"] for field in payload["fields"]], list(VARIANTS))


if __name__ == "__main__":
    unittest.main()
