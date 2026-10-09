"""Describe the selected image inputs for the shared Discord notification action."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess  # nosec B404 - only a shell-free Git read with validated SHA below.
from pathlib import Path

from scripts.manage_builds import DOCKERFILES, VARIANT_ORDER, load_states, parse_variant

TAG = re.compile(r"^release-(.+)_v(.+)-(\d+)$")
BASE = re.compile(r"^saltydk/alpine-s6overlay:sha-([0-9a-f]{40})@")
BASE_COMMITS = "https://github.com/saltydk/docker-alpine-s6overlay/commit/"


def versions(tag):
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError(f"invalid versioned image tag: {tag}")
    return match.groups()


def build_data(
    states, selected, *, update, previous, event_name, event, run_id, run_attempt
):
    fields = []
    reasons = set()
    changes = set()
    selected = [name for name in VARIANT_ORDER if name in selected]
    for name in selected:
        current = versions(states[name].versioned_tag)
        detail = update.get("variants", {}).get(name) if update else None
        old = (
            versions(detail["current"])
            if detail
            else versions(previous[name].versioned_tag)
            if name in previous
            else current
        )
        if detail:
            reasons.update(detail["reasons"])
        lines = []
        for label, before, after in zip(
            ("qBittorrent", "libtorrent", "Revision"), old, current
        ):
            lines.append(
                f"{label}: {before} → {after}"
                if before != after
                else f"{label}: {after}"
            )
            if before != after:
                changes.add(label)
        fields.append({"name": name, "value": "\n".join(lines), "inline": True})

    parts = []
    base_pairs = set()
    if update:
        for label in ("qBittorrent", "libtorrent", "Revision"):
            if label in changes:
                parts.append(f"{label} updated")
        if "binary" in reasons:
            parts.append("Upstream binaries updated")
        if "base" in reasons:
            for image in update.get("images", []):
                for item in image.get("inputs", []):
                    if item["name"] == "base image" and item.get("old") != item.get(
                        "new"
                    ):
                        before, after = (
                            BASE.match(item.get("old") or ""),
                            BASE.match(item.get("new") or ""),
                        )
                        if before and after:
                            base_pairs.add((before[1], after[1]))
            if not base_pairs:
                parts.append("Base image updated")
        if "packages" in reasons and "base" not in reasons:
            parts.append("Package locks refreshed")
        if "pending-publication" in reasons:
            parts.append("Publication retry")
        if not parts and not base_pairs:
            parts.append("Update check; build details unavailable")
    elif event_name == "push":
        message = event.get("head_commit", {}).get("message") or "Commit update"
        parts.append("Commit: " + (message.splitlines() or ["Commit update"])[0][:300])
        if any(name not in previous for name in selected):
            parts.append("Previous input details unavailable")
        for name in selected:
            if (
                name in previous
                and previous[name].base_image != states[name].base_image
            ):
                before, after = (
                    BASE.match(previous[name].base_image),
                    BASE.match(states[name].base_image),
                )
                if before and after:
                    base_pairs.add((before[1], after[1]))
    elif event_name == "workflow_dispatch":
        parts.append("Manual build")
    else:
        parts.append("Scheduled build; update details unavailable")
    for before, after in sorted(base_pairs):
        parts.append(
            f"Base image updated: [{before[:7]}]({BASE_COMMITS}{before}) → [{after[:7]}]({BASE_COMMITS}{after})"
        )
    # Describe the trigger, not a successful result: the workflow may have failed.
    parts.append("Variants: " + ", ".join(selected))
    return {
        "schema": 1,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "event_details": "; ".join(parts) + ".",
        "fields": fields,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish-matrix-json", required=True)
    parser.add_argument("--published-variants-json", default="")
    parser.add_argument("--publish-result", required=True)
    parser.add_argument("--output", type=Path, default=Path("notification.json"))
    args = parser.parse_args(argv)
    root = Path(".")
    states = load_states(root)
    selected = [
        item["variant"] for item in json.loads(args.publish_matrix_json)["include"]
    ]
    if args.publish_result == "success":
        published = json.loads(args.published_variants_json)
        if not isinstance(published, list) or not all(
            variant in VARIANT_ORDER for variant in published
        ):
            raise ValueError(
                "published variants must be an array of supported image names"
            )
        selected = [variant for variant in selected if variant in published]
    report_path = Path("update-reports/update-report.json")
    update = json.loads(report_path.read_text()) if report_path.exists() else None
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    previous = {}
    before = event.get("before", "")
    if not update and re.fullmatch(r"[0-9a-f]{40}", before) and before != "0" * 40:
        executable = shutil.which("git")
        if executable is None:
            raise ValueError("required Git executable is unavailable")
        for name in selected:
            # The revision is a full SHA and recipe paths come from the fixed map.
            result = subprocess.run(  # nosec B603 - resolved Git and validated read argv.
                [executable, "show", f"{before}:{DOCKERFILES[name]}"],
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
            if result.returncode == 0:
                previous[name] = parse_variant(name, DOCKERFILES[name], result.stdout)
    data = build_data(
        states,
        selected,
        update=update,
        previous=previous,
        event_name=os.environ["GITHUB_EVENT_NAME"],
        event=event,
        run_id=int(os.environ["GITHUB_RUN_ID"]),
        run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
    )
    args.output.write_text(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    main()
