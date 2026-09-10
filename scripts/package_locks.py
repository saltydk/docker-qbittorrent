"""Canonical APK lock files and disposable-container package resolution."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Mapping, Sequence


ARCHITECTURES = {"linux/amd64": "x86_64", "linux/arm64": "aarch64", "linux/arm/v7": "armv7"}
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9+_.-]*\Z")
VERSION = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9+_.:~\-]*\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PARENT = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}\Z")


class LockError(RuntimeError):
    """Package inputs cannot be resolved or verified reliably."""


class BaseUpdateRequired(LockError):
    """The inherited base must be refreshed before downstream resolution."""


def inventory(text: str) -> dict[str, str]:
    packages: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, version = line.partition("=")
        if not separator or not NAME.fullmatch(name) or not VERSION.fullmatch(version):
            raise LockError(f"invalid exact package constraint: {line!r}")
        if name in packages:
            raise LockError(f"duplicate package in inventory: {name}")
        packages[name] = version
    if not packages:
        raise LockError("package inventory is empty")
    return dict(sorted(packages.items()))


@dataclass(frozen=True)
class PackageLock:
    architecture: str
    parent: str
    requests_sha256: str
    packages: dict[str, str]

    def render(self) -> str:
        if self.architecture not in ARCHITECTURES.values():
            raise LockError(f"unsupported APK architecture: {self.architecture}")
        if not PARENT.fullmatch(self.parent):
            raise LockError("lock parent must be an image pinned by SHA-256 digest")
        if not SHA256.fullmatch(self.requests_sha256):
            raise LockError("invalid request-list SHA-256")
        body = "".join(f"{name}={version}\n" for name, version in sorted(self.packages.items()))
        inventory(body)
        return (f"# apk-lock: 1\n# architecture: {self.architecture}\n"
                f"# parent: {self.parent}\n# requests-sha256: {self.requests_sha256}\n{body}")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.render().encode("utf-8")).hexdigest()


def parse_lock(text: str) -> PackageLock:
    headers: dict[str, str] = {}
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("# "):
            key, separator, value = line[2:].partition(": ")
            if not separator or key in headers:
                raise LockError("invalid or duplicate lock header")
            headers[key] = value
        else:
            body.append(line)
    if headers.get("apk-lock") != "1":
        raise LockError("unsupported APK lock format")
    if set(headers) != {"apk-lock", "architecture", "parent", "requests-sha256"}:
        raise LockError("missing or unknown lock metadata")
    lock = PackageLock(headers["architecture"], headers["parent"],
                       headers["requests-sha256"], inventory("\n".join(body)))
    if lock.render() != text:
        raise LockError("lock is not canonical; regenerate package locks")
    return lock


def read_requests(path: Path) -> tuple[str, ...]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.partition("#")[0].strip()
        if not name:
            continue
        if not NAME.fullmatch(name):
            raise LockError(f"{path}: expected an unversioned package name, got {name!r}")
        names.add(name)
    if not names:
        raise LockError(f"{path}: package requests are empty")
    return tuple(sorted(names))


def requests_digest(requests: Sequence[str]) -> str:
    normalized = "".join(name + "\n" for name in sorted(set(requests)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def lock_path(root: Path, profile: str, architecture: str) -> Path:
    if profile not in {"builder", "runtime"} or architecture not in ARCHITECTURES.values():
        raise LockError(f"unsupported lock profile/architecture: {profile}/{architecture}")
    return root / "packages" / profile / f"{architecture}.lock"


def validate_lock(root: Path, profile: str, architecture: str, parent: str) -> PackageLock:
    path = lock_path(root, profile, architecture)
    lock = parse_lock(path.read_text(encoding="utf-8"))
    expected_requests = requests_digest(read_requests(path.parent / "requested.txt"))
    if lock.architecture != architecture or lock.parent != parent or lock.requests_sha256 != expected_requests:
        raise LockError(f"{path}: stale lock metadata; regenerate locks for the requested packages and parent image")
    return lock


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def platform_image_reference(parent: str, platform: str, *, runner=run) -> str:
    """Use a child manifest so classic Docker stores can hold each architecture."""
    if platform not in ARCHITECTURES or not PARENT.fullmatch(parent):
        raise LockError("platform selection requires a supported platform and digest-pinned parent")
    completed = runner(["docker", "buildx", "imagetools", "inspect", parent, "--raw"])
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise LockError(f"failed to inspect parent for {platform}: {detail}")
    try:
        manifest = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise LockError(f"invalid parent manifest for {platform}") from error
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
        raise LockError(f"invalid parent manifest for {platform}")
    descriptors = manifest.get("manifests")
    if not isinstance(descriptors, list):
        if (manifest.get("schemaVersion") == 2 and isinstance(manifest.get("config"), dict)
                and isinstance(manifest.get("layers"), list)):
            return parent
        raise LockError(f"parent manifest has no image or platform list for {platform}")
    operating_system, architecture, *variant = platform.split("/")
    if variant:
        variants = {variant[0]}
    elif architecture == "arm64":
        variants = {"", "v8"}
    else:
        variants = {"", "v1"}
    matches = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("platform"), dict):
            continue
        metadata = descriptor["platform"]
        if (metadata.get("os") == operating_system and metadata.get("architecture") == architecture
                and isinstance(metadata.get("variant", ""), str) and metadata.get("variant", "") in variants):
            matches.append(descriptor.get("digest"))
    if len(matches) != 1:
        raise LockError(f"parent manifest must contain exactly one image for {platform}")
    digest = matches[0]
    if not isinstance(digest, str) or not digest.startswith("sha256:") or not SHA256.fullmatch(digest[7:]):
        raise LockError(f"parent manifest has an invalid image digest for {platform}")
    return parent.partition("@")[0] + "@" + digest


def resolve_lock(root: Path, profile: str, platform: str, parent: str, *,
                 inherited: bool = False, helper: Path | None = None, runner=run) -> PackageLock:
    if platform not in ARCHITECTURES:
        raise LockError(f"unsupported platform: {platform}")
    if not PARENT.fullmatch(parent):
        raise LockError("package resolution requires a parent image pinned by SHA-256 digest")
    requests_path = (root / "packages" / profile / "requested.txt").resolve()
    requests = read_requests(requests_path)
    image = platform_image_reference(parent, platform, runner=runner)
    command = ["docker", "run", "--rm", "--pull=always", "--platform", platform,
               "--mount", f"type=bind,source={requests_path},target=/tmp/apk-requests.txt,readonly"]
    executable = "/usr/local/libexec/apk-lock"
    if helper is not None:
        executable = "/tmp/apk-lock"
        command.extend(["--mount", f"type=bind,source={helper.resolve()},target={executable},readonly"])
    script = '''actual=$(apk --print-arch)
if [ "$actual" != "$1" ]; then
  printf 'package architecture mismatch: expected %s, got %s\\n' "$1" "$actual" >&2
  exit 1
fi
exec /bin/sh "$2" resolve "$3" "$4"
'''
    command.extend(["--entrypoint", "/bin/sh", image, "-ec", script, "apk-lock",
                    ARCHITECTURES[platform], executable, "/tmp/apk-requests.txt",
                    "inherited" if inherited else "base"])
    completed = runner(command)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        if inherited and "apk-lock: inherited package constraints conflict:" in detail:
            raise BaseUpdateRequired(f"base update required for {profile} on {platform}: {detail}")
        if (inherited and helper is None and "/usr/local/libexec/apk-lock" in detail
                and ("No such file" in detail or "not found" in detail)):
            raise BaseUpdateRequired(
                f"base image {parent} does not provide the APK lock helper; "
                "publish and adopt the locked base image before refreshing qBittorrent packages"
            )
        raise LockError(f"failed to resolve {profile} on {platform}: {detail}")
    return PackageLock(ARCHITECTURES[platform], parent, requests_digest(requests), inventory(completed.stdout))


def package_changes(old: Mapping[str, str], new: Mapping[str, str]) -> list[dict[str, str | None]]:
    return [{"name": name, "old": old.get(name), "new": new.get(name)}
            for name in sorted(old.keys() | new.keys()) if old.get(name) != new.get(name)]


def input_files_digest(root: Path, paths: Sequence[Path],
                       overrides: Mapping[Path, str] | None = None) -> str:
    """Hash Git-representable file content, executable bits, and symlink identity."""
    overrides = overrides or {}
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink() and path not in overrides:
            kind = b"symlink"
            payload = os.fsencode(os.readlink(path))
        else:
            executable = path.exists() and bool(path.stat().st_mode & 0o111)
            kind = b"executable" if executable else b"file"
            payload = overrides[path].encode("utf-8") if path in overrides else path.read_bytes()
        for part in (relative, kind, payload):
            digest.update(str(len(part)).encode("ascii") + b":" + part)
    return digest.hexdigest()


def write_locks(locks: Mapping[Path, PackageLock]) -> None:
    write_files({path: lock.render() for path, lock in locks.items()})


def locks_digest(root: Path) -> str:
    digest = hashlib.sha256()
    paths = sorted((root / "packages").rglob("*.lock"))
    if not paths:
        raise LockError("no package locks found")
    for path in paths:
        content = path.read_text(encoding="utf-8")
        parse_lock(content)
        digest.update(content.encode("utf-8"))
    return digest.hexdigest()


def write_files(rendered: Mapping[Path, str]) -> None:
    # Stage every file before replacing any destination. Callers must resolve
    # and validate the whole input set before entering this function.
    staged: list[tuple[Path, Path]] = []
    try:
        for path, content in rendered.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".apk-lock-", delete=False) as temporary:
                staged.append((Path(temporary.name), path))
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary.name, 0o644)
        for temporary, path in staged:
            os.replace(temporary, path)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
