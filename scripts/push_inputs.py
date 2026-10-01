#!/usr/bin/env python3
"""Push one generated input commit with reconciled transport retries."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

TRANSIENT = re.compile(
    r"fatal error in commit_refs|timed? out|connection reset|temporary failure|"
    r"HTTP (?:408|429|5\d\d)|remote end hung up|unexpected EOF|could not resolve host",
    re.IGNORECASE,
)


def run(command):
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=300)


def redact(value):
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(key):
            value = value.replace(os.environ[key], "<REDACTED>")
    return re.sub(r"https://[^\s/@]+:[^\s/@]+@", "https://<REDACTED>@", value)


def read_git(command, runner, sleep):
    for attempt in range(4):
        try:
            result = runner(command)
            if result.returncode == 0:
                return result.stdout.strip()
            detail = redact(result.stderr or result.stdout)
            retry = bool(TRANSIENT.search(detail))
        except subprocess.TimeoutExpired:
            detail, retry = "git read timed out", True
        if not retry or attempt == 3:
            raise RuntimeError(detail)
        sleep(2 ** attempt)
    raise AssertionError("unreachable")


def push_inputs(*, runner=run, sleep=time.sleep):
    head = read_git(["git", "rev-parse", "HEAD"], runner, sleep)
    parent = read_git(["git", "rev-parse", "HEAD^"], runner, sleep)
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (head, parent)):
        raise RuntimeError("generated input commit has invalid Git identity")
    for attempt in range(4):
        try:
            result = runner(["git", "push", "origin", "HEAD:refs/heads/main"])
            if result.returncode == 0:
                return
            detail = redact(result.stderr or result.stdout)
            retry = bool(TRANSIENT.search(detail))
        except subprocess.TimeoutExpired:
            detail, retry = "git push timed out", True
        if not retry:
            raise RuntimeError(detail)
        # An interrupted push may have succeeded. Observe its exact target
        # before replaying the same commit, and never force or rewrite history.
        remote = read_git(["git", "ls-remote", "origin", "refs/heads/main"], runner, sleep).split()
        if len(remote) != 2 or remote[1] != "refs/heads/main":
            raise RuntimeError("could not establish the current remote branch")
        if remote[0] == head:
            return
        if remote[0] != parent:
            raise RuntimeError("remote branch advanced during input publication; refusing to overwrite")
        if attempt == 3:
            raise RuntimeError(f"input publication failed after four attempts: {detail}")
        print(f"Transient GitHub push failure; retrying the same commit in {2 ** attempt}s", flush=True)
        sleep(2 ** attempt)


def main():
    try:
        push_inputs()
        return 0
    except (OSError, RuntimeError) as error:
        print(f"Input publication failed: {redact(str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
