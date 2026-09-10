import hashlib
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.build_report import (
    ReportError,
    aggregate_reports,
    main,
    render_markdown,
    snapshot_baseline,
    verify_candidate,
    write_report,
)


DIGEST = "sha256:" + "d" * 64
LOCK_TEXT = """# apk-lock: 1
# architecture: x86_64
# parent: alpine:3.24@sha256:{parent}
# requests-sha256: {requests}
busybox=1.37.0-r1
zlib=1.3.1-r2
""".format(parent="a" * 64, requests="b" * 64)


def inspect_output() -> str:
    return json.dumps(
        {
            "manifest": {"digest": DIGEST},
            "image": {
                "linux/amd64": {
                    "config": {
                        "Labels": {
                            "org.opencontainers.image.revision": "c" * 40,
                        }
                    }
                }
            },
        }
    )


def inventory_output(*, embedded_digest: str = "", embedded_lock: str = "") -> str:
    return (
        "__APK_LOCK_ARCH__=x86_64\n"
        f"__APK_LOCK_DIGEST__={embedded_digest}\n"
        "__APK_LOCK_CONTENT__\n"
        f"{embedded_lock}"
        "__APK_LOCK_INVENTORY__\n"
        "busybox=1.37.0-r1\n"
        "zlib=1.3.1-r2\n"
    )


def candidate_labels() -> dict[str, str]:
    return {
        "org.opencontainers.image.revision": "d" * 40,
        "org.opencontainers.image.base.name": "alpine:3.24@sha256:" + "2" * 64,
        "io.saltydk.qbittorrent.release": "release-5.2.4_v2.0.11",
        "io.saltydk.qbittorrent.revision": "1",
        "io.saltydk.qbittorrent.sha256.amd64": "3" * 64,
        "io.saltydk.qbittorrent.sha256.arm64": "4" * 64,
    }


class BaselineTests(unittest.TestCase):
    def test_snapshot_resolves_tag_before_reading_inventory_with_overridden_entrypoint(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command[1:4] == ["buildx", "imagetools", "inspect"]:
                return subprocess.CompletedProcess(command, 0, inspect_output(), "")
            return subprocess.CompletedProcess(
                command,
                0,
                "__APK_LOCK_ARCH__=x86_64\n"
                "__APK_LOCK_DIGEST__=\n"
                "__APK_LOCK_CONTENT__\n"
                "__APK_LOCK_INVENTORY__\n"
                "busybox 1.37.0-r1 x86_64 {installed}\n"
                "zlib 1.3.1-r2 x86_64 {installed}\n",
                "",
            )

        report = snapshot_baseline(
            "example/base",
            "base",
            "linux/amd64",
            "runtime",
            "registry.example/base:latest",
            runner=runner,
        )

        baseline = report["images"][0]["baseline"]
        self.assertEqual(baseline["status"], "available")
        self.assertEqual(baseline["digest"], DIGEST)
        self.assertEqual(baseline["revision"], "c" * 40)
        self.assertEqual(baseline["labels"]["org.opencontainers.image.revision"], "c" * 40)
        self.assertEqual(
            baseline["inventory"],
            {"busybox": "1.37.0-r1", "zlib": "1.3.1-r2"},
        )
        self.assertIsNone(report["images"][0]["changes"])
        run_command = commands[1]
        self.assertIn("registry.example/base:latest@" + DIGEST, run_command)
        self.assertEqual(run_command[run_command.index("--entrypoint") + 1], "/bin/sh")

    def test_builder_snapshot_uses_retained_builder_lock_instead_of_runtime_inventory(self) -> None:
        builder_lock = LOCK_TEXT.replace("zlib=1.3.1-r2\n", "")
        builder_digest = hashlib.sha256(builder_lock.encode()).hexdigest()

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            if command[1:4] == ["buildx", "imagetools", "inspect"]:
                return subprocess.CompletedProcess(command, 0, inspect_output(), "")
            return subprocess.CompletedProcess(
                command,
                0,
                inventory_output(embedded_digest=builder_digest, embedded_lock=builder_lock),
                "",
            )

        report = snapshot_baseline(
            "example/base",
            "base",
            "linux/amd64",
            "builder",
            "registry.example/base:latest",
            runner=runner,
        )

        baseline = report["images"][0]["baseline"]
        self.assertEqual(baseline["status"], "available")
        self.assertEqual(baseline["inventory_source"], "embedded-lock")
        self.assertEqual(baseline["inventory"], {"busybox": "1.37.0-r1"})

    def test_builder_snapshot_marks_legacy_image_without_retained_lock_unavailable(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            if command[1:4] == ["buildx", "imagetools", "inspect"]:
                return subprocess.CompletedProcess(command, 0, inspect_output(), "")
            return subprocess.CompletedProcess(command, 0, inventory_output(), "")

        report = snapshot_baseline(
            "example/base",
            "base",
            "linux/amd64",
            "builder",
            "registry.example/base:latest",
            runner=runner,
        )

        baseline = report["images"][0]["baseline"]
        self.assertEqual(baseline["status"], "unavailable")
        self.assertNotIn("inventory", baseline)

    def test_snapshot_treats_only_confirmed_manifest_absence_as_first_publication(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "manifest unknown")

        report = snapshot_baseline(
            "example/base",
            "base",
            "linux/amd64",
            "runtime",
            "registry.example/base:latest",
            runner=runner,
        )

        self.assertEqual(report["status"], "pending-publication")
        self.assertEqual(report["images"][0]["baseline"]["status"], "absent")
        self.assertNotIn("inventory", report["images"][0]["baseline"])
        self.assertIsNone(report["images"][0]["changes"])

    def test_snapshot_recognizes_docker_hubs_exact_missing_tag_error(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "ERROR: docker.io/example/base:first-release: not found",
            )

        report = snapshot_baseline(
            "example/base",
            "base",
            "linux/amd64",
            "runtime",
            "example/base:first-release",
            runner=runner,
        )

        self.assertEqual(report["status"], "pending-publication")
        self.assertEqual(report["images"][0]["baseline"]["status"], "absent")

    def test_snapshot_fails_closed_on_registry_auth_errors(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "unauthorized: authentication required")

        with self.assertRaisesRegex(ReportError, "authentication required"):
            snapshot_baseline(
                "example/base",
                "base",
                "linux/amd64",
                "runtime",
                "registry.example/base:latest",
                runner=runner,
            )


class CandidateTests(unittest.TestCase):
    def test_verification_compares_full_inventory_and_reports_actual_baseline_delta(self) -> None:
        lock_digest = hashlib.sha256(LOCK_TEXT.encode()).hexdigest()
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command[1:3] == ["image", "inspect"]:
                return subprocess.CompletedProcess(command, 0, json.dumps(candidate_labels()), "")
            if command[1:4] == ["buildx", "imagetools", "inspect"]:
                return subprocess.CompletedProcess(command, 0, inspect_output(), "")
            return subprocess.CompletedProcess(
                command,
                0,
                inventory_output(embedded_digest=lock_digest, embedded_lock=LOCK_TEXT),
                "",
            )

        baseline = {
            "schema": 1,
            "kind": "build",
            "repository": "example/base",
            "status": "built",
            "changed_files": [],
            "outcomes": {},
            "published": [],
            "images": [
                {
                    "name": "base",
                    "platform": "linux/amd64",
                    "stage": "runtime",
                    "changes": [],
                    "inputs": [],
                    "baseline": {
                        "reference": "registry.example/base:latest",
                        "digest": "sha256:" + "e" * 64,
                        "revision": "f" * 40,
                        "status": "available",
                        "inventory": {"busybox": "1.36.1-r0", "zlib": "1.3.1-r2"},
                        "labels": {
                            "org.opencontainers.image.revision": "f" * 40,
                            "org.opencontainers.image.base.name": "alpine:3.23@sha256:" + "1" * 64,
                            "io.saltydk.qbittorrent.release": "release-5.2.4_v2.0.11",
                            "io.saltydk.qbittorrent.sha256.amd64": "5" * 64,
                            "io.saltydk.qbittorrent.sha256.arm64": "6" * 64,
                        },
                    },
                }
            ],
        }
        with TemporaryDirectory() as directory:
            lock_path = Path(directory) / "x86_64.lock"
            lock_path.write_text(LOCK_TEXT, encoding="utf-8")
            report = verify_candidate(
                "example/base",
                "base",
                "linux/amd64",
                "runtime",
                "registry.example/base:candidate",
                lock_path,
                baseline,
                runner=runner,
            )

        image = report["images"][0]
        self.assertEqual(report["status"], "built")
        self.assertEqual(
            image["changes"],
            [{"name": "busybox", "old": "1.36.1-r0", "new": "1.37.0-r1"}],
        )
        self.assertEqual(image["verification"]["status"], "passed")
        self.assertEqual(image["verification"]["lock_digest"], lock_digest)
        self.assertEqual(image["verification"]["embedded_lock_digest"], lock_digest)
        self.assertEqual(
            image["inputs"],
            [
                {"name": "base image", "old": "alpine:3.23@sha256:" + "1" * 64, "new": "alpine:3.24@sha256:" + "2" * 64},
                {"name": "qBittorrent binary SHA-256", "old": "5" * 64, "new": "3" * 64},
                {"name": "qBittorrent revision", "old": None, "new": "1"},
                {"name": "source revision", "old": "f" * 40, "new": "d" * 40},
            ],
        )
        self.assertNotIn("4" * 64, json.dumps(image["inputs"]))
        self.assertIn("/usr/share/image-inputs/runtime.lock", commands[1][-1])

    def test_verification_fails_when_candidate_has_an_unlocked_package(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            if command[1:3] == ["image", "inspect"]:
                return subprocess.CompletedProcess(command, 0, json.dumps(candidate_labels()), "")
            if command[1:4] == ["buildx", "imagetools", "inspect"]:
                return subprocess.CompletedProcess(command, 0, inspect_output(), "")
            return subprocess.CompletedProcess(
                command,
                0,
                inventory_output() + "unexpected=9.9-r0\n",
                "",
            )

        with TemporaryDirectory() as directory:
            lock_path = Path(directory) / "x86_64.lock"
            lock_path.write_text(LOCK_TEXT, encoding="utf-8")
            report = verify_candidate(
                "example/base",
                "base",
                "linux/amd64",
                "runtime",
                "registry.example/base:candidate",
                lock_path,
                None,
                runner=runner,
            )

        self.assertEqual(report["status"], "failed")
        self.assertIsNone(report["images"][0]["changes"])
        self.assertIn("unexpected: expected absent, actual 9.9-r0", report["images"][0]["verification"]["error"])

    def test_verification_requires_the_embedded_candidate_lock(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            if command[1:3] == ["image", "inspect"]:
                return subprocess.CompletedProcess(command, 0, json.dumps(candidate_labels()), "")
            return subprocess.CompletedProcess(command, 0, inventory_output(), "")

        with TemporaryDirectory() as directory:
            lock_path = Path(directory) / "x86_64.lock"
            lock_path.write_text(LOCK_TEXT, encoding="utf-8")
            report = verify_candidate(
                "example/base",
                "base",
                "linux/amd64",
                "runtime",
                "registry.example/base:candidate",
                lock_path,
                None,
                runner=runner,
            )

        self.assertEqual(report["status"], "failed")
        self.assertIn("embedded runtime lock is unavailable", report["error"])

    def test_verification_rejects_embedded_lock_content_that_does_not_match_its_digest(self) -> None:
        lock_digest = hashlib.sha256(LOCK_TEXT.encode()).hexdigest()

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            if command[1:3] == ["image", "inspect"]:
                return subprocess.CompletedProcess(command, 0, json.dumps(candidate_labels()), "")
            altered = LOCK_TEXT.replace("busybox=1.37.0-r1", "busybox=1.36.1-r0")
            return subprocess.CompletedProcess(
                command,
                0,
                inventory_output(embedded_digest=lock_digest, embedded_lock=altered),
                "",
            )

        with TemporaryDirectory() as directory:
            lock_path = Path(directory) / "x86_64.lock"
            lock_path.write_text(LOCK_TEXT, encoding="utf-8")
            report = verify_candidate(
                "example/base",
                "base",
                "linux/amd64",
                "runtime",
                "registry.example/base:candidate",
                lock_path,
                None,
                runner=runner,
            )

        self.assertEqual(report["status"], "failed")
        self.assertIn("embedded lock content", report["error"])

    def test_verification_rejects_lock_for_another_platform(self) -> None:
        arm_lock = LOCK_TEXT.replace("# architecture: x86_64", "# architecture: aarch64")
        with TemporaryDirectory() as directory:
            lock_path = Path(directory) / "aarch64.lock"
            lock_path.write_text(arm_lock, encoding="utf-8")
            report = verify_candidate(
                "example/base",
                "base",
                "linux/amd64",
                "runtime",
                "registry.example/base:candidate",
                lock_path,
                None,
                runner=lambda command: subprocess.CompletedProcess(command, 99, "", "must not run"),
            )

        self.assertEqual(report["status"], "failed")
        self.assertIn("x86_64", report["images"][0]["verification"]["error"])


class RenderingTests(unittest.TestCase):
    def test_markdown_renders_machine_statuses_as_human_labels(self) -> None:
        expected = {
            "waiting-for-base": "Waiting for base image",
            "pending-publication": "Publication pending",
            "update-available": "Updates available",
            "no-changes": "No changes",
            "failed": "Failed",
            "built": "Built",
            "published": "Published",
        }
        for status, label in expected.items():
            with self.subTest(status=status):
                markdown = render_markdown(
                    {
                        "schema": 1,
                        "kind": "update",
                        "repository": "example/repo",
                        "status": status,
                        "changed_files": [],
                        "outcomes": {},
                        "published": [],
                        "images": [],
                    }
                )
                self.assertIn(f"- Status: {label}", markdown)

    def test_markdown_groups_identical_rows_and_escapes_diagnostics(self) -> None:
        report = {
            "schema": 1,
            "kind": "update",
            "repository": "example/repo",
            "status": "update-available",
            "source_sha": "a" * 40,
            "changed_files": ["Dockerfile", "packages/runtime/x86_64.lock"],
            "outcomes": {"scan": "ok|warn"},
            "published": [],
            "error": "bad | value\n<script>alert(1)</script>",
            "images": [
                {
                    "name": name,
                    "platform": platform,
                    "stage": "runtime",
                    "changes": [{"name": "busybox", "old": "1-r0", "new": "2-r0"}],
                    "inputs": [{"name": "source", "old": "v1", "new": "v2"}],
                    "baseline": {"reference": "example:latest", "status": "error"},
                }
                for name, platform in (("one", "linux/amd64"), ("two", "linux/arm64"))
            ],
        }

        markdown = render_markdown(report)

        self.assertEqual(markdown.count("| busybox | 1-r0 | 2-r0 |"), 1)
        self.assertEqual(markdown.count("| source | v1 | v2 |"), 1)
        self.assertIn("one (linux/amd64, runtime)<br>two (linux/arm64, runtime)", markdown)
        self.assertIn("ok\\|warn", markdown)
        self.assertIn("bad \\| value &lt;script&gt;alert(1)&lt;/script&gt;", markdown)
        self.assertIn("Unavailable", markdown)

    def test_markdown_distinguishes_unavailable_delta_from_no_changes(self) -> None:
        unavailable = {
            "schema": 1,
            "kind": "build",
            "repository": "example/repo",
            "status": "failed",
            "changed_files": [],
            "outcomes": {},
            "published": [],
            "images": [
                {
                    "name": "one",
                    "platform": "linux/amd64",
                    "stage": "runtime",
                    "changes": None,
                    "inputs": [],
                }
            ],
        }
        unchanged = json.loads(json.dumps(unavailable))
        unchanged["status"] = "built"
        unchanged["images"][0]["changes"] = []

        self.assertIn("Package delta unavailable", render_markdown(unavailable))
        self.assertIn("No package changes", render_markdown(unchanged))

    def test_markdown_does_not_claim_no_changes_when_update_collected_no_images(self) -> None:
        report = {
            "schema": 1,
            "kind": "update",
            "repository": "example/repo",
            "changed_files": [],
            "outcomes": {},
            "published": [],
            "images": [],
        }

        for status in ("failed", "waiting-for-base"):
            with self.subTest(status=status):
                report["status"] = status
                markdown = render_markdown(report)
                self.assertIn("Package delta unavailable", markdown)
                self.assertNotIn("No package changes", markdown)

        report["status"] = "no-changes"
        self.assertIn("No package changes", render_markdown(report))

    def test_write_report_replaces_json_and_appends_markdown_summary(self) -> None:
        report = {
            "schema": 1,
            "kind": "build",
            "repository": "example/repo",
            "status": "built",
            "changed_files": [],
            "outcomes": {},
            "published": [],
            "images": [],
        }
        with TemporaryDirectory() as directory:
            json_path = Path(directory) / "report.json"
            summary_path = Path(directory) / "summary.md"
            summary_path.write_text("Existing summary\n", encoding="utf-8")

            result = write_report(report, json_path=json_path, summary_path=summary_path)

            self.assertIsNone(result)
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), report)
            self.assertTrue(json_path.read_text(encoding="utf-8").endswith("\n"))
            self.assertTrue(summary_path.read_text(encoding="utf-8").startswith("Existing summary\n# Build report"))


class AggregateTests(unittest.TestCase):
    def test_aggregate_cli_reports_source_only_changed_files_from_real_git_history(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            tracked = root / "source.txt"
            tracked.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "source.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--quiet", "-m", "feat: add source input"],
                check=True,
            )
            previous = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            tracked.write_text("after\n", encoding="utf-8")
            (root / "added.txt").write_text("new\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "source.txt", "added.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--quiet", "-m", "fix: update source input"],
                check=True,
            )
            current = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            image_report = root / "image.json"
            image_report.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "kind": "build",
                        "repository": "example/repo",
                        "status": "built",
                        "changed_files": [],
                        "outcomes": {},
                        "published": [],
                        "images": [{"name": "base", "platform": "linux/amd64", "stage": "runtime", "changes": [], "inputs": [], "baseline": {"status": "available", "revision": previous}}],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "aggregate.json"

            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                exit_code = main(
                    [
                        "aggregate", "--repository", "example/repo", "--kind", "build",
                        "--status", "built", "--source-sha", current,
                        "--source-root", str(root), "--image-report", str(image_report),
                        "--output", str(output),
                    ]
                )
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["changed_files"], ["added.txt", "source.txt"])
        self.assertEqual(
            report["source_comparisons"],
            [{
                "previous": previous,
                "current": current,
                "status": "compared",
                "targets": ["base (linux/amd64, runtime)"],
                "changed_files": ["added.txt", "source.txt"],
            }],
        )
        markdown = render_markdown(report)
        self.assertIn(f"| base (linux/amd64, runtime) | `{previous}` | `{current}` | compared |", markdown)

    def test_missing_baseline_commit_marks_source_comparison_unavailable_without_failing_build(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            (root / "source.txt").write_text("current\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "source.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--quiet", "-m", "feat: add current source"],
                check=True,
            )
            current = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            image_report = {
                "schema": 1,
                "kind": "build",
                "repository": "example/repo",
                "status": "built",
                "changed_files": ["packages/runtime/x86_64.lock"],
                "outcomes": {},
                "published": [],
                "images": [{"name": "base", "platform": "linux/amd64", "stage": "runtime", "changes": None, "inputs": [], "baseline": {"status": "available", "revision": "f" * 40}}],
            }

            report = aggregate_reports(
                "example/repo", "build", "built", [image_report],
                source_sha=current, source_root=root,
            )

        self.assertEqual(report["status"], "built")
        self.assertEqual(report["changed_files"], ["packages/runtime/x86_64.lock"])
        comparison = report["source_comparisons"][0]
        self.assertEqual(comparison["status"], "unavailable")
        self.assertIn("git diff", comparison["error"])

    def test_invalid_source_revisions_are_rejected_before_git_execution(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            trace = root / "git.trace"
            cases = ((None, "a" * 40), ("main", "a" * 40), ("b" * 40, "HEAD"))
            for previous, current in cases:
                with self.subTest(previous=previous, current=current):
                    trace.unlink(missing_ok=True)
                    image_report = {
                        "schema": 1,
                        "kind": "build",
                        "repository": "example/repo",
                        "status": "built",
                        "changed_files": [],
                        "outcomes": {},
                        "published": [],
                        "images": [{"name": "base", "platform": "linux/amd64", "stage": "runtime", "changes": None, "inputs": [], "baseline": {"status": "unavailable", "revision": previous}}],
                    }
                    with patch.dict(os.environ, {"GIT_TRACE": str(trace)}):
                        report = aggregate_reports(
                            "example/repo", "build", "built", [image_report],
                            source_sha=current, source_root=root,
                        )

                    self.assertFalse(trace.exists())
                    self.assertEqual(report["source_comparisons"][0]["status"], "unavailable")

    def test_verified_unknown_delta_wins_over_updater_lock_delta_in_both_report_orders(self) -> None:
        updater = {
            "schema": 1,
            "kind": "update",
            "repository": "example/repo",
            "status": "update-available",
            "changed_files": [],
            "outcomes": {},
            "published": [],
            "images": [{"name": "base", "platform": "linux/amd64", "stage": "runtime", "changes": [{"name": "busybox", "old": "1", "new": "2"}], "inputs": []}],
        }
        for baseline_status in ("absent", "unavailable", "failed"):
            verified = {
                "schema": 1,
                "kind": "build",
                "repository": "example/repo",
                "status": "built",
                "changed_files": [],
                "outcomes": {},
                "published": [],
                "images": [{"name": "base", "platform": "linux/amd64", "stage": "runtime", "changes": None, "inputs": [], "baseline": {"status": baseline_status}, "verification": {"status": "passed", "delta_status": "unavailable"}}],
            }
            for reports in ((updater, verified), (verified, updater)):
                with self.subTest(baseline_status=baseline_status, verified_first=reports[0] is verified):
                    report = aggregate_reports("example/repo", "build", "built", reports)
                    self.assertIsNone(report["images"][0]["changes"])

    def test_aggregate_uses_verified_inputs_with_verified_package_delta(self) -> None:
        update = {
            "schema": 1,
            "kind": "update",
            "repository": "example/repo",
            "status": "update-available",
            "changed_files": ["Dockerfile"],
            "outcomes": {},
            "published": [],
            "images": [{"name": "base", "platform": "linux/amd64", "stage": "runtime", "changes": None, "inputs": [{"name": "Alpine parent", "old": "3.23", "new": "3.24"}]}],
        }
        verified = {
            "schema": 1,
            "kind": "build",
            "repository": "example/repo",
            "status": "built",
            "changed_files": [],
            "outcomes": {},
            "published": [],
            "images": [{"name": "base", "platform": "linux/amd64", "stage": "runtime", "changes": [{"name": "busybox", "old": "1", "new": "2"}], "inputs": [{"name": "source revision", "old": "a", "new": "b"}], "verification": {"status": "passed"}}],
        }

        report = aggregate_reports("example/repo", "build", "published", [update, verified])

        self.assertEqual(len(report["images"]), 1)
        self.assertEqual(report["images"][0]["changes"], verified["images"][0]["changes"])
        self.assertEqual(
            report["images"][0]["inputs"],
            [
                {"name": "source revision", "old": "a", "new": "b"},
            ],
        )

    def test_verified_input_rows_win_over_planned_rows_in_both_report_orders(self) -> None:
        updater = {
            "schema": 1,
            "kind": "update",
            "repository": "example/repo",
            "status": "update-available",
            "changed_files": [],
            "outcomes": {},
            "published": [],
            "images": [{
                "name": "base",
                "platform": "linux/amd64",
                "stage": "runtime",
                "changes": [],
                "inputs": [
                    {"name": "base image", "old": "B", "new": "C"},
                    {"name": "planned only", "old": "1", "new": "2"},
                ],
            }],
        }
        for actual_old in ("A", None):
            verified = {
                "schema": 1,
                "kind": "build",
                "repository": "example/repo",
                "status": "built",
                "changed_files": [],
                "outcomes": {},
                "published": [],
                "images": [{
                    "name": "base",
                    "platform": "linux/amd64",
                    "stage": "runtime",
                    "changes": [],
                    "inputs": [{"name": "base image", "old": actual_old, "new": "C"}],
                    "verification": {"status": "passed"},
                }],
            }
            for reports in ((updater, verified), (verified, updater)):
                with self.subTest(actual_old=actual_old, verified_first=reports[0] is verified):
                    report = aggregate_reports("example/repo", "build", "built", reports)
                    self.assertEqual(
                        report["images"][0]["inputs"],
                        [{"name": "base image", "old": actual_old, "new": "C"}],
                    )

    def test_failed_image_cannot_be_masked_by_published_aggregate_status(self) -> None:
        passing = {
            "schema": 1,
            "kind": "build",
            "repository": "example/repo",
            "status": "built",
            "changed_files": [],
            "outcomes": {"amd64": "passed"},
            "published": [],
            "images": [{"name": "base", "platform": "linux/amd64", "stage": "runtime", "changes": [], "inputs": [], "verification": {"status": "passed"}}],
        }
        failing = json.loads(json.dumps(passing))
        failing["status"] = "failed"
        failing["outcomes"] = {"arm64": "failed"}
        failing["images"][0]["platform"] = "linux/arm64"
        failing["images"][0]["verification"] = {"status": "failed", "error": "wrong package"}

        report = aggregate_reports(
            "example/repo",
            "build",
            "published",
            [passing, failing],
            outcomes={"publish": "completed"},
            published=["example/repo:latest"],
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["outcomes"], {"amd64": "passed", "arm64": "failed", "publish": "completed"})
        self.assertEqual(report["published"], ["example/repo:latest"])
        self.assertIn("wrong package", report["error"])

    def test_cli_writes_failed_snapshot_artifact_on_unexpected_read_error(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "dial tcp: network unreachable")

        with TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                exit_code = main(
                    [
                        "snapshot",
                        "--repository", "example/repo",
                        "--image", "base",
                        "--platform", "linux/amd64",
                        "--stage", "runtime",
                        "--reference", "example/repo:latest",
                        "--output", str(output),
                    ],
                    runner=runner,
                )

            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertIn("network unreachable", report["error"])


if __name__ == "__main__":
    unittest.main()
