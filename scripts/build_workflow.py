"""Select build inputs and enforce the build workflow's required outcomes."""

from __future__ import annotations

import argparse
import subprocess  # nosec B404 - invokes only the repository manager without a shell.
import sys
from pathlib import Path

from scripts.package_locks import LockError, locks_digest


def run_manager(arguments: list[str]) -> int:
    # Both entry points are fixed; source identifiers remain separate argv values.
    completed = subprocess.run(  # nosec B603 - fixed interpreter and checked-out helper.
        [sys.executable, "scripts/manage_builds.py", *arguments],
        check=False,
        shell=False,
    )
    return (
        completed.returncode
        if completed.returncode >= 0
        else 128 - completed.returncode
    )


def append_output(path: Path, name: str, value: str) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"{name}={value}\n")


def verify_inputs(args: argparse.Namespace) -> int:
    status = run_manager(["verify"])
    if status == 0:
        append_output(args.github_output, "skip-build", "false")
        return 0
    if status != 3 or args.event_name != "push" or args.called_source_sha:
        return status
    append_output(args.github_output, "skip-build", "true")
    print(
        "::notice::Skipping image build and publication because the upstream release or build revision changed."
    )
    with args.summary.open("a", encoding="utf-8") as summary:
        summary.write(
            "Image build and publication skipped because the upstream release or build revision changed. "
            "The scheduled or manually run version-check workflow can refresh the inputs.\n"
        )
    return 0


def select_matrix(args: argparse.Namespace) -> int:
    arguments = ["matrix"]
    if args.called_source_sha and args.selected_variants_json:
        arguments.extend(("--variants", args.selected_variants_json))
    elif args.called_source_sha or args.event_name == "workflow_dispatch":
        arguments.append("--all")
    elif args.event_name == "pull_request":
        arguments.extend(("--before", args.base_sha, "--after", args.head_sha))
    else:
        arguments.extend(("--before", args.before_sha, "--after", args.head_sha))
    arguments.extend(("--github-output", str(args.github_output)))
    return run_manager(arguments)


def require_jobs(args: argparse.Namespace) -> int:
    required = {"static": args.static_result, "prepare": args.prepare_result}
    if args.has_builds == "true":
        required["candidates"] = args.test_result
        if args.should_publish == "true":
            required["publish"] = args.publish_result
    for name, outcome in required.items():
        if outcome != "success":
            print(
                f"Required {name} job did not succeed: {outcome or 'unknown'}",
                file=sys.stderr,
            )
            return 1
    return 0


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    verify = commands.add_parser(
        "verify", help="verify upstream inputs or skip an obsolete ordinary push"
    )
    verify.add_argument("--event-name", required=True)
    verify.add_argument("--called-source-sha", required=True)
    verify.add_argument("--github-output", type=Path, required=True)
    verify.add_argument("--summary", type=Path, required=True)
    matrix = commands.add_parser(
        "matrix", help="select variants from the caller, event, or changed source"
    )
    for option in (
        "event-name",
        "before-sha",
        "base-sha",
        "head-sha",
        "called-source-sha",
        "selected-variants-json",
    ):
        matrix.add_argument(f"--{option}", required=True)
    matrix.add_argument("--github-output", type=Path, required=True)
    locks = commands.add_parser(
        "locks", help="write the digest of the validated package lock set"
    )
    locks.add_argument("--source-root", type=Path, required=True)
    locks.add_argument("--github-output", type=Path, required=True)
    requirements = commands.add_parser(
        "requirements", help="enforce required job outcomes"
    )
    for option in (
        "static-result",
        "prepare-result",
        "has-builds",
        "test-result",
        "publish-result",
        "should-publish",
    ):
        requirements.add_argument(f"--{option}", required=True)
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "verify":
            return verify_inputs(args)
        if args.command == "matrix":
            return select_matrix(args)
        if args.command == "requirements":
            return require_jobs(args)
        append_output(args.github_output, "locks-sha", locks_digest(args.source_root))
    except (OSError, LockError) as error:
        print(f"Build workflow {args.command} failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
