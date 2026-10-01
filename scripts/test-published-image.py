#!/usr/bin/env python3
"""Run the existing runtime acceptance test for one frozen published target."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.manage_builds import build_matrices, load_states


def main():
    variant = os.environ['VARIANT']
    platform = os.environ['PLATFORM']
    states = load_states(ROOT)
    if variant not in states:
        raise ValueError('unknown published variant')
    targets = build_matrices(states, [variant])['candidates']['include']
    target = next(row for row in targets if row['platform'] == platform)
    return subprocess.run([
        str(ROOT / 'scripts/test-image.sh'), os.environ['IMAGE'], platform,
        target['architecture'], variant, target['qbittorrent_version'],
        target['libtorrent_prefix'], target['config_profile'],
    ], check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
