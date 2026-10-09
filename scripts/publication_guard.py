"""Prevent image tags from moving to an older published source revision."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess  # nosec B404 - only validated, shell-free Git/Docker reads below.
import sys
from collections.abc import Callable
from pathlib import Path

SOURCE_REVISION = re.compile(r"[0-9a-f]{40}\Z")
MANIFEST_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
IMAGE_REFERENCE = re.compile(r"saltydk/qbittorrent:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
PLATFORMS = ("linux/amd64", "linux/arm64")
REGISTRY_FAILURE = re.compile(
    r"authoriz|authenticat|credential|denied|insufficient_scope|timeout|timed out|"
    r"tls|ssl|dial tcp|network|connection|resolve|resolving|lookup|no such host",
    re.IGNORECASE,
)
MANIFEST_ABSENT = re.compile(
    r"manifest[ _]unknown|name[ _]unknown|no such manifest", re.IGNORECASE
)
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class PublicationError(RuntimeError):
    """Published state or Git ancestry could not be established reliably."""


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    if not command or command[0] not in {"git", "docker"}:
        raise PublicationError("publication inspection requires Git or Docker")
    executable = shutil.which(command[0])
    if executable is None:
        raise PublicationError(f"required executable is unavailable: {command[0]}")
    # Callers validate revision/image operands and use fixed inspection subcommands.
    return subprocess.run(  # nosec B603 - resolved allowed tool, argv only, no shell.
        [executable, *command[1:]],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=300,
    )


def published_revision(reference: str, runner: Runner) -> str | None:
    completed = runner(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{json .}}",
        ]
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        absent = MANIFEST_ABSENT.search(detail) or re.search(
            re.escape(reference) + r":\s*not found\b",
            detail,
            re.IGNORECASE,
        )
        if absent and not REGISTRY_FAILURE.search(detail):
            return None
        raise PublicationError(f"failed to inspect {reference}: {detail}")
    try:
        metadata = json.loads(completed.stdout)
        digest = metadata["manifest"]["digest"]
        revisions = {
            metadata["image"][platform]["config"]["Labels"][
                "org.opencontainers.image.revision"
            ]
            for platform in PLATFORMS
        }
        if not isinstance(digest, str) or not MANIFEST_DIGEST.fullmatch(digest):
            raise ValueError("invalid manifest digest")
        if len(revisions) != 1 or not all(
            isinstance(revision, str) and SOURCE_REVISION.fullmatch(revision)
            for revision in revisions
        ):
            raise ValueError("invalid source revisions")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise PublicationError(
            f"{reference} has missing, invalid, or inconsistent published source metadata"
        ) from error
    return revisions.pop()


def is_ancestor(older: str, newer: str, runner: Runner) -> bool:
    completed = runner(["git", "merge-base", "--is-ancestor", older, newer])
    if completed.returncode not in (0, 1):
        raise PublicationError(
            "cannot establish published source ancestry; full Git history is required"
        )
    return completed.returncode == 0


def check_publication(source_sha: str, tags: list[str], runner: Runner = run) -> bool:
    if not isinstance(source_sha, str) or not SOURCE_REVISION.fullmatch(source_sha):
        raise PublicationError("publication source must be a full Git revision")
    if (
        not isinstance(tags, list)
        or not tags
        or not all(
            isinstance(tag, str) and IMAGE_REFERENCE.fullmatch(tag) for tag in tags
        )
    ):
        raise PublicationError(
            "publication tags must be nonempty qBittorrent image references"
        )
    if runner(["git", "cat-file", "-e", f"{source_sha}^{{commit}}"]).returncode:
        raise PublicationError("publication source commit is unavailable")
    shallow = runner(["git", "rev-parse", "--is-shallow-repository"])
    if shallow.returncode or shallow.stdout.strip() != "false":
        raise PublicationError("full Git history is required for publication")
    eligible = True
    for tag in dict.fromkeys(tags):
        published_sha = published_revision(tag, runner)
        if published_sha is None:
            continue
        if published_sha == source_sha or is_ancestor(
            published_sha, source_sha, runner
        ):
            continue
        if is_ancestor(source_sha, published_sha, runner):
            print(
                f"::notice::Skipping {tag}: published source {published_sha} "
                f"is newer than candidate source {source_sha}."
            )
        else:
            print(
                f"::notice::Skipping {tag}: published source {published_sha} "
                f"diverges from candidate source {source_sha}."
            )
        eligible = False
    return eligible


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--tags-json", required=True)
    parser.add_argument("--github-output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if not args.source_ref or not args.default_branch:
            raise PublicationError(
                "workflow ref and repository default branch must be available"
            )
        if args.source_ref != f"refs/heads/{args.default_branch}":
            print(
                f"::notice::Skipping publication from {args.source_ref}; "
                f"only refs/heads/{args.default_branch} may publish image aliases."
            )
            eligible = False
        else:
            eligible = check_publication(args.source_sha, json.loads(args.tags_json))
        with args.github_output.open("a") as output:
            output.write(f"eligible={str(eligible).lower()}\n")
    except (
        KeyError,
        ValueError,
        OSError,
        subprocess.TimeoutExpired,
        PublicationError,
    ) as error:
        print(f"Publication rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
