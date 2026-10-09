import json
import os

# Local fixture CLIs use argument lists with an explicit trusted interpreter.
import subprocess  # nosec B404
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "d" * 64
NEWER_DIGEST = "sha256:" + "e" * 64
VARIANTS = ("legacy", "libtorrent1", "libtorrent2")
PLATFORMS = ("linux/amd64", "linux/arm64")


class PublicationSecurityTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.github_output = self.root / "github-output"
        self.output = self.root / "report.json"
        self.expected = self.root / "expected-targets.json"
        self.registry = self.root / "registry.json"
        self.commands = self.root / "registry-commands.jsonl"
        binary_directory = self.root / "bin"
        binary_directory.mkdir()
        docker = binary_directory / "docker"
        docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "with pathlib.Path(os.environ['REGISTRY_COMMANDS']).open('a') as output:\n"
            "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "responses = json.loads(pathlib.Path(os.environ['REGISTRY_FIXTURE']).read_text())\n"
            "response = responses[sys.argv[4]]\n"
            "if response.get('returncode', 0):\n"
            "    print(response.get('stderr', 'registry error'), file=sys.stderr)\n"
            "    raise SystemExit(response['returncode'])\n"
            "print(response.get('raw') or json.dumps({'manifest': {'digest': response['digest']}}))\n"
        )
        docker.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": str(binary_directory) + os.pathsep + os.environ["PATH"],
            "PYTHONPATH": str(ROOT),
            "REGISTRY_FIXTURE": str(self.registry),
            "REGISTRY_COMMANDS": str(self.commands),
        }

    def execute(self, command, *arguments, module=True):
        script = (
            ["-m", "scripts.publication_security"]
            if module
            else [str(ROOT / "scripts/publication_security.py")]
        )
        # Test-owned module/file paths and arguments run without shell expansion.
        return subprocess.run(  # nosec B603
            [
                sys.executable,
                *script,
                command,
                "--github-output",
                str(self.github_output),
                "--output",
                str(self.output),
                *arguments,
            ],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )

    def outputs(self):
        return dict(
            line.split("=", 1) for line in self.github_output.read_text().splitlines()
        )

    def targets(self, variants=VARIANTS):
        targets = [
            {
                "name": variant,
                "platform": platform,
                "image": f"saltydk/qbittorrent@{DIGEST}",
                "tracking_reference": f"saltydk/qbittorrent:{variant}",
            }
            for variant in variants
            for platform in PLATFORMS
        ]
        self.expected.write_text(json.dumps(targets))
        return targets

    def registry_responses(self, **overrides):
        responses = {variant: {"digest": DIGEST} for variant in VARIANTS}
        responses.update(overrides)
        self.registry.write_text(
            json.dumps(
                {
                    f"saltydk/qbittorrent:{variant}": response
                    for variant, response in responses.items()
                }
            )
        )

    def scope(self):
        return self.execute("scope", "--expected-targets", str(self.expected))

    def test_missing_docker_is_an_error_instead_of_a_successful_deferral(self):
        self.targets()
        self.environment["PATH"] = str(self.root)
        result = self.scope()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required Docker executable is unavailable", result.stderr)
        self.assertFalse(self.github_output.exists())
        self.assertFalse(self.commands.exists())

    def test_recorded_targets_scan_only_the_published_digest_and_variants(self):
        result = self.execute(
            "targets",
            "--published-images-json",
            json.dumps(
                {
                    "libtorrent2": f"saltydk/qbittorrent@{DIGEST}",
                }
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = self.outputs()
        self.assertEqual(outputs["recorded"], "true")
        self.assertEqual(outputs["expected-targets"], str(self.output))
        targets = json.loads(self.output.read_text())
        self.assertEqual(
            targets,
            [
                {
                    "name": "libtorrent2",
                    "platform": platform,
                    "image": f"saltydk/qbittorrent@{DIGEST}",
                    "tracking_reference": "saltydk/qbittorrent:libtorrent2",
                }
                for platform in PLATFORMS
            ],
        )
        matrix = json.loads(outputs["matrix"])["include"]
        self.assertEqual(
            matrix,
            [
                {
                    **target,
                    "slug": target["platform"].replace("/", "-"),
                    "architecture": architecture,
                }
                for target, architecture in zip(targets, ("x86_64", "aarch64"))
            ],
        )

    def test_scheduled_targets_snapshot_all_mutable_variant_tags(self):
        result = self.execute("targets", "--published-images-json", "", module=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = self.outputs()
        self.assertEqual(outputs["recorded"], "false")
        self.assertEqual(
            json.loads(outputs["targets"]),
            [
                {
                    "name": variant,
                    "platform": platform,
                    "image": f"saltydk/qbittorrent:{variant}",
                }
                for variant in VARIANTS
                for platform in PLATFORMS
            ],
        )
        self.assertNotIn("matrix", outputs)
        self.assertFalse(self.output.exists())

    def test_invalid_recorded_images_do_not_emit_snapshot_fallback(self):
        for payload in (
            "{",
            "{}",
            "[]",
            '{"unknown":"saltydk/qbittorrent@' + DIGEST + '"}',
            '{"libtorrent1":"saltydk/qbittorrent:latest"}',
        ):
            with self.subTest(payload=payload):
                result = self.execute("targets", "--published-images-json", payload)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.github_output.exists())
                self.assertFalse(self.output.exists())

    def test_partial_scan_defers_without_inspecting_registry_or_polluting_outputs(self):
        targets = self.targets(("libtorrent2",))
        result = self.scope()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.github_output.read_text(), "eligible=false\n")
        self.assertIn("::notice::", result.stderr)
        self.assertFalse(self.commands.exists())
        report = json.loads(self.output.read_text())
        self.assertEqual(report["status"], "deferred")
        self.assertEqual(report["assessed_targets"], targets)
        self.assertEqual(report["actions"], [])
        self.assertEqual(report["applied"], [])
        self.assertFalse(report["complete"])

    def test_full_current_scan_is_eligible_and_inspects_each_tag_once(self):
        self.targets()
        self.registry_responses()
        result = self.scope()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.github_output.read_text(), "eligible=true\n")
        self.assertFalse(self.output.exists())
        commands = [json.loads(line) for line in self.commands.read_text().splitlines()]
        self.assertEqual(
            commands,
            [
                [
                    "buildx",
                    "imagetools",
                    "inspect",
                    f"saltydk/qbittorrent:{variant}",
                    "--format",
                    "{{json .}}",
                ]
                for variant in VARIANTS
            ],
        )

    def test_full_stale_scan_defers_successfully(self):
        self.targets()
        self.registry_responses(libtorrent1={"digest": NEWER_DIGEST})
        result = self.scope()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.github_output.read_text(), "eligible=false\n")
        self.assertIn("::notice::", result.stderr)
        report = json.loads(self.output.read_text())
        self.assertEqual(report["status"], "deferred")
        self.assertEqual(
            report["reason"], "a newer publication replaced an assessed image"
        )
        self.assertEqual(report["applied"], [])

    def test_registry_failure_is_not_a_successful_deferral(self):
        self.targets()
        self.registry_responses(
            libtorrent2={"returncode": 1, "stderr": "TLS connection timeout"}
        )
        result = self.scope()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TLS connection timeout", result.stderr)
        self.assertFalse(self.github_output.exists())
        self.assertFalse(self.output.exists())

    def test_stale_tag_does_not_hide_a_later_registry_failure(self):
        self.targets()
        self.registry_responses(
            legacy={"digest": NEWER_DIGEST},
            libtorrent2={"returncode": 1, "stderr": "unauthorized"},
        )
        result = self.scope()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unauthorized", result.stderr)
        self.assertFalse(self.github_output.exists())
        self.assertFalse(self.output.exists())

    def test_malformed_registry_metadata_fails_closed(self):
        self.targets()
        for raw in ("not json", "[]", "{}", '{"manifest":{"digest":"invalid"}}'):
            with self.subTest(raw=raw):
                self.registry_responses(legacy={"raw": raw})
                result = self.scope()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.github_output.exists())
                self.assertFalse(self.output.exists())

    def test_invalid_target_artifacts_do_not_become_partial_scan_deferrals(self):
        targets = self.targets(("libtorrent2",))
        payloads: tuple[object, ...] = (
            [],
            {},
            [*targets, targets[0]],
            [{**targets[0], "tracking_reference": "other/image:latest"}],
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.expected.write_text(json.dumps(payload))
                result = self.scope()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.github_output.exists())
                self.assertFalse(self.output.exists())
                self.assertFalse(self.commands.exists())

    def test_outputs_append_without_notices(self):
        self.github_output.write_text("existing=value\n")
        self.targets(("libtorrent2",))
        result = self.scope()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.github_output.read_text(), "existing=value\neligible=false\n"
        )


if __name__ == "__main__":
    unittest.main()
