#!/usr/bin/env python3
"""Manage qBittorrent image inputs and GitHub Actions build matrices."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable, Iterable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


VARIANT_ORDER = ("libtorrent1", "libtorrent2", "legacy")
DOCKERFILES = {name: f"Dockerfile.{name}" for name in VARIANT_ORDER}
PLATFORMS = (
    ("linux/amd64", "amd64", "x86_64"),
    ("linux/arm64", "arm64", "aarch64"),
)
REQUIRED_ARGS = (
    "BASE_IMAGE",
    "QBITTORRENT_REPOSITORY",
    "QBITTORRENT_RELEASE",
    "QBITTORRENT_REVISION",
    "QBITTORRENT_SHA256_AMD64",
    "QBITTORRENT_SHA256_ARM64",
)
ARG_PATTERN = re.compile(r"^ARG\s+([A-Z0-9_]+)=(?:\"([^\"]*)\"|(\S+))\s*$", re.MULTILINE)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
BASE_IMAGE_PATTERN = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
MAIN_REPOSITORY = "userdocs/qbittorrent-nox-static"
LEGACY_REPOSITORY = "userdocs/qbittorrent-nox-static-legacy"
MAIN_METADATA_URL = f"https://github.com/{MAIN_REPOSITORY}/releases/latest/download/dependency-version.json"
LEGACY_METADATA_URL = f"https://github.com/{LEGACY_REPOSITORY}/releases/latest/download/dependency-version.json"
BASE_IMAGE_REPOSITORY = "saltydk/alpine-s6overlay"
BASE_IMAGE_TAG = f"{BASE_IMAGE_REPOSITORY}:latest"
BASE_IMAGE_PLATFORMS = ("linux/amd64", "linux/arm64", "linux/arm/v7")
OCI_REVISION_LABEL = "org.opencontainers.image.revision"
IMAGE_REPOSITORY = "saltydk/qbittorrent"
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class BuildInputError(ValueError):
    """Raised when tracked build inputs violate their required contract."""


@dataclass(frozen=True)
class VariantState:
    name: str
    dockerfile: str
    repository: str
    release: str
    revision: int
    sha256_amd64: str
    sha256_arm64: str
    base_image: str

    @property
    def versioned_tag(self) -> str:
        return f"{self.release}-{self.revision}"


@dataclass(frozen=True)
class ArtifactTarget:
    release: str
    revision: int
    sha256_amd64: str
    sha256_arm64: str


@dataclass(frozen=True)
class UpdateDecision:
    state: VariantState
    reasons: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return any(reason in {"release", "revision", "binary", "base"} for reason in self.reasons)

    @property
    def rebuild(self) -> bool:
        return bool(self.reasons) and self.reasons != ("pending-publication",)


class UpdateProvider(Protocol):
    def artifact_targets(self) -> Mapping[str, ArtifactTarget]: ...

    def base_image(self) -> str: ...

    def is_published(self, variant: VariantState) -> bool: ...

    def packages_outdated(self, variant: VariantState) -> bool: ...


class HttpClient:
    def __init__(self, token: str | None = None, retries: int = 3) -> None:
        self.token = token
        self.retries = retries

    def get_json(self, url: str, allow_not_found: bool = False) -> object | None:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "docker-qbittorrent-updater"}
        if self.token and url.startswith("https://api.github.com/"):
            headers["Authorization"] = f"Bearer {self.token}"

        for attempt in range(1, self.retries + 1):
            try:
                with urlopen(Request(url, headers=headers), timeout=30) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code == 404 and allow_not_found:
                    return None
                if error.code not in {429, 500, 502, 503, 504} or attempt == self.retries:
                    raise BuildInputError(f"failed to fetch {url}: HTTP {error.code}") from error
            except (URLError, TimeoutError, json.JSONDecodeError) as error:
                if attempt == self.retries:
                    raise BuildInputError(f"failed to fetch {url}: {error}") from error
            time.sleep(attempt)
        raise AssertionError("unreachable")


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise BuildInputError(f"{description} must be a JSON object")
    return value


def _required_string(data: Mapping[str, object], key: str, description: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise BuildInputError(f"{description} is missing {key}")
    return value


def _revision(data: Mapping[str, object], key: str, description: str) -> int:
    value = _required_string(data, key, description)
    try:
        revision = int(value)
    except ValueError as error:
        raise BuildInputError(f"{description} has an invalid {key}") from error
    if revision < 0:
        raise BuildInputError(f"{description} has an invalid {key}")
    return revision


class LiveProvider:
    def __init__(self, http: HttpClient, runner: Runner = _run) -> None:
        self.http = http
        self.runner = runner

    def _target(self, repository: str, release: str, revision: int) -> ArtifactTarget:
        encoded_release = quote(release, safe="")
        release_data = _mapping(
            self.http.get_json(f"https://api.github.com/repos/{repository}/releases/tags/{encoded_release}"),
            f"{release} release",
        )
        amd64, arm64 = extract_asset_checksums(release_data)
        return ArtifactTarget(release, revision, amd64, arm64)

    def artifact_targets(self) -> Mapping[str, ArtifactTarget]:
        main = _mapping(self.http.get_json(MAIN_METADATA_URL), "main dependency metadata")
        qbittorrent = _required_string(main, "qbittorrent", "main dependency metadata")
        targets: dict[str, ArtifactTarget] = {}
        for name, key in (("libtorrent1", "libtorrent_1_2"), ("libtorrent2", "libtorrent_2_0")):
            libtorrent = _required_string(main, key, "main dependency metadata")
            release = f"release-{qbittorrent}_v{libtorrent}"
            metadata = _mapping(
                self.http.get_json(
                    f"https://github.com/{MAIN_REPOSITORY}/releases/download/{quote(release, safe='')}/dependency-version.json"
                ),
                f"{release} dependency metadata",
            )
            revision = _revision(metadata, "revision", f"{release} dependency metadata")
            targets[name] = self._target(MAIN_REPOSITORY, release, revision)

        legacy = _mapping(self.http.get_json(LEGACY_METADATA_URL), "legacy dependency metadata")
        legacy_qbittorrent = _required_string(legacy, "qbittorrent", "legacy dependency metadata")
        legacy_libtorrent = _required_string(legacy, "libtorrent_1_2", "legacy dependency metadata")
        legacy_revision = _revision(legacy, "revision", "legacy dependency metadata")
        legacy_release = f"release-{legacy_qbittorrent}_v{legacy_libtorrent}"
        targets["legacy"] = self._target(LEGACY_REPOSITORY, legacy_release, legacy_revision)
        return targets

    def base_image(self) -> str:
        command = [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            BASE_IMAGE_TAG,
            "--format",
            "{{json .}}",
        ]
        completed = self.runner(command)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise BuildInputError(f"failed to inspect base image: {detail}")
        try:
            manifest_json = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise BuildInputError("base image inspection returned invalid JSON") from error
        metadata = _mapping(manifest_json, "base image metadata")
        manifest = _mapping(metadata.get("manifest"), "base image manifest")
        digest = _required_string(manifest, "digest", "base image manifest")
        images = _mapping(metadata.get("image"), "base image platform metadata")
        revisions: set[str] = set()
        for platform in BASE_IMAGE_PLATFORMS:
            image = _mapping(images.get(platform), f"base image {platform} metadata")
            config = _mapping(image.get("config"), f"base image {platform} config")
            labels = _mapping(config.get("Labels"), f"base image {platform} labels")
            revision = _required_string(labels, OCI_REVISION_LABEL, f"base image {platform} labels")
            if not GIT_SHA_PATTERN.fullmatch(revision):
                raise BuildInputError(f"base image {platform} has an invalid OCI revision label")
            revisions.add(revision)
        if len(revisions) != 1:
            raise BuildInputError("base image platforms do not share one OCI revision label")

        revision = revisions.pop()
        sha_tag = f"{BASE_IMAGE_REPOSITORY}:sha-{revision}"
        tag_command = [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            sha_tag,
            "--format",
            "{{json .Manifest}}",
        ]
        tag_completed = self.runner(tag_command)
        if tag_completed.returncode != 0:
            detail = tag_completed.stderr.strip() or tag_completed.stdout.strip() or "unknown error"
            raise BuildInputError(f"failed to inspect base image SHA tag: {detail}")
        try:
            tag_manifest_json = json.loads(tag_completed.stdout)
        except json.JSONDecodeError as error:
            raise BuildInputError("base image SHA tag inspection returned invalid JSON") from error
        tag_manifest = _mapping(tag_manifest_json, "base image SHA tag manifest")
        tag_digest = _required_string(tag_manifest, "digest", "base image SHA tag manifest")
        if tag_digest != digest:
            raise BuildInputError("base image SHA tag does not match the latest manifest digest")

        base_image = f"{sha_tag}@{digest}"
        if not BASE_IMAGE_PATTERN.fullmatch(base_image):
            raise BuildInputError("base image manifest returned an invalid digest")
        return base_image

    def is_published(self, variant: VariantState) -> bool:
        tag = quote(variant.versioned_tag, safe="")
        response = self.http.get_json(
            f"https://hub.docker.com/v2/repositories/{IMAGE_REPOSITORY}/tags/{tag}",
            allow_not_found=True,
        )
        return response is not None

    def packages_outdated(self, variant: VariantState) -> bool:
        image = f"{IMAGE_REPOSITORY}:{variant.versioned_tag}"
        command = [
            "docker",
            "run",
            "--pull=always",
            "--rm",
            "--platform",
            "linux/amd64",
            "--entrypoint",
            "/bin/sh",
            image,
            "-ec",
            'apk update >/dev/null; apk version -l "<"',
        ]
        completed = self.runner(command)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise BuildInputError(f"failed to check packages in {image}: {detail}")
        return any(re.search(r"\s<\s", line) for line in completed.stdout.splitlines())


def _validate_sha256(value: str, field: str) -> None:
    if not SHA256_PATTERN.fullmatch(value):
        raise BuildInputError(f"{field} must be a lowercase SHA-256 digest")


def parse_variant(name: str, dockerfile: str, text: str) -> VariantState:
    values = {match.group(1): match.group(2) if match.group(2) is not None else match.group(3) for match in ARG_PATTERN.finditer(text)}
    missing = [argument for argument in REQUIRED_ARGS if argument not in values]
    if missing:
        raise BuildInputError(f"{dockerfile} is missing required arguments: {', '.join(missing)}")

    if not BASE_IMAGE_PATTERN.fullmatch(values["BASE_IMAGE"]):
        raise BuildInputError(f"{dockerfile} BASE_IMAGE must include a sha256 manifest digest")
    _validate_sha256(values["QBITTORRENT_SHA256_AMD64"], "QBITTORRENT_SHA256_AMD64")
    _validate_sha256(values["QBITTORRENT_SHA256_ARM64"], "QBITTORRENT_SHA256_ARM64")

    try:
        revision = int(values["QBITTORRENT_REVISION"])
    except ValueError as error:
        raise BuildInputError(f"{dockerfile} QBITTORRENT_REVISION must be a non-negative integer") from error
    if revision < 0:
        raise BuildInputError(f"{dockerfile} QBITTORRENT_REVISION must be a non-negative integer")

    return VariantState(
        name=name,
        dockerfile=dockerfile,
        repository=values["QBITTORRENT_REPOSITORY"],
        release=values["QBITTORRENT_RELEASE"],
        revision=revision,
        sha256_amd64=values["QBITTORRENT_SHA256_AMD64"],
        sha256_arm64=values["QBITTORRENT_SHA256_ARM64"],
        base_image=values["BASE_IMAGE"],
    )


def render_variant(text: str, state: VariantState) -> str:
    replacements = {
        "BASE_IMAGE": state.base_image,
        "QBITTORRENT_REPOSITORY": state.repository,
        "QBITTORRENT_RELEASE": state.release,
        "QBITTORRENT_REVISION": str(state.revision),
        "QBITTORRENT_SHA256_AMD64": state.sha256_amd64,
        "QBITTORRENT_SHA256_ARM64": state.sha256_arm64,
    }

    rendered = text
    for argument, value in replacements.items():
        pattern = re.compile(rf"^ARG\s+{re.escape(argument)}=.*$", re.MULTILINE)
        rendered, count = pattern.subn(f'ARG {argument}="{value}"', rendered, count=1)
        if count != 1:
            raise BuildInputError(f"{state.dockerfile} must contain exactly one {argument} argument")
    return rendered


def load_states(root: Path) -> dict[str, VariantState]:
    states = {
        name: parse_variant(name, dockerfile, (root / dockerfile).read_text(encoding="utf-8"))
        for name, dockerfile in DOCKERFILES.items()
    }
    if len({variant.base_image for variant in states.values()}) != 1:
        raise BuildInputError("all variants must use the same BASE_IMAGE")
    return states


def write_updates(root: Path, decisions: Mapping[str, UpdateDecision]) -> None:
    rendered: dict[Path, str] = {}
    for decision in decisions.values():
        if not decision.changed:
            continue
        path = root / decision.state.dockerfile
        original = path.read_text(encoding="utf-8")
        rendered[path] = render_variant(original, decision.state)

    for path, content in rendered.items():
        path.write_text(content, encoding="utf-8")


def extract_asset_checksums(release: Mapping[str, object]) -> tuple[str, str]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise BuildInputError("release response is missing assets")

    digests: dict[str, str] = {}
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        digest = item.get("digest")
        if name in {"x86_64-qbittorrent-nox", "aarch64-qbittorrent-nox"} and isinstance(digest, str):
            value = digest.removeprefix("sha256:")
            _validate_sha256(value, str(name))
            digests[str(name)] = value

    missing = [name for name in ("x86_64-qbittorrent-nox", "aarch64-qbittorrent-nox") if name not in digests]
    if missing:
        raise BuildInputError(f"release response is missing checksums for: {', '.join(missing)}")
    return digests["x86_64-qbittorrent-nox"], digests["aarch64-qbittorrent-nox"]


def compute_update(
    current: VariantState,
    target: ArtifactTarget,
    base_image: str,
    *,
    packages_outdated: bool,
    published: bool,
) -> UpdateDecision:
    _validate_sha256(target.sha256_amd64, "target amd64 checksum")
    _validate_sha256(target.sha256_arm64, "target arm64 checksum")
    if not BASE_IMAGE_PATTERN.fullmatch(base_image):
        raise BuildInputError("target base image must include a sha256 manifest digest")
    if target.revision < 0:
        raise BuildInputError("target upstream revision must be non-negative")

    release_changed = current.release != target.release
    revision_changed = current.revision != target.revision
    binary_changed = (
        current.sha256_amd64 != target.sha256_amd64
        or current.sha256_arm64 != target.sha256_arm64
    )
    base_changed = current.base_image != base_image

    if not published and not (release_changed or revision_changed or binary_changed or base_changed):
        if packages_outdated:
            return UpdateDecision(current, ("pending-publication",))
        return UpdateDecision(current, ())

    reasons: list[str] = []
    if release_changed:
        reasons.append("release")
    else:
        if revision_changed:
            reasons.append("revision")
        if binary_changed:
            reasons.append("binary")
    if base_changed:
        reasons.append("base")
    if packages_outdated:
        reasons.append("packages")

    if not reasons:
        return UpdateDecision(current, ())

    updated = replace(
        current,
        release=target.release,
        revision=target.revision,
        sha256_amd64=target.sha256_amd64,
        sha256_arm64=target.sha256_arm64,
        base_image=base_image,
    )
    return UpdateDecision(updated, tuple(reasons))


def plan_updates(
    states: Mapping[str, VariantState],
    provider: UpdateProvider,
) -> dict[str, UpdateDecision]:
    targets = provider.artifact_targets()
    base_image = provider.base_image()
    decisions: dict[str, UpdateDecision] = {}
    for name, current in states.items():
        if name not in targets:
            raise BuildInputError(f"no artifact target was resolved for {name}")
        published = provider.is_published(current)
        packages_outdated = provider.packages_outdated(current) if published else True
        decisions[name] = compute_update(
            current,
            targets[name],
            base_image,
            packages_outdated=packages_outdated,
            published=published,
        )
    return decisions


def validate_artifact_inputs(
    states: Mapping[str, VariantState],
    targets: Mapping[str, ArtifactTarget],
) -> None:
    for name, current in states.items():
        target = targets.get(name)
        if target is None:
            raise BuildInputError(f"no upstream artifact target was resolved for {name}")
        if current.revision != target.revision:
            raise BuildInputError(
                f"{name} QBITTORRENT_REVISION is {current.revision}; "
                f"upstream requires {target.revision}"
            )
        if current.release != target.release:
            raise BuildInputError(
                f"{name} QBITTORRENT_RELEASE is {current.release}; "
                f"upstream requires {target.release}"
            )
        if current.sha256_amd64 != target.sha256_amd64:
            raise BuildInputError(f"{name} amd64 checksum does not match the upstream artifact")
        if current.sha256_arm64 != target.sha256_arm64:
            raise BuildInputError(f"{name} arm64 checksum does not match the upstream artifact")


def select_variants(changed_paths: Iterable[str]) -> tuple[str, ...]:
    selected: set[str] = set()
    for path in changed_paths:
        if path.startswith("root/") or path.startswith(".github/workflows/") or path == "scripts/manage_builds.py":
            selected.update(VARIANT_ORDER)
        elif path.startswith("root-legacy/"):
            selected.add("legacy")
        else:
            for name, dockerfile in DOCKERFILES.items():
                if path == dockerfile:
                    selected.add(name)
                    break
    return tuple(name for name in VARIANT_ORDER if name in selected)


def _tags(state: VariantState) -> list[str]:
    tags = [
        f"saltydk/qbittorrent:{state.versioned_tag}",
        f"saltydk/qbittorrent:{state.release}",
        f"saltydk/qbittorrent:{state.name}",
    ]
    if state.name == "libtorrent1":
        tags.append("saltydk/qbittorrent:latest")
    return tags


def build_matrices(states: Mapping[str, VariantState], selected: Sequence[str]) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    publish: list[dict[str, object]] = []
    for name in selected:
        current = states[name]
        release_match = re.fullmatch(r"release-([^_]+)_v.+", current.release)
        if not release_match:
            raise BuildInputError(f"{current.dockerfile} has an invalid QBITTORRENT_RELEASE")
        qbittorrent_version = release_match.group(1)
        libtorrent_prefix = "2.0." if name == "libtorrent2" else "1.2."
        config_profile = "legacy" if name == "legacy" else "modern"
        publish.append(
            {
                "variant": name,
                "dockerfile": current.dockerfile,
                "release": current.release,
                "revision": current.revision,
                "tags": _tags(current),
            }
        )
        for platform, slug, architecture in PLATFORMS:
            candidates.append(
                {
                    "variant": name,
                    "dockerfile": current.dockerfile,
                    "platform": platform,
                    "platform_slug": slug,
                    "architecture": architecture,
                    "qbittorrent_version": qbittorrent_version,
                    "libtorrent_prefix": libtorrent_prefix,
                    "config_profile": config_profile,
                    "release": current.release,
                    "revision": current.revision,
                }
            )
    return {"candidates": {"include": candidates}, "publish": {"include": publish}}


def _changed_paths(root: Path, before: str, after: str) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", before, after, "--"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "git diff failed"
        raise BuildInputError(detail)
    return [line for line in completed.stdout.splitlines() if line]


def _write_github_outputs(path: Path, values: Mapping[str, str]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def _update_report(
    states: Mapping[str, VariantState],
    decisions: Mapping[str, UpdateDecision],
) -> dict[str, object]:
    changed = [name for name in VARIANT_ORDER if name in decisions and decisions[name].changed]
    rebuild = [name for name in VARIANT_ORDER if name in decisions and decisions[name].rebuild]
    pending = [
        name
        for name in VARIANT_ORDER
        if name in decisions and decisions[name].reasons == ("pending-publication",)
    ]
    return {
        "changed": changed,
        "rebuild": rebuild,
        "pending": pending,
        "variants": {
            name: {
                "current": states[name].versioned_tag,
                "target": decision.state.versioned_tag,
                "reasons": list(decision.reasons),
            }
            for name, decision in decisions.items()
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)

    update = subparsers.add_parser("update")
    update.add_argument("--write", action="store_true")
    update.add_argument("--github-output", type=Path)

    subparsers.add_parser("verify")

    matrix = subparsers.add_parser("matrix")
    selection = matrix.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--before")
    matrix.add_argument("--after")
    matrix.add_argument("--github-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    try:
        states = load_states(root)
        if args.command == "update":
            provider = LiveProvider(HttpClient(os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")))
            decisions = plan_updates(states, provider)
            if args.write:
                write_updates(root, decisions)
            report = _update_report(states, decisions)
            if args.github_output:
                _write_github_outputs(
                    args.github_output,
                    {
                        "changed": str(bool(report["changed"])).lower(),
                        "changed-variants": json.dumps(report["changed"], separators=(",", ":")),
                        "rebuild": str(bool(report["rebuild"])).lower(),
                        "rebuild-variants": json.dumps(report["rebuild"], separators=(",", ":")),
                        "report": json.dumps(report, separators=(",", ":")),
                    },
                )
        elif args.command == "verify":
            provider = LiveProvider(HttpClient(os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")))
            validate_artifact_inputs(states, provider.artifact_targets())
            report = {"verified": list(VARIANT_ORDER)}
        else:
            if args.all:
                selected = VARIANT_ORDER
            else:
                if not args.after:
                    raise BuildInputError("matrix --before requires --after")
                if args.before == "0" * 40:
                    selected = VARIANT_ORDER
                else:
                    changed_paths = _changed_paths(root, args.before, args.after)
                    selected = select_variants(changed_paths)
            matrices = build_matrices(states, selected)
            report = {"selected": list(selected), **matrices}
            if args.github_output:
                _write_github_outputs(
                    args.github_output,
                    {
                        "has-builds": str(bool(selected)).lower(),
                        "candidate-matrix": json.dumps(matrices["candidates"], separators=(",", ":")),
                        "publish-matrix": json.dumps(matrices["publish"], separators=(",", ":")),
                    },
                )
    except (BuildInputError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
