import re
import shlex
import unittest
from pathlib import Path
from typing import cast

from scripts.manage_builds import (
    ArtifactTarget,
    BuildInputError,
    DOCKERFILES,
    HttpClient,
    LiveProvider,
    VariantState,
    build_matrices,
)

MAIN_REPOSITORY = "userdocs/qbittorrent-nox-static"
LEGACY_REPOSITORY = "userdocs/qbittorrent-nox-static-legacy"
MAIN_METADATA_URL = (
    f"https://github.com/{MAIN_REPOSITORY}/releases/latest/download/"
    "dependency-version.json"
)
LEGACY_METADATA_URL = (
    f"https://github.com/{LEGACY_REPOSITORY}/releases/latest/download/"
    "dependency-version.json"
)
RELEASES_URL = f"https://api.github.com/repos/{MAIN_REPOSITORY}/releases?per_page=100"


class StubHttp(HttpClient):
    def __init__(self, responses: dict[str, object]) -> None:
        super().__init__()
        self.responses = responses
        self.requested: list[str] = []

    def get_json(self, url: str, allow_not_found: bool = False) -> object | None:
        self.requested.append(url)
        return self.responses[url]


def add_release(
    responses: dict[str, object],
    repository: str,
    release: str,
    revision: str,
    amd64: str,
    arm64: str,
) -> None:
    responses[
        f"https://github.com/{repository}/releases/download/{release}/"
        "dependency-version.json"
    ] = {"revision": revision}
    responses[f"https://api.github.com/repos/{repository}/releases/tags/{release}"] = {
        "assets": [
            {"name": "x86_64-qbittorrent-nox", "digest": "sha256:" + amd64 * 64},
            {"name": "aarch64-qbittorrent-nox", "digest": "sha256:" + arm64 * 64},
        ]
    }


def version_state(variant: str, version: str) -> VariantState:
    return VariantState(
        name=variant,
        dockerfile=f"Dockerfile.{variant}",
        repository=LEGACY_REPOSITORY if variant == "legacy" else MAIN_REPOSITORY,
        release=f"release-5.3.0_v{version}",
        revision=0,
        sha256_amd64="a" * 64,
        sha256_arm64="b" * 64,
        base_image=f"saltydk/alpine-s6overlay:sha-{'1' * 40}@sha256:{'1' * 64}",
    )


class LibtorrentDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.releases = [
            {
                "tag_name": "release-5.2.3_v1.2.20",
                "draft": False,
                "prerelease": True,
            },
            {
                "tag_name": "release-5.3.1_v2.0.14",
                "draft": False,
                "prerelease": True,
            },
            {
                "tag_name": "release-5.3.0_v2.1.2",
                "draft": False,
                "prerelease": False,
            },
        ]
        self.responses: dict[str, object] = {
            LEGACY_METADATA_URL: {
                "qbittorrent": "4.3.9",
                "libtorrent_1_2": "1.2.20",
                "revision": "7",
            },
            f"{RELEASES_URL}&page=1": self.releases,
        }
        add_release(
            self.responses, MAIN_REPOSITORY, "release-5.2.3_v1.2.20", "5", "a", "b"
        )
        add_release(
            self.responses, MAIN_REPOSITORY, "release-5.3.1_v2.0.14", "4", "c", "d"
        )
        add_release(
            self.responses, MAIN_REPOSITORY, "release-5.3.0_v2.1.2", "0", "e", "f"
        )
        add_release(
            self.responses, LEGACY_REPOSITORY, "release-4.3.9_v1.2.20", "7", "1", "2"
        )
        self.http = StubHttp(self.responses)

    def test_release_discovery_resolves_independent_versions_for_all_variants(
        self,
    ) -> None:
        targets = LiveProvider(self.http).artifact_targets()

        self.assertEqual(
            targets,
            {
                "libtorrent1": ArtifactTarget(
                    "release-5.2.3_v1.2.20", 5, "a" * 64, "b" * 64
                ),
                "libtorrent2": ArtifactTarget(
                    "release-5.3.1_v2.0.14", 4, "c" * 64, "d" * 64
                ),
                "libtorrent21": ArtifactTarget(
                    "release-5.3.0_v2.1.2", 0, "e" * 64, "f" * 64
                ),
                "legacy": ArtifactTarget(
                    "release-4.3.9_v1.2.20", 7, "1" * 64, "2" * 64
                ),
            },
        )
        self.assertIn(f"{RELEASES_URL}&page=1", self.http.requested)
        self.assertNotIn(f"{RELEASES_URL}&page=2", self.http.requested)
        self.assertNotIn(MAIN_METADATA_URL, self.http.requested)

    def test_discovery_scans_all_pages_and_filters_drafts_and_other_series(
        self,
    ) -> None:
        self.responses[f"{RELEASES_URL}&page=1"] = [
            {"tag_name": "release-99.0.0_v1.2.99", "draft": True},
            {"tag_name": "release-99.0.0_v2.0.99", "draft": True},
            {"tag_name": "release-99.0.0_v2.1.99", "draft": True},
            {"tag_name": "release-99.0.0_v1.20.99", "draft": False},
            {"tag_name": "release-99.0.0_v2.10.99", "draft": False},
            {"tag_name": "release-99.0.0_v1.2.99-extra", "draft": False},
            {"tag_name": "release-5.1.0_v1.2.20", "draft": False},
            {"tag_name": "release-5.1.0_v2.0.14", "draft": False},
            {"tag_name": "release-5.1.0_v2.1.2", "draft": False},
            *[
                {"tag_name": f"release-5.3.{patch}_v3.0.0", "draft": False}
                for patch in range(91)
            ],
        ]
        self.responses[f"{RELEASES_URL}&page=2"] = self.releases

        targets = LiveProvider(self.http).artifact_targets()

        self.assertEqual(targets["libtorrent1"].release, "release-5.2.3_v1.2.20")
        self.assertEqual(targets["libtorrent2"].release, "release-5.3.1_v2.0.14")
        self.assertEqual(targets["libtorrent21"].release, "release-5.3.0_v2.1.2")
        self.assertEqual(self.http.requested.count(f"{RELEASES_URL}&page=1"), 1)
        self.assertEqual(self.http.requested.count(f"{RELEASES_URL}&page=2"), 1)
        self.assertNotIn(f"{RELEASES_URL}&page=3", self.http.requested)

    def test_discovery_selects_numeric_versions_from_unordered_releases(self) -> None:
        releases: list[dict[str, object]] = []
        for series in ("1.2", "2.0", "2.1"):
            releases.extend(
                {"tag_name": f"release-{version}_v{series}.{patch}", "draft": False}
                for version, patch in (
                    ("5.2.4", 99),
                    ("5.9.5", 22),
                    ("5.10.0", 20),
                    ("5.10.0", 21),
                )
            )
            add_release(
                self.responses,
                MAIN_REPOSITORY,
                f"release-5.10.0_v{series}.21",
                "3",
                "1",
                "2",
            )
        self.responses[f"{RELEASES_URL}&page=1"] = releases

        targets = LiveProvider(self.http).artifact_targets()

        for variant, series in (
            ("libtorrent1", "1.2"),
            ("libtorrent2", "2.0"),
            ("libtorrent21", "2.1"),
        ):
            with self.subTest(variant=variant):
                self.assertEqual(
                    targets[variant],
                    ArtifactTarget(
                        f"release-5.10.0_v{series}.21", 3, "1" * 64, "2" * 64
                    ),
                )

    def test_discovery_fails_when_any_supported_series_is_missing(self) -> None:
        for series in ("1.2", "2.0", "2.1"):
            with self.subTest(series=series):
                self.responses[f"{RELEASES_URL}&page=1"] = [
                    release
                    for release in self.releases
                    if f"_v{series}." not in str(release["tag_name"])
                ]
                with self.assertRaisesRegex(
                    BuildInputError, series.replace(".", r"\.")
                ):
                    LiveProvider(self.http).artifact_targets()

    def test_discovery_rejects_non_array_release_response(self) -> None:
        self.responses[f"{RELEASES_URL}&page=1"] = {"message": "invalid response"}

        with self.assertRaisesRegex(BuildInputError, "JSON array"):
            LiveProvider(self.http).artifact_targets()

    def test_main_latest_metadata_does_not_determine_any_variant_version(self) -> None:
        for metadata in (
            {"qbittorrent": "99.0.0", "libtorrent_2_1": "2.1.99"},
            {
                "qbittorrent": "99.0.0",
                "libtorrent_1_2": "1.2.99",
                "libtorrent_2_0": "2.0.99",
                "libtorrent_2_1": "2.1.99",
            },
        ):
            with self.subTest(metadata=metadata):
                self.responses[MAIN_METADATA_URL] = metadata
                targets = LiveProvider(self.http).artifact_targets()

                self.assertEqual(
                    targets["libtorrent1"].release, "release-5.2.3_v1.2.20"
                )
                self.assertEqual(
                    targets["libtorrent2"].release, "release-5.3.1_v2.0.14"
                )
                self.assertEqual(
                    targets["libtorrent21"].release, "release-5.3.0_v2.1.2"
                )
                self.assertNotIn(MAIN_METADATA_URL, self.http.requested)


class LibtorrentMatrixTests(unittest.TestCase):
    def test_runtime_prefix_uses_committed_libtorrent_series(self) -> None:
        for variant, version, prefix in (
            ("libtorrent21", "2.1.2", "2.1."),
            ("libtorrent2", "2.0.14", "2.0."),
            ("libtorrent1", "1.2.20", "1.2."),
            ("legacy", "1.2.20", "1.2."),
        ):
            with self.subTest(variant=variant, version=version):
                state = version_state(variant, version)

                matrices = build_matrices({variant: state}, (variant,))

                candidates = cast(
                    dict[str, list[dict[str, object]]], matrices["candidates"]
                )["include"]
                self.assertEqual(len(candidates), 2)
                self.assertEqual(
                    [candidate["libtorrent_prefix"] for candidate in candidates],
                    [prefix, prefix],
                )
                self.assertEqual(
                    [candidate["qbittorrent_version"] for candidate in candidates],
                    ["5.3.0", "5.3.0"],
                )

    def test_matrix_rejects_release_series_that_changes_variant_identity(self) -> None:
        for variant, version, required_line in (
            ("libtorrent2", "2.1.2", "2.0"),
            ("libtorrent21", "2.0.14", "2.1"),
            ("libtorrent21", "2.2.0", "2.1"),
            ("legacy", "2.1.2", "1.2"),
        ):
            with self.subTest(variant=variant, version=version):
                state = version_state(variant, version)

                with self.assertRaisesRegex(
                    BuildInputError,
                    re.escape(
                        f"Dockerfile.{variant} must use libtorrent {required_line}"
                    ),
                ):
                    build_matrices({variant: state}, (variant,))


class LibtorrentWorkflowTests(unittest.TestCase):
    def test_version_check_stages_every_supported_dockerfile(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/version-check.yml").read_text()
        commit_step = workflow.split("      - name: Commit image inputs\n", 1)[1]
        commit_step = commit_step.split("\n      - ", 1)[0]
        commands = [
            shlex.split(line)
            for line in commit_step.replace("\\\n", "").splitlines()
            if line.strip().startswith("git add ")
        ]
        staged_paths = {path for command in commands for path in command[2:]}

        self.assertEqual(set(DOCKERFILES.values()) - staged_paths, set())


if __name__ == "__main__":
    unittest.main()
