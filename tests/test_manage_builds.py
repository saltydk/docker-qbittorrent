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
    plan_updates,
    render_variant,
    revision_variants,
    select_variants,
    validate_revision_changes,
    write_updates,
)


BASE_OLD = "saltydk/alpine-s6overlay:latest@sha256:" + "1" * 64
BASE_NEW = "saltydk/alpine-s6overlay:latest@sha256:" + "2" * 64


def state(
    name: str = "libtorrent1",
    release: str = "release-5.2.3_v1.2.20",
    upstream_revision: int = 5,
    image_revision: int = 6,
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
        upstream_revision=upstream_revision,
        image_revision=image_revision,
        sha256_amd64=amd64,
        sha256_arm64=arm64,
        base_image=base_image,
    )


class ComputeUpdateTests(unittest.TestCase):
    def test_same_release_binary_change_never_downgrades_local_revision(self) -> None:
        current = state(name="legacy", release="release-4.3.9_v1.2.20", upstream_revision=7, image_revision=9)
        target = ArtifactTarget(current.release, 7, current.sha256_amd64, "c" * 64)

        decision = compute_update(current, target, BASE_OLD, packages_outdated=False, published=True)

        self.assertEqual(decision.state.image_revision, 10)
        self.assertEqual(decision.state.sha256_arm64, "c" * 64)
        self.assertEqual(decision.reasons, ("binary",))

    def test_base_change_increments_image_revision_once(self) -> None:
        current = state()
        target = ArtifactTarget(current.release, current.upstream_revision, current.sha256_amd64, current.sha256_arm64)

        decision = compute_update(current, target, BASE_NEW, packages_outdated=False, published=True)

        self.assertEqual(decision.state.image_revision, 7)
        self.assertEqual(decision.state.base_image, BASE_NEW)
        self.assertEqual(decision.reasons, ("base",))

    def test_new_release_uses_upstream_revision(self) -> None:
        current = state()
        target = ArtifactTarget("release-5.2.4_v1.2.20", 0, "c" * 64, "d" * 64)

        decision = compute_update(current, target, BASE_NEW, packages_outdated=True, published=True)

        self.assertEqual(decision.state.image_revision, 0)
        self.assertEqual(decision.state.upstream_revision, 0)
        self.assertEqual(decision.reasons, ("release", "base", "packages"))

    def test_combined_same_release_changes_increment_only_once(self) -> None:
        current = state(image_revision=8)
        target = ArtifactTarget(current.release, 10, "c" * 64, "d" * 64)

        decision = compute_update(current, target, BASE_NEW, packages_outdated=True, published=True)

        self.assertEqual(decision.state.image_revision, 10)
        self.assertEqual(decision.reasons, ("binary", "base", "packages"))

    def test_pending_exact_tag_blocks_another_package_revision(self) -> None:
        current = state()
        target = ArtifactTarget(current.release, current.upstream_revision, current.sha256_amd64, current.sha256_arm64)

        decision = compute_update(current, target, BASE_OLD, packages_outdated=True, published=False)

        self.assertEqual(decision.state, current)
        self.assertEqual(decision.reasons, ("pending-publication",))

    def test_base_change_reuses_an_unpublished_image_revision(self) -> None:
        current = state(image_revision=6)
        target = ArtifactTarget(current.release, current.upstream_revision, current.sha256_amd64, current.sha256_arm64)

        decision = compute_update(current, target, BASE_NEW, packages_outdated=False, published=False)

        self.assertEqual(decision.state.image_revision, 6)
        self.assertEqual(decision.state.base_image, BASE_NEW)
        self.assertEqual(decision.reasons, ("base",))


class DockerfileTests(unittest.TestCase):
    def test_parse_and_render_round_trip_variant_inputs(self) -> None:
        text = f'''ARG BASE_IMAGE="{BASE_OLD}"
FROM ${{BASE_IMAGE}}
ARG QBITTORRENT_REPOSITORY="userdocs/qbittorrent-nox-static"
ARG QBITTORRENT_RELEASE="release-5.2.3_v1.2.20"
ARG QBITTORRENT_UPSTREAM_REVISION="5"
ARG QBITTORRENT_REVISION="6"
ARG QBITTORRENT_SHA256_AMD64="{'a' * 64}"
ARG QBITTORRENT_SHA256_ARM64="{'b' * 64}"
'''

        current = parse_variant("libtorrent1", "Dockerfile.libtorrent1", text)
        updated = state(base_image=BASE_NEW, image_revision=7)
        rendered = render_variant(text, updated)

        self.assertEqual(current, state())
        self.assertIn(f'ARG BASE_IMAGE="{BASE_NEW}"', rendered)
        self.assertIn('ARG QBITTORRENT_REVISION="7"', rendered)
        self.assertEqual(parse_variant("libtorrent1", "Dockerfile.libtorrent1", rendered), updated)

    def test_missing_required_argument_fails_closed(self) -> None:
        with self.assertRaisesRegex(BuildInputError, "QBITTORRENT_RELEASE"):
            parse_variant("libtorrent1", "Dockerfile.libtorrent1", 'ARG BASE_IMAGE="alpine"\n')

    def test_load_states_rejects_mismatched_base_images(self) -> None:
        template = '''ARG BASE_IMAGE="{base}"
ARG QBITTORRENT_REPOSITORY="userdocs/qbittorrent-nox-static"
ARG QBITTORRENT_RELEASE="release-5.2.3_v1.2.20"
ARG QBITTORRENT_UPSTREAM_REVISION="5"
ARG QBITTORRENT_REVISION="6"
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
ARG QBITTORRENT_UPSTREAM_REVISION="5"
ARG QBITTORRENT_REVISION="6"
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
                packages_outdated=False,
                published=True,
            )

            write_updates(root, {"libtorrent1": decision})

            updated = parse_variant("libtorrent1", "Dockerfile.libtorrent1", path.read_text(encoding="utf-8"))
            self.assertEqual(updated, decision.state)


class SourceTests(unittest.TestCase):
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

    def test_plan_updates_marks_unpublished_unchanged_input_as_pending(self) -> None:
        current = state()

        class Provider:
            def artifact_targets(self):
                return {
                    "libtorrent1": ArtifactTarget(
                        current.release,
                        current.upstream_revision,
                        current.sha256_amd64,
                        current.sha256_arm64,
                    )
                }

            def base_image(self):
                return BASE_OLD

            def is_published(self, variant):
                return False

            def packages_outdated(self, variant):
                raise AssertionError("an unpublished image must not be probed")

        decisions = plan_updates({"libtorrent1": current}, Provider())

        self.assertEqual(decisions["libtorrent1"].reasons, ("pending-publication",))

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

    def test_live_provider_resolves_base_digest_and_package_upgrades(self) -> None:
        class Http:
            def get_json(self, url, allow_not_found=False):
                return {"name": "published"}

        def runner(command):
            if command[:3] == ["docker", "buildx", "imagetools"]:
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
        self.assertTrue(provider.packages_outdated(state()))


class MatrixTests(unittest.TestCase):
    def test_changed_paths_select_only_affected_variants(self) -> None:
        self.assertEqual(select_variants(["Dockerfile.libtorrent2"]), ("libtorrent2",))
        self.assertEqual(select_variants(["root-legacy/etc/cont-init.d/10-config"]), ("legacy",))
        self.assertEqual(select_variants(["root/etc/services.d/qbittorrent/run"]), ("libtorrent1", "libtorrent2", "legacy"))
        self.assertEqual(select_variants([".github/workflows/security-scan.yml"]), ("libtorrent1", "libtorrent2", "legacy"))
        self.assertEqual(revision_variants([".github/workflows/security-scan.yml"]), ())
        self.assertEqual(revision_variants(["root/etc/services.d/qbittorrent/run"]), ("libtorrent1", "libtorrent2", "legacy"))

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
                "saltydk/qbittorrent:release-5.2.3_v1.2.20-6",
                "saltydk/qbittorrent:release-5.2.3_v1.2.20",
                "saltydk/qbittorrent:libtorrent1",
                "saltydk/qbittorrent:latest",
            ],
        )
        self.assertNotIn("saltydk/qbittorrent:latest", matrices["publish"]["include"][1]["tags"])

    def test_same_release_image_change_requires_revision_increment(self) -> None:
        before = {"libtorrent1": state(image_revision=6)}
        after = {"libtorrent1": state(image_revision=6, base_image=BASE_NEW)}

        with self.assertRaisesRegex(BuildInputError, "libtorrent1.*revision"):
            validate_revision_changes(before, after, ("libtorrent1",))

    def test_new_release_may_reset_revision(self) -> None:
        before = {"libtorrent1": state(image_revision=8)}
        after = {"libtorrent1": state(release="release-5.2.4_v1.2.20", upstream_revision=0, image_revision=0)}

        validate_revision_changes(before, after, ("libtorrent1",))

    def test_matrix_cli_emits_candidate_and_publish_matrices(self) -> None:
        template = '''ARG BASE_IMAGE="{base}"
ARG QBITTORRENT_REPOSITORY="{repository}"
ARG QBITTORRENT_RELEASE="release-5.2.3_v1.2.20"
ARG QBITTORRENT_UPSTREAM_REVISION="5"
ARG QBITTORRENT_REVISION="6"
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


if __name__ == "__main__":
    unittest.main()
