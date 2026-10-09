"""Record publication receipts and report only images this build published."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.build_report import (
    SCHEMA_VERSION,
    ReportError,
    aggregate_reports,
    write_report,
)
from scripts.manage_builds import VARIANT_ORDER
from scripts.publication_guard import IMAGE_REFERENCE, MANIFEST_DIGEST


def publication_tags(raw: str) -> list[str]:
    tags = json.loads(raw)
    if (
        not isinstance(tags, list)
        or not tags
        or not all(
            isinstance(tag, str) and IMAGE_REFERENCE.fullmatch(tag) for tag in tags
        )
    ):
        raise ValueError(
            "publication tags must be nonempty qBittorrent image references"
        )
    return tags


def record(args: argparse.Namespace) -> None:
    tags = publication_tags(args.tags_json)
    if not MANIFEST_DIGEST.fullmatch(args.digest):
        raise ValueError("publication returned an invalid manifest digest")
    if f"saltydk/qbittorrent:{args.variant}" not in tags:
        raise ValueError("publication tags must include the selected variant alias")
    args.output_directory.mkdir(parents=True, exist_ok=True)
    path = args.output_directory / f"published-{args.variant}.txt"
    path.write_text("".join(f"{tag}@{args.digest}\n" for tag in tags))


def outcomes(args: argparse.Namespace) -> None:
    tags = publication_tags(args.tags_json)
    values = {
        "source-guard": args.source_guard,
        "eligible": args.eligible,
        "build": args.build_result,
        "digest": args.digest,
        "tags": ",".join(tags),
        "record": args.record_result,
    }
    args.output_directory.mkdir(parents=True, exist_ok=True)
    path = args.output_directory / f"publication-{args.variant}-outcomes.txt"
    path.write_text(
        "".join(f"{args.variant}-{key}={value}\n" for key, value in values.items())
    )


def read_outcomes(root: Path) -> dict[str, str]:
    values = {}
    for path in sorted(root.rglob("publication-*-outcomes.txt")):
        for line in path.read_text().splitlines():
            key, separator, value = line.partition("=")
            if not separator or not key or key in values:
                raise ValueError(f"invalid or duplicate publication outcome in {path}")
            values[key] = value or "unknown"
    return values


def read_publications(root: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    images = {}
    receipts = {}
    for path in sorted(root.rglob("published-*.txt")):
        variant = path.stem.removeprefix("published-")
        if variant not in VARIANT_ORDER or variant in images:
            raise ValueError(f"unknown or duplicate publication variant in {path}")
        references = path.read_text().splitlines()
        digests = set()
        for reference in references:
            tag, separator, digest = reference.partition("@")
            if (
                not separator
                or not IMAGE_REFERENCE.fullmatch(tag)
                or not MANIFEST_DIGEST.fullmatch(digest)
            ):
                raise ValueError(f"invalid published digest receipt for {variant}")
            digests.add(digest)
        if len(digests) != 1:
            raise ValueError(f"inconsistent published digest receipt for {variant}")
        digest = digests.pop()
        if f"saltydk/qbittorrent:{variant}@{digest}" not in references:
            raise ValueError(f"missing published variant receipt for {variant}")
        images[variant] = f"saltydk/qbittorrent@{digest}"
        receipts[variant] = references
    return images, receipts


def validate_success(
    matrix: str,
    publications: dict[str, str],
    receipts: dict[str, list[str]],
    values: dict[str, str],
) -> None:
    rows = json.loads(matrix)["include"]
    selected = [row["variant"] for row in rows]
    if len(set(selected)) != len(selected) or any(
        variant not in VARIANT_ORDER for variant in selected
    ):
        raise ValueError("invalid publication matrix variants")
    if set(publications) - set(selected):
        raise ValueError("publication receipt does not belong to the selected matrix")
    for row in rows:
        variant = row["variant"]
        eligible = values[f"{variant}-eligible"]
        expected = "success" if eligible == "true" else "skipped"
        if (
            values[f"{variant}-source-guard"] != "success"
            or eligible not in {"true", "false"}
            or any(
                values[f"{variant}-{step}"] != expected for step in ("build", "record")
            )
            or (variant in publications) != (eligible == "true")
        ):
            raise ValueError(f"inconsistent publication outcomes for {variant}")
        if eligible == "true":
            digest = publications[variant].rsplit("@", 1)[-1]
            if values[f"{variant}-digest"] != digest:
                raise ValueError(
                    f"publication receipt does not match the build digest for {variant}"
                )
            tags = publication_tags(json.dumps(row["tags"]))
            if set(receipts[variant]) != {f"{tag}@{digest}" for tag in tags}:
                raise ValueError(
                    f"publication receipt does not cover all selected tags for {variant}"
                )


def write_publication_outputs(path: Path, publications: dict[str, str]) -> None:
    variants = sorted(publications)
    values = {
        "has-publications": str(bool(variants)).lower(),
        "published-variants": json.dumps(variants, separators=(",", ":")),
        "published-images": json.dumps(publications, separators=(",", ":")),
    }
    with path.open("a") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def report(args: argparse.Namespace) -> dict[str, object]:
    for directory in (
        args.candidate_directory,
        args.update_directory,
        args.publication_directory,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    publications, receipts = read_publications(args.publication_directory)
    values = read_outcomes(args.publication_directory)
    if args.should_publish == "true" and args.publish_result == "success":
        validate_success(args.publish_matrix_json, publications, receipts, values)
    write_publication_outputs(args.github_output, publications)
    status = "failed"
    if (
        args.has_builds == "false"
        and args.static_result == "success"
        and args.prepare_result == "success"
    ):
        status = "no-build-required"
    elif args.test_result == "success":
        if args.should_publish == "true":
            if args.publish_result == "success":
                status = "published" if publications else "superseded"
        else:
            status = "built"
    reports = []
    for directory in (args.candidate_directory, args.update_directory):
        for path in sorted(directory.rglob("*.json")):
            payload = json.loads(path.read_text())
            if not isinstance(payload, dict) or payload.get("schema") != SCHEMA_VERSION:
                raise ValueError(
                    f"{path} is not a schema version {SCHEMA_VERSION} report"
                )
            reports.append(payload)
    job_outcomes = {
        "static": args.static_result,
        "prepare": args.prepare_result,
        "candidates": args.test_result,
        "publish": args.publish_result,
    }
    job_outcomes.update({f"publication-{key}": value for key, value in values.items()})
    return aggregate_reports(
        args.repository,
        "build",
        status,
        reports,
        source_sha=args.source_sha,
        source_root=args.source_root,
        outcomes=job_outcomes,
        published=[
            reference for references in receipts.values() for reference in references
        ],
    )


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("record", "outcomes"):
        command = commands.add_parser(name)
        command.add_argument("--variant", required=True, choices=VARIANT_ORDER)
        command.add_argument("--digest", required=True)
        command.add_argument("--tags-json", required=True)
        command.add_argument("--output-directory", type=Path, default=Path("."))
        if name == "outcomes":
            for option in ("source-guard", "eligible", "build-result", "record-result"):
                command.add_argument(f"--{option}", required=True)
    grouped = commands.add_parser("report")
    for option in (
        "repository",
        "source-sha",
        "static-result",
        "prepare-result",
        "test-result",
        "publish-result",
        "has-builds",
        "should-publish",
        "publish-matrix-json",
    ):
        grouped.add_argument(f"--{option}", required=True)
    grouped.add_argument("--github-output", required=True, type=Path)
    grouped.add_argument("--summary", required=True, type=Path)
    grouped.add_argument("--output", type=Path, default=Path("build-report.json"))
    grouped.add_argument("--source-root", type=Path, default=Path("."))
    grouped.add_argument(
        "--candidate-directory", type=Path, default=Path("candidate-reports")
    )
    grouped.add_argument(
        "--update-directory", type=Path, default=Path("update-reports")
    )
    grouped.add_argument(
        "--publication-directory", type=Path, default=Path("published-tags")
    )
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "record":
            record(args)
        elif args.command == "outcomes":
            outcomes(args)
        else:
            payload = report(args)
            write_report(payload, json_path=args.output, summary_path=args.summary)
            print(f"Build report status: {payload['status']}")
            return 1 if payload["status"] == "failed" else 0
    except (OSError, KeyError, TypeError, ValueError, ReportError) as error:
        if args.command == "report":
            evidence = {
                "static": args.static_result,
                "prepare": args.prepare_result,
                "candidates": args.test_result,
                "publish": args.publish_result,
            }
            try:
                evidence.update(
                    {
                        f"publication-{key}": value
                        for key, value in read_outcomes(
                            args.publication_directory
                        ).items()
                    }
                )
            except (OSError, ValueError):
                pass
            payload = aggregate_reports(
                args.repository,
                "build",
                "failed",
                [],
                source_sha=args.source_sha,
                outcomes=evidence,
                error=str(error),
            )
            write_report(payload, json_path=args.output, summary_path=args.summary)
        print(f"Publication {args.command} failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
