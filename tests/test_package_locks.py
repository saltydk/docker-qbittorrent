import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.package_locks import (
    LockError, PackageLock, inventory, package_changes, parse_lock,
    read_requests, requests_digest, resolve_lock, validate_lock, write_locks, BaseUpdateRequired,
    input_files_digest, platform_image_reference,
)


PARENT = "alpine:3.24@sha256:" + "a" * 64
AMD64_PARENT = "alpine:3.24@sha256:" + "1" * 64
ARM64_PARENT = "alpine:3.24@sha256:" + "2" * 64


def manifest_result(command, manifests=None):
    if manifests is None:
        manifests = [
            {"digest": "sha256:" + "1" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": "sha256:" + "2" * 64, "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
        ]
    return subprocess.CompletedProcess(command, 0, json.dumps({"schemaVersion": 2, "manifests": manifests}), "")


class PlatformReferenceTests(unittest.TestCase):
    def test_distinct_platforms_use_distinct_immutable_child_digests(self):
        self.assertEqual(platform_image_reference(PARENT, "linux/amd64", runner=manifest_result), AMD64_PARENT)
        self.assertEqual(platform_image_reference(PARENT, "linux/arm64", runner=manifest_result), ARM64_PARENT)

    def test_missing_duplicate_and_invalid_platform_descriptors_fail_closed(self):
        valid = {"digest": "sha256:" + "1" * 64, "platform": {"os": "linux", "architecture": "amd64"}}
        for descriptors in ([], [valid, valid], [{**valid, "digest": "sha256:invalid"}]):
            with self.subTest(descriptors=descriptors), self.assertRaises(LockError):
                platform_image_reference(PARENT, "linux/amd64", runner=lambda command: manifest_result(command, descriptors))

    def test_arm_variant_is_selected_without_matching_other_arm_variants(self):
        descriptors = [
            {"digest": "sha256:" + "6" * 64, "platform": {"os": "linux", "architecture": "arm", "variant": "v6"}},
            {"digest": "sha256:" + "7" * 64, "platform": {"os": "linux", "architecture": "arm", "variant": "v7"}},
            {"digest": "sha256:" + "8" * 64, "platform": {"os": "linux", "architecture": "arm", "variant": ["v7"]}},
        ]
        self.assertEqual(platform_image_reference(PARENT, "linux/arm/v7", runner=lambda command: manifest_result(command, descriptors)),
                         "alpine:3.24@sha256:" + "7" * 64)

    def test_single_platform_manifest_keeps_its_pinned_reference(self):
        def runner(command):
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": "sha256:" + "3" * 64}, "layers": [],
            }), "")
        self.assertEqual(platform_image_reference(AMD64_PARENT, "linux/amd64", runner=runner), AMD64_PARENT)


class PackageLockTests(unittest.TestCase):
    def test_input_hash_captures_executable_mode_and_symlink_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "run"
            script.write_text("exec app\n")
            script.chmod(0o644)
            original = input_files_digest(root, [script])
            script.chmod(0o755)
            self.assertNotEqual(input_files_digest(root, [script]), original)
            (root / "one").write_text("same")
            (root / "two").write_text("same")
            link = root / "link"
            link.symlink_to("one")
            original = input_files_digest(root, [link])
            link.unlink()
            link.symlink_to("two")
            self.assertNotEqual(input_files_digest(root, [link]), original)

    def test_planned_new_input_hash_matches_the_written_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "new.lock"
            planned = input_files_digest(root, [path], {path: "contents\n"})
            path.write_text("contents\n")
            self.assertEqual(input_files_digest(root, [path]), planned)

    def test_lock_roundtrip_is_stable_and_contains_complete_exact_constraints(self):
        lock = PackageLock("x86_64", PARENT, "b" * 64,
                           {"xz-libs": "5.8.4-r0", "xz": "5.8.4-r0"})
        expected = ("# apk-lock: 1\n# architecture: x86_64\n# parent: " + PARENT
                    + "\n# requests-sha256: " + "b" * 64
                    + "\nxz=5.8.4-r0\nxz-libs=5.8.4-r0\n")
        self.assertEqual(lock.render(), expected)
        self.assertEqual(parse_lock(expected), lock)
        self.assertEqual(lock.digest, hashlib.sha256(expected.encode()).hexdigest())

    def test_duplicate_or_unconstrained_packages_are_rejected(self):
        for text in ("xz=1\nxz=2\n", "xz>=1\n", "xz\n", "xz=1;touch\n", ""):
            with self.subTest(text=text), self.assertRaises(LockError):
                inventory(text)

    def test_unknown_lock_format_is_rejected(self):
        text = PackageLock("x86_64", PARENT, "b" * 64, {"xz": "1"}).render()
        with self.assertRaisesRegex(LockError, "format"):
            parse_lock(text.replace("apk-lock: 1", "apk-lock: 2"))

    def test_requests_are_normalized_and_invalid_constraints_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requested.txt"
            path.write_text("# requested packages\nxz\nbash\nxz\n\n")
            self.assertEqual(read_requests(path), ("bash", "xz"))
            self.assertEqual(requests_digest(read_requests(path)),
                             hashlib.sha256(b"bash\nxz\n").hexdigest())
            path.write_text("xz>=1\n")
            with self.assertRaises(LockError):
                read_requests(path)

    def test_changes_include_additions_removals_and_versions(self):
        self.assertEqual(package_changes({"xz": "1", "old": "2", "same": "3"},
                                         {"xz": "2", "new": "4", "same": "3"}),
                         [{"name": "new", "old": None, "new": "4"},
                          {"name": "old", "old": "2", "new": None},
                          {"name": "xz", "old": "1", "new": "2"}])

    def test_resolver_records_target_parent_and_architecture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests = root / "packages/runtime/requested.txt"
            requests.parent.mkdir(parents=True)
            requests.write_text("xz\n")
            def runner(command):
                if command[:3] == ["docker", "buildx", "imagetools"]:
                    return manifest_result(command)
                self.assertIn("linux/arm64", command)
                self.assertIn(ARM64_PARENT, command)
                self.assertEqual(command[-1], "inherited")
                return subprocess.CompletedProcess(command, 0, "xz=5.8.4-r0\n", "")
            lock = resolve_lock(root, "runtime", "linux/arm64", PARENT,
                                inherited=True, runner=runner)
            self.assertEqual(lock.architecture, "aarch64")
            self.assertEqual(lock.parent, PARENT)
            self.assertEqual(lock.packages, {"xz": "5.8.4-r0"})
            self.assertEqual(lock.requests_sha256, hashlib.sha256(b"xz\n").hexdigest())

    def test_resolution_failure_never_changes_existing_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "packages/runtime"
            profile.mkdir(parents=True)
            (profile / "requested.txt").write_text("xz\n")
            path = profile / "x86_64.lock"
            path.write_text("preserve me\n")
            def runner(command):
                return subprocess.CompletedProcess(command, 1, "", "repository unavailable")
            with self.assertRaisesRegex(LockError, "linux/amd64.*repository unavailable"):
                resolve_lock(root, "runtime", "linux/amd64", PARENT, runner=runner)
            self.assertEqual(path.read_text(), "preserve me\n")

    def test_resolver_rejects_wrong_actual_architecture_before_installing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "packages/runtime"
            profile.mkdir(parents=True)
            (profile / "requested.txt").write_text("xz\n")
            binary = root / "bin"
            binary.mkdir()
            apk = binary / "apk"
            apk.write_text("#!/bin/sh\nprintf 'x86_64\\n'\n")
            apk.chmod(0o755)
            def runner(command):
                if command[:3] == ["docker", "buildx", "imagetools"]:
                    return manifest_result(command)
                entry = command.index("--entrypoint")
                return subprocess.run(["/bin/sh", *command[entry + 3:]], capture_output=True, text=True,
                                      env={**os.environ, "PATH": str(binary) + ":" + os.environ["PATH"]})
            with self.assertRaisesRegex(LockError, "expected aarch64, got x86_64"):
                resolve_lock(root, "runtime", "linux/arm64", PARENT, inherited=True, runner=runner)

    def test_only_base_conflicts_and_missing_helpers_request_a_base_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "packages/runtime"
            profile.mkdir(parents=True)
            (profile / "requested.txt").write_text("xz\n")
            for diagnostic, expected in (
                ("apk-lock: inherited package constraints conflict: breaks world[xz=1]", BaseUpdateRequired),
                ("/bin/sh: can't open '/usr/local/libexec/apk-lock': No such file or directory", BaseUpdateRequired),
                ("apk-lock: failed to resolve inherited packages: temporary error (try again later)", LockError),
            ):
                def runner(command):
                    if command[:3] == ["docker", "buildx", "imagetools"]:
                        return manifest_result(command)
                    return subprocess.CompletedProcess(command, 1, "", diagnostic)
                with self.subTest(diagnostic=diagnostic), self.assertRaises(LockError) as caught:
                    resolve_lock(root, "runtime", "linux/amd64", PARENT, inherited=True, runner=runner)
                self.assertIs(type(caught.exception), expected)

    def test_verify_rejects_changed_requests_parent_and_architecture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "packages/runtime"
            profile.mkdir(parents=True)
            (profile / "requested.txt").write_text("xz\n")
            path = profile / "x86_64.lock"
            lock = PackageLock("x86_64", PARENT, requests_digest(("xz",)), {"xz": "1"})
            write_locks({path: lock})
            self.assertEqual(validate_lock(root, "runtime", "x86_64", PARENT), lock)
            for bad in (PackageLock("aarch64", PARENT, lock.requests_sha256, lock.packages),
                        PackageLock("x86_64", PARENT.replace("a" * 64, "c" * 64), lock.requests_sha256, lock.packages),
                        PackageLock("x86_64", PARENT, "d" * 64, lock.packages)):
                write_locks({path: bad})
                with self.assertRaises(LockError):
                    validate_lock(root, "runtime", "x86_64", PARENT)


if __name__ == "__main__":
    unittest.main()
