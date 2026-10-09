"""Select immutable publication scans and protect repository-wide issue reporting."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess  # nosec B404 - only a shell-free Docker manifest inspection below.
import sys
from pathlib import Path

VARIANTS = {"libtorrent1", "libtorrent2", "legacy"}
ARCHITECTURES = {"linux/amd64": "x86_64", "linux/arm64": "aarch64"}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
IMAGE = re.compile(r"saltydk/qbittorrent@sha256:[0-9a-f]{64}\Z")


def write_outputs(path: Path, outputs: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for name, value in outputs.items():
            stream.write(f"{name}={value}\n")


def select_targets(raw: str, output: Path) -> dict[str, str]:
    if not raw:
        targets = [
            {
                "name": variant,
                "platform": platform,
                "image": f"saltydk/qbittorrent:{variant}",
            }
            for variant in sorted(VARIANTS)
            for platform in ARCHITECTURES
        ]
        return {
            "recorded": "false",
            "targets": json.dumps(targets, separators=(",", ":")),
        }

    images = json.loads(raw)
    if (
        not isinstance(images, dict)
        or not images
        or not all(
            variant in VARIANTS and isinstance(image, str) and IMAGE.fullmatch(image)
            for variant, image in images.items()
        )
    ):
        raise ValueError(
            "published images must map supported variants to immutable image references"
        )
    targets = [
        {
            "name": variant,
            "platform": platform,
            "image": image,
            "tracking_reference": f"saltydk/qbittorrent:{variant}",
        }
        for variant, image in sorted(images.items())
        for platform in ARCHITECTURES
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(targets, indent=2) + "\n", encoding="utf-8")
    matrix = {
        "include": [
            {
                **target,
                "slug": target["platform"].replace("/", "-"),
                "architecture": ARCHITECTURES[target["platform"]],
            }
            for target in targets
        ]
    }
    return {
        "recorded": "true",
        "matrix": json.dumps(matrix, separators=(",", ":")),
        "expected-targets": str(output.resolve()),
    }


def read_targets(path: Path) -> list[dict[str, str]]:
    targets = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(targets, list) or not targets:
        raise ValueError("expected targets must be a nonempty list")
    identities = set()
    for target in targets:
        fields = {"name", "platform", "image", "tracking_reference"}
        if not isinstance(target, dict) or set(target) != fields:
            raise ValueError(
                "expected target requires name, platform, image, and tracking_reference"
            )
        name, platform, image = target["name"], target["platform"], target["image"]
        if (
            not isinstance(name, str)
            or name not in VARIANTS
            or not isinstance(platform, str)
            or platform not in ARCHITECTURES
        ):
            raise ValueError("unsupported expected target variant or platform")
        if not isinstance(image, str) or not IMAGE.fullmatch(image):
            raise ValueError(
                "expected targets must identify immutable qBittorrent images"
            )
        if target["tracking_reference"] != f"saltydk/qbittorrent:{name}":
            raise ValueError(
                "expected target tracking reference does not match its variant"
            )
        identity = name, platform
        if identity in identities:
            raise ValueError("duplicate expected target")
        identities.add(identity)
    return targets


def current_digest(reference: str) -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise ValueError("required Docker executable is unavailable")
    # read_targets accepts only the fixed repository's supported variant aliases.
    completed = subprocess.run(  # nosec B603 - resolved Docker and validated alias argv.
        [
            executable,
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{json .}}",
        ],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=300,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise ValueError(f"failed to inspect {reference}: {detail}")
    try:
        digest = json.loads(completed.stdout)["manifest"]["digest"]
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ValueError("invalid digest")
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"{reference} has invalid current published digest metadata"
        ) from error
    return digest


def check_scope(expected_targets: Path, output: Path) -> dict[str, str]:
    targets = read_targets(expected_targets)
    identities = {(target["name"], target["platform"]) for target in targets}
    complete = identities == {
        (variant, platform) for variant in VARIANTS for platform in ARCHITECTURES
    }
    reason = "" if complete else "only a subset of published variants was assessed"
    if complete:
        inspected = {}
        for target in targets:
            reference = target["tracking_reference"]
            if reference not in inspected:
                inspected[reference] = current_digest(reference)
            if target["image"].rsplit("@", 1)[-1] != inspected[reference]:
                complete = False
                reason = "a newer publication replaced an assessed image"
    if not complete:
        print(
            "::notice::Repository-wide issue reconciliation deferred to a full scheduled scan: "
            f"{reason}.",
            file=sys.stderr,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "complete": False,
                    "status": "deferred",
                    "reason": reason,
                    "assessed_targets": targets,
                    "actions": [],
                    "applied": [],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return {"eligible": str(complete).lower()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    targets = commands.add_parser(
        "targets", help="select recorded images or scheduled snapshot inputs"
    )
    targets.add_argument("--published-images-json", required=True)
    targets.add_argument("--github-output", type=Path, required=True)
    targets.add_argument("--output", type=Path, default=Path("expected-targets.json"))
    scope = commands.add_parser(
        "scope", help="check complete and current repository-wide coverage"
    )
    scope.add_argument("--expected-targets", type=Path, required=True)
    scope.add_argument("--github-output", type=Path, required=True)
    scope.add_argument(
        "--output", type=Path, default=Path("container-security-report.json")
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "targets":
            outputs = select_targets(args.published_images_json, args.output)
        else:
            outputs = check_scope(args.expected_targets, args.output)
        write_outputs(args.github_output, outputs)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Publication security rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
