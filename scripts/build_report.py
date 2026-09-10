#!/usr/bin/env python3
"""Capture image package baselines and produce human-readable build reports."""

from __future__ import annotations

import argparse
from html import escape
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable, Mapping, Sequence

try:
    from scripts import package_locks
except ImportError:  # Direct execution places scripts/ on sys.path.
    import package_locks  # type: ignore[no-redef]


SCHEMA_VERSION = 1
PLATFORM_ARCHITECTURES = {
    "linux/amd64": "x86_64",
    "linux/arm64": "aarch64",
    "linux/arm/v7": "armv7",
}
STAGES = ("runtime", "builder")
STATUSES = (
    "update-available",
    "no-changes",
    "waiting-for-base",
    "pending-publication",
    "failed",
    "built",
    "published",
)
STATUS_LABELS = {
    "waiting-for-base": "Waiting for base image",
    "pending-publication": "Publication pending",
    "update-available": "Updates available",
    "no-changes": "No changes",
    "failed": "Failed",
    "built": "Built",
    "published": "Published",
}
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
GIT_REVISION = re.compile(r"[0-9a-f]{40}\Z")
CONFIRMED_ABSENCE = re.compile(
    r"(?:manifest unknown|manifest[^\n]*not found|no such manifest|name unknown|404 not found)",
    re.IGNORECASE,
)
ACCESS_OR_TRANSPORT_FAILURE = re.compile(
    r"(?:unauthorized|authentication|denied|insufficient_scope|timeout|timed out|tls|dial tcp|network|connection)",
    re.IGNORECASE,
)
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
INPUT_LABELS = {
    "org.opencontainers.image.revision": "source revision",
    "org.opencontainers.image.base.name": "base image",
    "io.saltydk.qbittorrent.release": "qBittorrent release",
    "io.saltydk.qbittorrent.revision": "qBittorrent revision",
    "io.saltydk.unrar.version": "unrar version",
    "io.saltydk.s6.version": "s6-overlay version",
}


class ReportError(RuntimeError):
    """An image or report could not be read reliably."""


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _base_report(
    repository: str,
    kind: str,
    status: str,
    *,
    source_sha: str | None = None,
) -> dict[str, object]:
    report: dict[str, object] = {
        "schema": SCHEMA_VERSION,
        "kind": kind,
        "repository": repository,
        "status": status,
        "images": [],
        "changed_files": [],
        "outcomes": {},
        "published": [],
    }
    if source_sha:
        report["source_sha"] = source_sha
    return report


def _image_entry(name: str, platform: str, stage: str) -> dict[str, object]:
    return {
        "name": name,
        "platform": platform,
        "stage": stage,
        "changes": [],
        "inputs": [],
    }


def _detail(completed: subprocess.CompletedProcess[str]) -> str:
    return completed.stderr.strip() or completed.stdout.strip() or "unknown error"


def _confirmed_absence(detail: str, reference: str) -> bool:
    if ACCESS_OR_TRANSPORT_FAILURE.search(detail):
        return False
    return bool(
        CONFIRMED_ABSENCE.search(detail)
        or f"{reference.lower()}: not found" in detail.lower()
    )


def _inspect_remote(
    reference: str,
    platform: str,
    runner: Runner,
) -> tuple[str, str | None, dict[str, str]] | None:
    command = [
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        reference,
        "--format",
        "{{json .}}",
    ]
    completed = runner(command)
    if completed.returncode:
        detail = _detail(completed)
        if _confirmed_absence(detail, reference):
            return None
        raise ReportError(f"failed to inspect {reference}: {detail}")
    try:
        metadata = json.loads(completed.stdout)
        digest = metadata["manifest"]["digest"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ReportError(f"inspection for {reference} returned invalid metadata") from error
    if not isinstance(digest, str) or not SHA256_DIGEST.fullmatch(digest):
        raise ReportError(f"inspection for {reference} returned an invalid manifest digest")

    labels: dict[str, str] = {}
    try:
        raw_labels = metadata["image"][platform]["config"]["Labels"] or {}
        if isinstance(raw_labels, dict):
            labels = {
                str(key): value
                for key, value in raw_labels.items()
                if isinstance(key, str) and isinstance(value, str)
            }
    except (KeyError, TypeError):
        pass
    revision = labels.get("org.opencontainers.image.revision") or None
    return digest, revision, labels


def _inspect_local_labels(reference: str, runner: Runner) -> dict[str, str]:
    command = ["docker", "image", "inspect", reference, "--format", "{{json .Config.Labels}}"]
    completed = runner(command)
    if completed.returncode:
        raise ReportError(f"failed to inspect candidate {reference}: {_detail(completed)}")
    try:
        raw_labels = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ReportError(f"candidate inspection for {reference} returned invalid label metadata") from error
    if raw_labels is None:
        return {}
    if not isinstance(raw_labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_labels.items()
    ):
        raise ReportError(f"candidate inspection for {reference} returned invalid label metadata")
    return dict(raw_labels)


def _immutable_reference(reference: str, digest: str) -> str:
    return reference.partition("@")[0] + "@" + digest


def _parse_inventory_output(output: str) -> tuple[str, str | None, str | None, dict[str, str]]:
    lines = output.splitlines()
    if len(lines) < 4 or not lines[0].startswith("__APK_LOCK_ARCH__="):
        raise ReportError("container inventory omitted its architecture marker")
    if not lines[1].startswith("__APK_LOCK_DIGEST__=") or lines[2] != "__APK_LOCK_CONTENT__":
        raise ReportError("container inventory omitted its lock markers")
    try:
        inventory_marker = lines.index("__APK_LOCK_INVENTORY__", 3)
    except ValueError as error:
        raise ReportError("container inventory omitted its inventory marker") from error
    architecture = lines[0].partition("=")[2]
    embedded_digest = lines[1].partition("=")[2] or None
    if embedded_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", embedded_digest):
        raise ReportError("container returned an invalid embedded lock digest")
    embedded_lock = "\n".join(lines[3:inventory_marker])
    embedded_lock = embedded_lock + "\n" if embedded_lock else None
    if embedded_lock is not None:
        content_digest = hashlib.sha256(embedded_lock.encode("utf-8")).hexdigest()
        if embedded_digest != content_digest:
            raise ReportError("container embedded lock content does not match its digest")
    elif embedded_digest is not None:
        raise ReportError("container returned an embedded lock digest without lock content")

    canonical: list[str] = []
    for line in lines[inventory_marker + 1:]:
        if not line.strip():
            continue
        if "=" in line and not any(character.isspace() for character in line):
            canonical.append(line)
            continue
        fields = line.split()
        if len(fields) < 2:
            raise ReportError(f"container returned an invalid inventory row: {line!r}")
        canonical.append(f"{fields[0]}={fields[1]}")
    try:
        packages = package_locks.inventory("\n".join(canonical))
    except package_locks.LockError as error:
        raise ReportError(f"container returned an invalid inventory: {error}") from error
    return architecture, embedded_digest, embedded_lock, packages


def _read_image_inventory(
    reference: str,
    platform: str,
    stage: str,
    *,
    pull: bool,
    runner: Runner,
) -> tuple[str, str | None, str | None, dict[str, str]]:
    if stage not in STAGES:
        raise ReportError(f"unsupported image stage: {stage}")
    lock_path = f"/usr/share/image-inputs/{stage}.lock"
    script = f"""set -eu
printf '__APK_LOCK_ARCH__='
apk --print-arch
if [ -f {lock_path} ]; then
  printf '__APK_LOCK_DIGEST__='
  sha256sum {lock_path} | awk '{{print $1}}'
else
  printf '__APK_LOCK_DIGEST__=\n'
fi
printf '__APK_LOCK_CONTENT__\n'
if [ -f {lock_path} ]; then
  cat {lock_path}
fi
printf '__APK_LOCK_INVENTORY__\n'
if [ -x /usr/local/libexec/apk-lock ]; then
  /usr/local/libexec/apk-lock inventory
else
  apk list --installed --manifest
fi
"""
    command = ["docker", "run"]
    if pull:
        command.append("--pull=always")
    command.extend(
        [
            "--rm",
            "--platform",
            platform,
            "--entrypoint",
            "/bin/sh",
            reference,
            "-ec",
            script,
        ]
    )
    completed = runner(command)
    if completed.returncode:
        raise ReportError(f"failed to inventory {reference} on {platform}: {_detail(completed)}")
    return _parse_inventory_output(completed.stdout)


def snapshot_baseline(
    repository: str,
    image: str,
    platform: str,
    stage: str,
    reference: str,
    *,
    source_sha: str | None = None,
    runner: Runner = run,
) -> dict[str, object]:
    entry = _image_entry(image, platform, stage)
    entry["changes"] = None
    inspected = _inspect_remote(reference, platform, runner)
    if inspected is None:
        report = _base_report(repository, "build", "pending-publication", source_sha=source_sha)
        entry["baseline"] = {
            "reference": reference,
            "digest": None,
            "revision": None,
            "status": "absent",
        }
        report["images"] = [entry]
        return report

    digest, revision, labels = inspected
    immutable = _immutable_reference(reference, digest)
    architecture, embedded_digest, embedded_lock, packages = _read_image_inventory(
        immutable,
        platform,
        stage,
        pull=True,
        runner=runner,
    )
    report = _base_report(repository, "build", "built", source_sha=source_sha)
    baseline: dict[str, object] = {
        "reference": immutable,
        "digest": digest,
        "revision": revision,
        "status": "available",
        "architecture": architecture,
        "lock_digest": embedded_digest,
        "labels": labels,
    }
    if stage == "builder":
        if embedded_lock is None:
            baseline["status"] = "unavailable"
            baseline["inventory_source"] = "unavailable"
        else:
            try:
                retained_lock = package_locks.parse_lock(embedded_lock)
            except package_locks.LockError as error:
                raise ReportError(f"published builder lock is invalid: {error}") from error
            if retained_lock.architecture != architecture:
                raise ReportError(
                    f"published builder lock architecture {retained_lock.architecture} does not match {architecture}"
                )
            baseline["inventory"] = retained_lock.packages
            baseline["inventory_source"] = "embedded-lock"
    else:
        baseline["inventory"] = packages
        baseline["inventory_source"] = "installed"
    entry["baseline"] = baseline
    report["images"] = [entry]
    return report


def _matching_baseline(
    report: Mapping[str, object] | None,
    name: str,
    platform: str,
    stage: str,
) -> Mapping[str, object] | None:
    if report is None:
        return None
    images = report.get("images")
    if not isinstance(images, list):
        return None
    for image in images:
        if not isinstance(image, dict):
            continue
        if (image.get("name"), image.get("platform"), image.get("stage")) == (name, platform, stage):
            baseline = image.get("baseline")
            return baseline if isinstance(baseline, dict) else None
    return None


def _inventory_difference(expected: Mapping[str, str], actual: Mapping[str, str]) -> str:
    rows = package_locks.package_changes(expected, actual)
    return ", ".join(
        f"{row['name']}: expected {row['old'] if row['old'] is not None else 'absent'}, "
        f"actual {row['new'] if row['new'] is not None else 'absent'}"
        for row in rows
    )


def _input_changes(
    platform: str,
    baseline: Mapping[str, object] | None,
    candidate_labels: Mapping[str, str],
) -> list[dict[str, str | None]]:
    labels = dict(INPUT_LABELS)
    binary_architecture = {
        "linux/amd64": "amd64",
        "linux/arm64": "arm64",
    }.get(platform)
    if binary_architecture:
        labels[f"io.saltydk.qbittorrent.sha256.{binary_architecture}"] = "qBittorrent binary SHA-256"
    old_labels: Mapping[str, object] = {}
    if baseline is not None and isinstance(baseline.get("labels"), dict):
        old_labels = baseline["labels"]  # type: ignore[assignment]
    changes: list[dict[str, str | None]] = []
    for label, name in sorted(labels.items(), key=lambda item: item[1]):
        old_value = old_labels.get(label)
        new_value = candidate_labels.get(label)
        old = old_value if isinstance(old_value, str) else None
        if old != new_value and (old is not None or new_value is not None):
            changes.append({"name": name, "old": old, "new": new_value})
    return changes


def verify_candidate(
    repository: str,
    image: str,
    platform: str,
    stage: str,
    candidate: str,
    lock_path: Path,
    baseline_report: Mapping[str, object] | None,
    *,
    source_sha: str | None = None,
    runner: Runner = run,
) -> dict[str, object]:
    report = _base_report(repository, "build", "failed", source_sha=source_sha)
    entry = _image_entry(image, platform, stage)
    entry["changes"] = None
    entry["inputs"] = None
    baseline = _matching_baseline(baseline_report, image, platform, stage)
    entry["baseline"] = dict(baseline) if baseline is not None else {
        "reference": None,
        "digest": None,
        "revision": None,
        "status": "unavailable",
    }
    report["images"] = [entry]

    verification: dict[str, object] = {"status": "failed"}
    entry["verification"] = verification
    try:
        if platform not in PLATFORM_ARCHITECTURES:
            raise ReportError(f"unsupported platform: {platform}")
        lock = package_locks.parse_lock(lock_path.read_text(encoding="utf-8"))
        expected_architecture = PLATFORM_ARCHITECTURES[platform]
        if lock.architecture != expected_architecture:
            raise ReportError(
                f"lock architecture {lock.architecture} does not match {platform} ({expected_architecture})"
            )
        candidate_labels = _inspect_local_labels(candidate, runner)
        entry["inputs"] = _input_changes(platform, baseline, candidate_labels)
        architecture, embedded_digest, embedded_lock, actual = _read_image_inventory(
            candidate,
            platform,
            stage,
            pull=False,
            runner=runner,
        )
        verification.update(
            {
                "architecture": architecture,
                "lock_digest": lock.digest,
                "embedded_lock_digest": embedded_digest,
                "inventory": actual,
            }
        )
        entry["lock_digest"] = lock.digest
        if architecture != expected_architecture:
            raise ReportError(
                f"candidate architecture {architecture} does not match {platform} ({expected_architecture})"
            )
        if actual != lock.packages:
            raise ReportError("candidate inventory differs from lock: " + _inventory_difference(lock.packages, actual))
        if embedded_digest is None:
            raise ReportError(f"candidate embedded {stage} lock is unavailable")
        if embedded_lock != lock.render():
            raise ReportError(f"candidate embedded {stage} lock content does not match committed lock")
        if embedded_digest != lock.digest:
            raise ReportError(
                f"candidate embedded lock digest {embedded_digest} does not match committed lock {lock.digest}"
            )

        if baseline is not None and baseline.get("status") == "available":
            old_inventory = baseline.get("inventory")
            if not isinstance(old_inventory, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in old_inventory.items()
            ):
                raise ReportError("available baseline omitted its package inventory")
            entry["changes"] = package_locks.package_changes(old_inventory, actual)
        else:
            entry["changes"] = None
            verification["delta_status"] = (
                "first-publication"
                if baseline is not None and baseline.get("status") == "absent"
                else "unavailable"
            )
        verification["status"] = "passed"
        report["status"] = "built"
    except (OSError, ReportError, package_locks.LockError) as error:
        verification["error"] = str(error)
        report["error"] = str(error)
    return report


def _merge_rows(first: object, second: object) -> object:
    if first is None:
        return second
    if second is None:
        return first
    if not isinstance(first, list) or not isinstance(second, list):
        return second
    rows: dict[tuple[str, str, str], object] = {}
    for row in [*first, *second]:
        if isinstance(row, dict):
            key = tuple(json.dumps(row.get(field), sort_keys=True) for field in ("name", "old", "new"))
            rows[key] = dict(row)
    return [rows[key] for key in sorted(rows)]


def _merge_images(images: Sequence[object]) -> list[object]:
    merged: dict[tuple[object, object, object], dict[str, object]] = {}
    unkeyed: list[object] = []
    for value in images:
        if not isinstance(value, dict):
            unkeyed.append(value)
            continue
        key = (value.get("name"), value.get("platform"), value.get("stage"))
        if not all(isinstance(part, str) for part in key):
            unkeyed.append(value)
            continue
        if key not in merged:
            merged[key] = dict(value)
            continue
        current = merged[key]
        incoming_verified = isinstance(value.get("verification"), dict)
        current_verified = isinstance(current.get("verification"), dict)
        if incoming_verified:
            current["inputs"] = value.get("inputs")
        elif not current_verified:
            current["inputs"] = _merge_rows(current.get("inputs"), value.get("inputs"))
        if incoming_verified or not current_verified:
            if "changes" in value:
                current["changes"] = value["changes"]
        for field, field_value in value.items():
            if field not in {"inputs", "changes"} and field_value is not None:
                current[field] = field_value
    return [*merged.values(), *unkeyed]


def _plain_target(image: Mapping[str, object]) -> str:
    return f"{image.get('name', 'Unavailable')} ({image.get('platform', 'Unavailable')}, {image.get('stage', 'Unavailable')})"


def _source_comparisons(
    images: Sequence[object],
    source_sha: str | None,
    source_root: Path,
) -> tuple[list[dict[str, object]], list[str]]:
    grouped: dict[str | None, set[str]] = {}
    for image in images:
        if not isinstance(image, dict):
            continue
        baseline = image.get("baseline")
        previous = baseline.get("revision") if isinstance(baseline, dict) else None
        previous = previous if isinstance(previous, str) and previous else None
        grouped.setdefault(previous, set()).add(_plain_target(image))
    if not grouped:
        grouped[None] = set()

    comparisons: list[dict[str, object]] = []
    changed_files: set[str] = set()
    for previous in sorted(grouped, key=lambda value: value or ""):
        comparison: dict[str, object] = {
            "previous": previous,
            "current": source_sha,
            "status": "unavailable",
            "targets": sorted(grouped[previous]),
        }
        if not isinstance(source_sha, str) or not GIT_REVISION.fullmatch(source_sha):
            comparison["error"] = "current source revision is unavailable or invalid"
        elif previous is None or not GIT_REVISION.fullmatch(previous):
            comparison["error"] = "historical source revision is unavailable or invalid"
        else:
            command = [
                "git",
                "-C",
                str(source_root),
                "diff",
                "--name-only",
                "-z",
                previous,
                source_sha,
                "--",
            ]
            completed = run(command)
            if completed.returncode:
                comparison["error"] = (
                    f"git diff {previous}..{source_sha} unavailable: {_detail(completed)}"
                )
            else:
                files = sorted(set(path for path in completed.stdout.split("\0") if path))
                comparison["status"] = "compared"
                comparison["changed_files"] = files
                changed_files.update(files)
        comparisons.append(comparison)
    return comparisons, sorted(changed_files)


def aggregate_reports(
    repository: str,
    kind: str,
    status: str,
    reports: Sequence[Mapping[str, object]],
    *,
    source_sha: str | None = None,
    source_root: Path | None = None,
    changed_files: Sequence[str] = (),
    outcomes: Mapping[str, object] | None = None,
    published: Sequence[str] = (),
    error: str | None = None,
) -> dict[str, object]:
    aggregate = _base_report(repository, kind, status, source_sha=source_sha)
    images: list[object] = []
    all_changed_files = list(changed_files)
    all_outcomes: dict[str, object] = {}
    all_published = list(published)
    errors = [error] if error else []
    failed = status == "failed"

    for report in reports:
        report_images = report.get("images", [])
        if isinstance(report_images, list):
            images.extend(report_images)
            for image in report_images:
                if isinstance(image, dict):
                    verification = image.get("verification")
                    if isinstance(verification, dict) and verification.get("status") == "failed":
                        failed = True
                        image_error = verification.get("error")
                        if isinstance(image_error, str) and image_error:
                            errors.append(image_error)
        report_changed = report.get("changed_files", [])
        if isinstance(report_changed, list):
            all_changed_files.extend(item for item in report_changed if isinstance(item, str))
        report_outcomes = report.get("outcomes", {})
        if isinstance(report_outcomes, dict):
            all_outcomes.update(report_outcomes)
        report_published = report.get("published", [])
        if isinstance(report_published, list):
            all_published.extend(item for item in report_published if isinstance(item, str))
        report_error = report.get("error")
        if isinstance(report_error, str) and report_error:
            errors.append(report_error)
        if report.get("status") == "failed":
            failed = True

    if outcomes:
        all_outcomes.update(outcomes)
    aggregate_images = sorted(
        _merge_images(images),
        key=lambda item: (
            str(item.get("name", "")),
            str(item.get("platform", "")),
            str(item.get("stage", "")),
        ) if isinstance(item, dict) else ("", "", ""),
    )
    aggregate["images"] = aggregate_images
    if source_root is not None:
        comparisons, source_changed_files = _source_comparisons(
            aggregate_images,
            source_sha,
            source_root,
        )
        aggregate["source_comparisons"] = comparisons
        all_changed_files.extend(source_changed_files)
    aggregate["changed_files"] = sorted(set(all_changed_files))
    aggregate["outcomes"] = dict(sorted(all_outcomes.items()))
    aggregate["published"] = sorted(set(all_published))
    if failed:
        aggregate["status"] = "failed"
    if errors:
        aggregate["error"] = "; ".join(dict.fromkeys(errors))
    return aggregate


def _markdown(value: object) -> str:
    if value is None or value == "":
        return "Unavailable"
    return escape(str(value).replace("\r", " ").replace("\n", " ")).replace("|", "\\|")


def _target(image: Mapping[str, object]) -> str:
    return f"{_markdown(image.get('name'))} ({_markdown(image.get('platform'))}, {_markdown(image.get('stage'))})"


def _change_groups(images: Sequence[Mapping[str, object]], key: str) -> tuple[list[tuple[list[str], object, object, object]], list[str]]:
    grouped: dict[tuple[str, str, str], tuple[object, object, object, set[str]]] = {}
    unavailable: list[str] = []
    for image in images:
        target = _target(image)
        rows = image.get(key)
        if rows is None:
            unavailable.append(target)
            continue
        if not isinstance(rows, list):
            unavailable.append(target)
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            values = (row.get("name"), row.get("old"), row.get("new"))
            identity = tuple(json.dumps(value, sort_keys=True) for value in values)
            if identity not in grouped:
                grouped[identity] = (*values, set())
            grouped[identity][3].add(target)
    result = [
        (sorted(targets), name, old, new)
        for name, old, new, targets in grouped.values()
    ]
    result.sort(key=lambda row: (_markdown(row[1]), _markdown(row[2]), _markdown(row[3]), row[0]))
    return result, sorted(set(unavailable))


def render_markdown(report: Mapping[str, object]) -> str:
    kind = str(report.get("kind", "build")).capitalize()
    status = report.get("status")
    status_label = STATUS_LABELS.get(status, status) if isinstance(status, str) else status
    lines = [
        f"# {kind} report",
        "",
        f"- Repository: {_markdown(report.get('repository'))}",
        f"- Status: {_markdown(status_label)}",
    ]
    if "source_sha" in report:
        lines.append(f"- Source: `{_markdown(report.get('source_sha'))}`")
    if report.get("error"):
        lines.extend(["", f"**Error:** {_markdown(report['error'])}"])

    images_value = report.get("images", [])
    images = [image for image in images_value if isinstance(image, dict)] if isinstance(images_value, list) else []
    if images:
        lines.extend(
            [
                "",
                "## Images",
                "",
                "| Image | Platform | Stage | Baseline | Verification |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for image in images:
            baseline = image.get("baseline")
            baseline_text = "Unavailable"
            if isinstance(baseline, dict):
                baseline_status = baseline.get("status")
                if baseline_status == "available":
                    baseline_text = _markdown(baseline.get("reference") or baseline.get("digest"))
                elif baseline_status == "absent":
                    baseline_text = "Not published"
                elif baseline_status == "error":
                    baseline_text = "Unavailable"
                    if baseline.get("error"):
                        baseline_text += ": " + _markdown(baseline.get("error"))
            verification = image.get("verification")
            verification_text = "Unavailable"
            if isinstance(verification, dict):
                verification_text = _markdown(verification.get("status"))
                if verification.get("error"):
                    verification_text += ": " + _markdown(verification.get("error"))
            lines.append(
                f"| {_markdown(image.get('name'))} | {_markdown(image.get('platform'))} | "
                f"{_markdown(image.get('stage'))} | {baseline_text} | {verification_text} |"
            )

    changes, unavailable_changes = _change_groups(images, "changes")
    lines.extend(["", "## Package changes", ""])
    if changes:
        lines.extend(["| Targets | Package | Previous | Current |", "| --- | --- | --- | --- |"])
        for targets, name, old, new in changes:
            lines.append(
                f"| {'<br>'.join(targets)} | {_markdown(name)} | {_markdown(old)} | {_markdown(new)} |"
            )
    elif not unavailable_changes and (images or report.get("status") == "no-changes"):
        lines.append("No package changes.")
    elif not unavailable_changes:
        lines.append("Package delta unavailable; no image evidence was collected.")
    if unavailable_changes:
        lines.append("Package delta unavailable for: " + ", ".join(unavailable_changes) + ".")

    inputs, unavailable_inputs = _change_groups(images, "inputs")
    if inputs or unavailable_inputs:
        lines.extend(["", "## Input changes", ""])
        if inputs:
            lines.extend(["| Targets | Input | Previous | Current |", "| --- | --- | --- | --- |"])
            for targets, name, old, new in inputs:
                lines.append(
                    f"| {'<br>'.join(targets)} | {_markdown(name)} | {_markdown(old)} | {_markdown(new)} |"
                )
        if unavailable_inputs:
            lines.append("Input delta unavailable for: " + ", ".join(unavailable_inputs) + ".")

    source_comparisons = report.get("source_comparisons")
    if isinstance(source_comparisons, list) and source_comparisons:
        lines.extend(
            [
                "",
                "## Source comparison",
                "",
                "| Targets | Previous | Current | Status |",
                "| --- | --- | --- | --- |",
            ]
        )
        for comparison in source_comparisons:
            if not isinstance(comparison, dict):
                continue
            targets = comparison.get("targets")
            if isinstance(targets, list) and targets:
                target_text = "<br>".join(_markdown(target) for target in targets)
            else:
                target_text = "Unavailable"
            previous = comparison.get("previous")
            current = comparison.get("current")
            previous_text = f"`{_markdown(previous)}`" if previous else "Unavailable"
            current_text = f"`{_markdown(current)}`" if current else "Unavailable"
            status_text = _markdown(comparison.get("status"))
            if comparison.get("error"):
                status_text += ": " + _markdown(comparison.get("error"))
            lines.append(
                f"| {target_text} | {previous_text} | {current_text} | {status_text} |"
            )

    changed_files = report.get("changed_files")
    if isinstance(changed_files, list) and changed_files:
        lines.extend(["", "## Changed files", ""])
        lines.extend(f"- `{_markdown(path)}`" for path in changed_files)
    outcomes = report.get("outcomes")
    if isinstance(outcomes, dict) and outcomes:
        lines.extend(["", "## Outcomes", "", "| Check | Result |", "| --- | --- |"])
        lines.extend(
            f"| {_markdown(name)} | {_markdown(value)} |" for name, value in sorted(outcomes.items())
        )
    published = report.get("published")
    if isinstance(published, list) and published:
        lines.extend(["", "## Published", ""])
        lines.extend(f"- `{_markdown(reference)}`" for reference in published)
    return "\n".join(lines) + "\n"


def write_report(
    report: Mapping[str, object],
    json_path: Path | None = None,
    summary_path: Path | None = None,
) -> None:
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=json_path.parent,
            prefix=".build-report-",
            delete=False,
        ) as temporary:
            temporary.write(rendered)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        try:
            os.replace(temporary_path, json_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    if summary_path is not None:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with summary_path.open("a", encoding="utf-8") as summary:
            summary.write(render_markdown(report))


def _load_report(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReportError(f"failed to read report {path}: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != SCHEMA_VERSION:
        raise ReportError(f"{path} is not a schema version {SCHEMA_VERSION} report")
    return value


def _failure_report(
    repository: str,
    kind: str,
    error: Exception,
    *,
    source_sha: str | None,
    image: str | None = None,
    platform: str | None = None,
    stage: str | None = None,
    reference: str | None = None,
) -> dict[str, object]:
    report = _base_report(repository, kind, "failed", source_sha=source_sha)
    report["error"] = str(error)
    if image and platform and stage:
        entry = _image_entry(image, platform, stage)
        entry["changes"] = None
        entry["baseline"] = {
            "reference": reference,
            "digest": None,
            "revision": None,
            "status": "error",
            "error": str(error),
        }
        report["images"] = [entry]
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser, *, image: bool = False) -> None:
        command.add_argument("--repository", required=True)
        command.add_argument("--source-sha")
        command.add_argument("--output", type=Path)
        command.add_argument("--summary", type=Path)
        if image:
            command.add_argument("--image", required=True)
            command.add_argument("--platform", required=True, choices=tuple(PLATFORM_ARCHITECTURES))
            command.add_argument("--stage", required=True, choices=STAGES)

    snapshot = subparsers.add_parser("snapshot", help="capture a published image baseline")
    common(snapshot, image=True)
    snapshot.add_argument("--reference", required=True)

    verify = subparsers.add_parser("verify", help="verify a candidate image against a package lock")
    common(verify, image=True)
    verify.add_argument("--candidate", required=True)
    verify.add_argument("--lock", required=True, type=Path)
    verify.add_argument("--baseline", type=Path)

    aggregate = subparsers.add_parser("aggregate", help="combine per-image reports")
    common(aggregate)
    aggregate.add_argument("--kind", required=True, choices=("update", "build"))
    aggregate.add_argument("--status", required=True, choices=STATUSES)
    aggregate.add_argument("--source-root", type=Path)
    aggregate.add_argument("--image-report", action="append", type=Path, default=[])
    aggregate.add_argument("--changed-file", action="append", default=[])
    aggregate.add_argument("--outcome", action="append", default=[])
    aggregate.add_argument("--published", action="append", default=[])
    aggregate.add_argument("--error")
    return parser


def _outcomes(values: Sequence[str]) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    for value in values:
        name, separator, result = value.partition("=")
        if not separator or not name or not result:
            raise ReportError(f"invalid outcome {value!r}; expected NAME=VALUE")
        outcomes[name] = result
    return outcomes


def main(argv: Sequence[str] | None = None, *, runner: Runner = run) -> int:
    args = _parser().parse_args(argv)
    report: dict[str, object]
    try:
        if args.command == "snapshot":
            report = snapshot_baseline(
                args.repository,
                args.image,
                args.platform,
                args.stage,
                args.reference,
                source_sha=args.source_sha,
                runner=runner,
            )
        elif args.command == "verify":
            baseline = _load_report(args.baseline) if args.baseline else None
            report = verify_candidate(
                args.repository,
                args.image,
                args.platform,
                args.stage,
                args.candidate,
                args.lock,
                baseline,
                source_sha=args.source_sha,
                runner=runner,
            )
        else:
            image_reports = [_load_report(path) for path in args.image_report]
            report = aggregate_reports(
                args.repository,
                args.kind,
                args.status,
                image_reports,
                source_sha=args.source_sha,
                source_root=args.source_root,
                changed_files=args.changed_file,
                outcomes=_outcomes(args.outcome),
                published=args.published,
                error=args.error,
            )
    except (OSError, ReportError, package_locks.LockError) as error:
        report = _failure_report(
            args.repository,
            "build" if args.command != "aggregate" else args.kind,
            error,
            source_sha=args.source_sha,
            image=getattr(args, "image", None),
            platform=getattr(args, "platform", None),
            stage=getattr(args, "stage", None),
            reference=getattr(args, "reference", None),
        )
    write_report(report, json_path=args.output, summary_path=args.summary)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    if report.get("status") == "failed":
        if report.get("error"):
            print(str(report["error"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
