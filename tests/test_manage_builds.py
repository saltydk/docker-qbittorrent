import unittest
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from scripts.manage_builds import (
    ArtifactTarget,
    BuildInputError,
    LiveProvider,
    VariantState,
    build_matrices,
    compute_update,
    extract_asset_checksums,
    load_states,
    main,
    parse_variant,
    render_variant,
    select_variants,
    validate_artifact_inputs,
    write_updates,
    prepare_updates,
    image_inputs_digest,
)
from scripts.package_locks import LockError, PackageLock, requests_digest


BASE_REPOSITORY = "saltydk/alpine-s6overlay"
BASE_OLD = f"{BASE_REPOSITORY}:sha-{'1' * 40}@sha256:{'1' * 64}"
BASE_NEW = f"{BASE_REPOSITORY}:sha-{'2' * 40}@sha256:{'2' * 64}"


def base_metadata(
    revisions: dict[str, str] | None = None,
    digest: str = "2" * 64,
) -> str:
    platform_revisions = revisions or {
        "linux/amd64": "2" * 40,
        "linux/arm64": "2" * 40,
        "linux/arm/v7": "2" * 40,
    }
    return json.dumps(
        {
            "manifest": {"digest": "sha256:" + digest},
            "image": {
                platform: {
                    "config": {
                        "Labels": {
                            "org.opencontainers.image.revision": revision,
                        }
                    }
                }
                for platform, revision in platform_revisions.items()
            },
        }
    )


def state(
    name: str = "libtorrent1",
    release: str = "release-5.2.3_v1.2.20",
    revision: int = 5,
    amd64: str = "a" * 64,
    arm64: str = "b" * 64,
    base_image: str = BASE_OLD,
) -> VariantState:
    repository = "userdocs/qbittorrent-nox-static-legacy" if name == "legacy" else "userdocs/qbittorrent-nox-static"
    return VariantState(
        name=name,
        dockerfile=f"Dockerfile.{name}",
        repository=repository,
        release=release,
        revision=revision,
        sha256_amd64=amd64,
        sha256_arm64=arm64,
        base_image=base_image,
    )


class ComputeUpdateTests(unittest.TestCase):
    def test_same_release_binary_change_keeps_upstream_revision(self) -> None:
        current = state(name="legacy", release="release-4.3.9_v1.2.20", revision=7)
        target = ArtifactTarget(current.release, 7, current.sha256_amd64, "c" * 64)

        decision = compute_update(current, target, BASE_OLD, packages_changed=False)

        self.assertEqual(decision.state.revision, 7)
        self.assertEqual(decision.state.sha256_arm64, "c" * 64)
        self.assertEqual(decision.reasons, ("binary",))

    def test_base_change_keeps_upstream_revision(self) -> None:
        current = state()
        target = ArtifactTarget(current.release, current.revision, current.sha256_amd64, current.sha256_arm64)

        decision = compute_update(current, target, BASE_NEW, packages_changed=False)

        self.assertEqual(decision.state.revision, 5)
        self.assertEqual(decision.state.base_image, BASE_NEW)
        self.assertEqual(decision.reasons, ("base",))

    def test_new_release_uses_upstream_revision(self) -> None:
        current = state()
        target = ArtifactTarget("release-5.2.4_v1.2.20", 0, "c" * 64, "d" * 64)

        decision = compute_update(current, target, BASE_NEW, packages_changed=True)

        self.assertEqual(decision.state.revision, 0)
        self.assertEqual(decision.reasons, ("release", "base", "packages"))

    def test_combined_same_release_changes_use_upstream_revision(self) -> None:
        current = state(revision=8)
        target = ArtifactTarget(current.release, 10, "c" * 64, "d" * 64)

        decision = compute_update(current, target, BASE_NEW, packages_changed=True)

        self.assertEqual(decision.state.revision, 10)
        self.assertEqual(decision.reasons, ("revision", "binary", "base", "packages"))

    def test_package_only_update_counts_as_a_tracked_input_change(self) -> None:
        current = state()
        target = ArtifactTarget(current.release, current.revision, current.sha256_amd64, current.sha256_arm64)

        decision = compute_update(current, target, BASE_OLD, packages_changed=True)

        self.assertEqual(decision.state, current)
        self.assertTrue(decision.changed)
        self.assertTrue(decision.rebuild)
        self.assertEqual(decision.reasons, ("packages",))


class DockerfileTests(unittest.TestCase):
    def test_parse_and_render_round_trip_variant_inputs(self) -> None:
        text = f'''ARG BASE_IMAGE="{BASE_OLD}"
FROM ${{BASE_IMAGE}}
ARG QBITTORRENT_REPOSITORY="userdocs/qbittorrent-nox-static"
ARG QBITTORRENT_RELEASE="release-5.2.3_v1.2.20"
ARG QBITTORRENT_REVISION="5"
ARG QBITTORRENT_SHA256_AMD64="{'a' * 64}"
ARG QBITTORRENT_SHA256_ARM64="{'b' * 64}"
'''

        current = parse_variant("libtorrent1", "Dockerfile.libtorrent1", text)
        updated = state(base_image=BASE_NEW)
        rendered = render_variant(text, updated)

        self.assertEqual(current, state())
        self.assertIn(f'ARG BASE_IMAGE="{BASE_NEW}"', rendered)
        self.assertIn('ARG QBITTORRENT_REVISION="5"', rendered)
        self.assertEqual(parse_variant("libtorrent1", "Dockerfile.libtorrent1", rendered), updated)

    def test_missing_required_argument_fails_closed(self) -> None:
        with self.assertRaisesRegex(BuildInputError, "QBITTORRENT_RELEASE"):
            parse_variant("libtorrent1", "Dockerfile.libtorrent1", 'ARG BASE_IMAGE="alpine"\n')

    def test_parse_rejects_base_without_source_sha_tag(self) -> None:
        text = f'''ARG BASE_IMAGE="saltydk/alpine-s6overlay:latest@sha256:{'1' * 64}"
ARG QBITTORRENT_REPOSITORY="userdocs/qbittorrent-nox-static"
ARG QBITTORRENT_RELEASE="release-5.2.3_v1.2.20"
ARG QBITTORRENT_REVISION="5"
ARG QBITTORRENT_SHA256_AMD64="{'a' * 64}"
ARG QBITTORRENT_SHA256_ARM64="{'b' * 64}"
'''

        with self.assertRaisesRegex(BuildInputError, "source SHA tag and manifest digest"):
            parse_variant("libtorrent1", "Dockerfile.libtorrent1", text)

    def test_load_states_rejects_mismatched_base_images(self) -> None:
        template = '''ARG BASE_IMAGE="{base}"
ARG QBITTORRENT_REPOSITORY="userdocs/qbittorrent-nox-static"
ARG QBITTORRENT_RELEASE="release-5.2.3_v1.2.20"
ARG QBITTORRENT_REVISION="5"
ARG QBITTORRENT_SHA256_AMD64="{amd64}"
ARG QBITTORRENT_SHA256_ARM64="{arm64}"
'''
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for index, name in enumerate(("libtorrent1", "libtorrent2", "legacy"), start=1):
                (root / f"Dockerfile.{name}").write_text(
                    template.format(
                        base=BASE_OLD if index < 3 else BASE_NEW,
                        amd64="a" * 64,
                        arm64="b" * 64,
                    ),
                    encoding="utf-8",
                )

            with self.assertRaisesRegex(BuildInputError, "same BASE_IMAGE"):
                load_states(root)

    def test_write_updates_changes_only_selected_dockerfiles(self) -> None:
        original = f'''ARG BASE_IMAGE="{BASE_OLD}"
ARG QBITTORRENT_REPOSITORY="userdocs/qbittorrent-nox-static"
ARG QBITTORRENT_RELEASE="release-5.2.3_v1.2.20"
ARG QBITTORRENT_REVISION="5"
ARG QBITTORRENT_SHA256_AMD64="{'a' * 64}"
ARG QBITTORRENT_SHA256_ARM64="{'b' * 64}"
'''
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "Dockerfile.libtorrent1"
            path.write_text(original, encoding="utf-8")
            decision = compute_update(
                state(),
                ArtifactTarget("release-5.2.4_v1.2.20", 0, "c" * 64, "d" * 64),
                BASE_NEW,
                packages_changed=False,
            )

            write_updates(root, {"libtorrent1": decision})

            updated = parse_variant("libtorrent1", "Dockerfile.libtorrent1", path.read_text(encoding="utf-8"))
            self.assertEqual(updated, decision.state)


class SourceTests(unittest.TestCase):
    def test_artifact_validation_rejects_non_upstream_revision(self) -> None:
        current = state(revision=6)
        target = ArtifactTarget(
            current.release,
            5,
            current.sha256_amd64,
            current.sha256_arm64,
        )

        with self.assertRaisesRegex(
            BuildInputError,
            "libtorrent1 QBITTORRENT_REVISION is 6; upstream requires 5",
        ):
            validate_artifact_inputs({"libtorrent1": current}, {"libtorrent1": target})

    def test_extracts_both_supported_asset_checksums(self) -> None:
        release = {
            "assets": [
                {"name": "x86_64-qbittorrent-nox", "digest": "sha256:" + "a" * 64},
                {"name": "aarch64-qbittorrent-nox", "digest": "sha256:" + "b" * 64},
                {"name": "armv7-qbittorrent-nox", "digest": "sha256:" + "c" * 64},
            ]
        }

        self.assertEqual(extract_asset_checksums(release), ("a" * 64, "b" * 64))

    def test_missing_supported_asset_fails_closed(self) -> None:
        release = {"assets": [{"name": "x86_64-qbittorrent-nox", "digest": "sha256:" + "a" * 64}]}

        with self.assertRaisesRegex(BuildInputError, "aarch64"):
            extract_asset_checksums(release)

    def test_live_provider_resolves_three_two_architecture_targets(self) -> None:
        def release(amd64: str, arm64: str):
            return {
                "assets": [
                    {"name": "x86_64-qbittorrent-nox", "digest": "sha256:" + amd64 * 64},
                    {"name": "aarch64-qbittorrent-nox", "digest": "sha256:" + arm64 * 64},
                ]
            }

        responses = {
            "https://github.com/userdocs/qbittorrent-nox-static/releases/latest/download/dependency-version.json": {
                "qbittorrent": "5.2.3",
                "libtorrent_1_2": "1.2.20",
                "libtorrent_2_0": "2.0.14",
            },
            "https://github.com/userdocs/qbittorrent-nox-static/releases/download/release-5.2.3_v1.2.20/dependency-version.json": {"revision": "5"},
            "https://github.com/userdocs/qbittorrent-nox-static/releases/download/release-5.2.3_v2.0.14/dependency-version.json": {"revision": "4"},
            "https://api.github.com/repos/userdocs/qbittorrent-nox-static/releases/tags/release-5.2.3_v1.2.20": release("a", "b"),
            "https://api.github.com/repos/userdocs/qbittorrent-nox-static/releases/tags/release-5.2.3_v2.0.14": release("c", "d"),
            "https://github.com/userdocs/qbittorrent-nox-static-legacy/releases/latest/download/dependency-version.json": {
                "qbittorrent": "4.3.9",
                "libtorrent_1_2": "1.2.20",
                "revision": "7",
            },
            "https://api.github.com/repos/userdocs/qbittorrent-nox-static-legacy/releases/tags/release-4.3.9_v1.2.20": release("e", "f"),
        }

        class Http:
            def get_json(self, url, allow_not_found=False):
                return responses[url]

        provider = LiveProvider(Http(), lambda command: subprocess.CompletedProcess(command, 0, "", ""))
        targets = provider.artifact_targets()

        self.assertEqual(targets["libtorrent1"], ArtifactTarget("release-5.2.3_v1.2.20", 5, "a" * 64, "b" * 64))
        self.assertEqual(targets["libtorrent2"], ArtifactTarget("release-5.2.3_v2.0.14", 4, "c" * 64, "d" * 64))
        self.assertEqual(targets["legacy"], ArtifactTarget("release-4.3.9_v1.2.20", 7, "e" * 64, "f" * 64))

    def test_live_provider_resolves_base_digest(self) -> None:
        class Http:
            def get_json(self, url, allow_not_found=False):
                return {"name": "published"}

        def runner(command):
            if command[:3] == ["docker", "buildx", "imagetools"]:
                if command[4] == "saltydk/alpine-s6overlay:latest":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        base_metadata(),
                        "",
                    )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    '{"digest":"sha256:' + "2" * 64 + '"}',
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "openssl-3.5.7-r0 < 3.5.8-r0\n", "")

        provider = LiveProvider(Http(), runner)

        self.assertEqual(provider.base_image(), BASE_NEW)
        self.assertTrue(provider.is_published(state()))

    def test_base_resolution_rejects_missing_platform_metadata(self) -> None:
        def runner(command):
            return subprocess.CompletedProcess(
                command,
                0,
                base_metadata(
                    {
                        "linux/amd64": "2" * 40,
                        "linux/arm64": "2" * 40,
                    }
                ),
                "",
            )

        provider = LiveProvider(object(), runner)

        with self.assertRaisesRegex(BuildInputError, "linux/arm/v7 metadata"):
            provider.base_image()

    def test_base_resolution_rejects_missing_revision_label(self) -> None:
        metadata = json.loads(base_metadata())
        metadata["image"]["linux/arm64"]["config"]["Labels"] = {}

        def runner(command):
            return subprocess.CompletedProcess(command, 0, json.dumps(metadata), "")

        provider = LiveProvider(object(), runner)

        with self.assertRaisesRegex(BuildInputError, "linux/arm64 labels is missing"):
            provider.base_image()

    def test_base_resolution_rejects_platform_revision_disagreement(self) -> None:
        def runner(command):
            return subprocess.CompletedProcess(
                command,
                0,
                base_metadata(
                    {
                        "linux/amd64": "2" * 40,
                        "linux/arm64": "3" * 40,
                        "linux/arm/v7": "2" * 40,
                    }
                ),
                "",
            )

        provider = LiveProvider(object(), runner)

        with self.assertRaisesRegex(BuildInputError, "do not share one OCI revision"):
            provider.base_image()

    def test_base_resolution_rejects_missing_sha_tag(self) -> None:
        def runner(command):
            if command[4] == "saltydk/alpine-s6overlay:latest":
                return subprocess.CompletedProcess(command, 0, base_metadata(), "")
            return subprocess.CompletedProcess(command, 1, "", "manifest unknown")

        provider = LiveProvider(object(), runner)

        with self.assertRaisesRegex(BuildInputError, "failed to inspect base image SHA tag"):
            provider.base_image()

    def test_base_resolution_rejects_sha_tag_digest_mismatch(self) -> None:
        def runner(command):
            if command[4] == "saltydk/alpine-s6overlay:latest":
                return subprocess.CompletedProcess(command, 0, base_metadata(), "")
            return subprocess.CompletedProcess(
                command,
                0,
                '{"digest":"sha256:' + "3" * 64 + '"}',
                "",
            )

        provider = LiveProvider(object(), runner)

        with self.assertRaisesRegex(BuildInputError, "does not match the latest manifest digest"):
            provider.base_image()


class MatrixTests(unittest.TestCase):
    def test_changed_paths_select_only_affected_variants(self) -> None:
        self.assertEqual(select_variants(["Dockerfile.libtorrent2"]), ("libtorrent2",))
        self.assertEqual(select_variants(["root-legacy/etc/cont-init.d/10-config"]), ("legacy",))
        self.assertEqual(select_variants(["root/etc/services.d/qbittorrent/run"]), ("libtorrent1", "libtorrent2", "legacy"))
        self.assertEqual(select_variants([".github/workflows/security-scan.yml"]), ("libtorrent1", "libtorrent2", "legacy"))
        self.assertEqual(select_variants(["packages/runtime/aarch64.lock"]), ("libtorrent1", "libtorrent2", "legacy"))

    def test_build_matrices_preserve_aliases_and_expand_platforms(self) -> None:
        states = {name: state(name=name) for name in ("libtorrent1", "libtorrent2", "legacy")}

        matrices = build_matrices(states, ("libtorrent1", "libtorrent2"))

        self.assertEqual(len(matrices["candidates"]["include"]), 4)
        self.assertEqual(len(matrices["publish"]["include"]), 2)
        self.assertEqual(matrices["candidates"]["include"][0]["qbittorrent_version"], "5.2.3")
        self.assertEqual(matrices["candidates"]["include"][0]["libtorrent_prefix"], "1.2.")
        self.assertEqual(matrices["candidates"]["include"][0]["config_profile"], "modern")
        self.assertEqual(
            matrices["publish"]["include"][0]["tags"],
            [
                "saltydk/qbittorrent:release-5.2.3_v1.2.20-5",
                "saltydk/qbittorrent:release-5.2.3_v1.2.20",
                "saltydk/qbittorrent:libtorrent1",
                "saltydk/qbittorrent:latest",
            ],
        )
        self.assertNotIn("saltydk/qbittorrent:latest", matrices["publish"]["include"][1]["tags"])

    def test_matrix_cli_emits_candidate_and_publish_matrices(self) -> None:
        template = '''ARG BASE_IMAGE="{base}"
ARG QBITTORRENT_REPOSITORY="{repository}"
ARG QBITTORRENT_RELEASE="release-5.2.3_v1.2.20"
ARG QBITTORRENT_REVISION="5"
ARG QBITTORRENT_SHA256_AMD64="{amd64}"
ARG QBITTORRENT_SHA256_ARM64="{arm64}"
'''
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("libtorrent1", "libtorrent2", "legacy"):
                repository = "userdocs/qbittorrent-nox-static-legacy" if name == "legacy" else "userdocs/qbittorrent-nox-static"
                (root / f"Dockerfile.{name}").write_text(
                    template.format(base=BASE_OLD, repository=repository, amd64="a" * 64, arm64="b" * 64),
                    encoding="utf-8",
                )
            output = StringIO()

            with redirect_stdout(output):
                result = main(["--root", str(root), "matrix", "--all"])

            report = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(report["selected"], ["libtorrent1", "libtorrent2", "legacy"])
            self.assertEqual(len(report["candidates"]["include"]), 6)
            self.assertEqual(len(report["publish"]["include"]), 3)


class LockedUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        source = Path(__file__).resolve().parents[1]
        for name in ("libtorrent1", "libtorrent2", "legacy"):
            text = (source / f"Dockerfile.{name}").read_text()
            current = parse_variant(name, f"Dockerfile.{name}", text)
            (self.root / current.dockerfile).write_text(render_variant(text, current))
        profile = self.root / "packages/runtime"
        profile.mkdir(parents=True)
        (profile / "requested.txt").write_text("xz\n")
        self.states = load_states(self.root)
        self.parent = self.states["libtorrent1"].base_image
        self.published = True
        self.version = "5.8.3-r0"
        outer = self
        class Provider:
            def artifact_targets(self):
                return {name: ArtifactTarget(s.release, s.revision, s.sha256_amd64, s.sha256_arm64)
                        for name, s in outer.states.items()}
            def base_image(self):
                return outer.parent
            def is_current(self, variant, input_sha):
                return outer.published
        self.provider = Provider()

    def resolve(self, root, profile, platform, parent, **kwargs):
        architecture = "x86_64" if platform == "linux/amd64" else "aarch64"
        return PackageLock(architecture, parent, requests_digest(("xz",)),
                           {"xz": self.version, "xz-libs": self.version})

    def test_package_updates_are_committed_inputs_without_revision_bumps(self):
        initial = prepare_updates(self.root, self.provider, resolver=self.resolve)
        initial.write()
        self.version = "5.8.4-r0"
        plan = prepare_updates(self.root, self.provider, resolver=self.resolve)
        self.assertEqual(plan.report["changed"], ["libtorrent1", "libtorrent2", "legacy"])
        self.assertEqual(plan.report["images"][0]["changes"], [
            {"name": "xz", "old": "5.8.3-r0", "new": "5.8.4-r0"},
            {"name": "xz-libs", "old": "5.8.3-r0", "new": "5.8.4-r0"},
        ])
        plan.write()
        self.assertIn("xz=5.8.4-r0", (self.root / "packages/runtime/x86_64.lock").read_text())
        self.assertEqual(load_states(self.root)["libtorrent1"].revision, self.states["libtorrent1"].revision)
        self.assertEqual(prepare_updates(self.root, self.provider, resolver=self.resolve).report["status"], "no-changes")

    def test_failed_architecture_resolution_does_not_write_any_inputs(self):
        prepare_updates(self.root, self.provider, resolver=self.resolve).write()
        original = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.version = "5.8.4-r0"
        def fail_arm(root, profile, platform, parent, **kwargs):
            if platform == "linux/arm64":
                raise LockError("repository unavailable")
            return self.resolve(root, profile, platform, parent, **kwargs)
        with self.assertRaisesRegex(LockError, "unavailable"):
            prepare_updates(self.root, self.provider, resolver=fail_arm).write()
        self.assertEqual({p: p.read_bytes() for p in original}, original)

    def test_pending_publication_retries_without_an_empty_input_commit(self):
        prepare_updates(self.root, self.provider, resolver=self.resolve).write()
        self.published = False
        plan = prepare_updates(self.root, self.provider, resolver=self.resolve)
        self.assertEqual(plan.report["status"], "pending-publication")
        self.assertEqual(plan.report["changed"], [])
        self.assertEqual(plan.report["rebuild"], ["libtorrent1", "libtorrent2", "legacy"])
        self.assertEqual(plan.files, {})

    def test_publication_identity_ignores_docs_and_other_variant_changes(self):
        original = image_inputs_digest(self.root, "libtorrent1")
        (self.root / "README.md").write_text("documentation changed")
        with (self.root / "Dockerfile.legacy").open("a") as output:
            output.write("\n# legacy-only build change\n")
        self.assertEqual(image_inputs_digest(self.root, "libtorrent1"), original)
        shared = self.root / "root/etc/services.d/qbittorrent/run"
        shared.parent.mkdir(parents=True)
        shared.write_text("exec qbittorrent-nox\n")
        self.assertNotEqual(image_inputs_digest(self.root, "libtorrent1"), original)

    def test_unknown_or_duplicate_matrix_variants_fail(self):
        from contextlib import redirect_stderr
        for value in ('["unknown"]', '["legacy", "legacy"]', '{}', 'broken'):
            with self.subTest(value=value), redirect_stderr(StringIO()):
                self.assertEqual(main(["--root", str(self.root), "matrix", "--variants", value]), 1)

    def test_explicit_matrix_selection_builds_only_requested_variants(self):
        output = StringIO()
        with redirect_stdout(output):
            result = main(["--root", str(self.root), "matrix", "--variants", '["legacy"]'])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["selected"], ["legacy"])
        self.assertEqual(len(report["candidates"]["include"]), 2)

    def test_referenced_artifact_verification_never_looks_up_latest(self):
        variant = self.states["legacy"]
        class Http:
            def get_json(self, url, allow_not_found=False):
                if "/latest" in url:
                    raise AssertionError("verification must use the committed release")
                if url.endswith("dependency-version.json"):
                    return {"revision": str(variant.revision)}
                return {"assets": [
                    {"name": "x86_64-qbittorrent-nox", "digest": "sha256:" + variant.sha256_amd64},
                    {"name": "aarch64-qbittorrent-nox", "digest": "sha256:" + variant.sha256_arm64},
                ]}
        provider = LiveProvider(Http())
        targets = provider.referenced_targets({"legacy": variant})
        validate_artifact_inputs({"legacy": variant}, targets)


if __name__ == "__main__":
    unittest.main()
