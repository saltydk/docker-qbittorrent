# Container images for qBittorrent

The repository publishes three image variants for `linux/amd64` and
`linux/arm64`.

| Variant | qBittorrent line | libtorrent line | Moving tags |
| --- | --- | --- | --- |
| `libtorrent1` | Current | 1.2 | `latest`, `libtorrent1` |
| `libtorrent2` | Current | 2.0 | `libtorrent2` |
| `legacy` | 4.3.9 | 1.2 | `legacy` |

Every build also publishes `<release>` and `<release>-<upstream revision>`
tags. The revision is the upstream static-binary build revision. Base-image and
APK-only updates commit new package locks and republish the same versioned tag
and moving aliases without changing that upstream revision.

## Run

```shell
docker run -d \
  --name=qbittorrent \
  --net=host \
  -e PUID=1001 \
  -e PGID=1001 \
  -v /path/to/config:/config \
  -v /path/to/downloads:/downloads \
  --restart on-failure \
  --stop-timeout 300 \
  saltydk/qbittorrent:latest
```

## Build from source

Choose the Dockerfile matching the required variant:

```shell
docker buildx build --platform linux/amd64 -f Dockerfile.libtorrent1 .
docker buildx build --platform linux/arm64 -f Dockerfile.libtorrent2 .
docker buildx build --platform linux/amd64 -f Dockerfile.legacy .
```

Each Dockerfile pins the shared base image by its source-commit tag and manifest
digest, and verifies the qBittorrent binary checksum for both supported
architectures.

The package list in `packages/runtime/requested.txt` expresses the runtime
requirements. The generated `x86_64.lock` and `aarch64.lock` files record the
complete installed package set, including dependencies, at exact versions.
Builds consume these locks and verify the resulting inventory. Changing a lock
invalidates the package-install cache; an unchanged lock can reuse it.

The base image owns packages it already supplies. qBittorrent adds packages
without upgrading, downgrading, or removing inherited packages. If a dependency
needs a newer inherited package, update and publish the base first. The updater
then adopts the verified base and refreshes both architecture locks together.

If a scheduled or manual update encounters that inherited-package conflict, it
requests `saltydk/docker-alpine-s6overlay`'s shared `request-refresh.yml` workflow
and finishes successfully once GitHub accepts the request. The coordinator
reuses a queued or running package refresh, or starts one. The qBittorrent run
leaves its inputs and published images unchanged. The next scheduled update, every six hours,
retries against the published base; a manual `version-check` run can retry sooner.
The requested base refresh must pass its own build and publication checks.
Selecting `refresh-base` on a manual `version-check` run submits the same request
directly and defers the qBittorrent update. Autoscan uses the same coordinator.

Cross-repository dispatch uses the existing `GH_TOKEN` repository secret, which
must have Actions write access to `saltydk/docker-alpine-s6overlay`. Missing
credentials, a rejected coordinator dispatch, and other input-resolution errors
still fail the update. Failures inside the coordinator or base build remain
visible in their respective base-repository runs. The local `update` command
returns exit code 4 for a required base refresh; the workflow handles that result
and requests the refresh.

To refresh inputs in a local checkout with Docker and support for both target
architectures:

```shell
python3 scripts/manage_builds.py update --write --report /tmp/qbittorrent-update.json --summary /tmp/qbittorrent-update.md
python3 scripts/manage_builds.py verify
```

Review the resulting Dockerfile and package-lock diffs together. Do not edit
generated locks individually. An exact package that is no longer available
causes a clear build failure; refresh the inputs rather than weakening the pin.
This repository does not archive Alpine packages, so a historical checkout is
not guaranteed to rebuild indefinitely.

## Updates and security

The scheduled updater checks qBittorrent releases, upstream build revisions,
both architecture-specific binary digests, the base-image manifest, and
compatible additional Alpine packages. It commits all resolved inputs only after
both architectures succeed, then explicitly builds that exact commit. Builds
verify the tracked release, revision, and architecture checksums against the
referenced release assets, without requiring the release to remain the latest.
Failed publication can be retried without producing an empty input commit.

On direct push runs, an upstream release or build-revision mismatch skips image
builds and publication with a notice. Unit tests and lint still run. The scheduled
or manually run `version-check` workflow refreshes the inputs separately. PR,
manual build, and updater-called builds still reject mismatches; checksum,
package-lock, and upstream request failures remain errors on every trigger.

Update summaries show proposed package versions and image-input changes. Build
summaries compare the verified candidate inventory with the previous published
image captured by digest, and report tests, scans, and publication separately.
Complete JSON reports are retained as workflow artifacts. Images include their
runtime lock at `/usr/share/image-inputs/runtime.lock`; the image labels record
the package-lock aggregate hash and the variant's input hash.
Builds boot each variant, authenticate to its Web API, validate its version and
libtorrent line, and scan the resulting image before publishing. Trivy applies
the Alpine vendor advisory policy; Docker Scout reports broader advisory data
and blocks fixable HIGH/CRITICAL or CISA KEV findings. Renovate owns GitHub
Actions and scanner updates; the image updater exclusively owns the base source
tag and digest.

Scout runs through `.github/actions/scout`, which downloads its binary with the
job's GitHub token and verifies the published checksum before invoking the
upstream action. Its `uses: docker/scout-action@...` reference is the sole version
pin. Renovate updates that reference, and preparation derives both the binary
release and checksum filename from it. The binary matches the runner's
architecture, independently of the image being scanned. Failed preparation or
report generation skips dependent steps without bypassing the security gate.

The base repository's updater owns Alpine image digest and package-lock
refreshes within its selected release line. Its APK helper is shipped in the
base image at `/usr/local/libexec/apk-lock` and used by this repository. When
introducing this lock format, publish the base implementation first, then refresh
qBittorrent's base pin and locks before publishing qBittorrent.

## Build notifications

The `Notify` workflow reports completed `build` and `version-check` runs through
`saltyorg/github-actions/notify`. Each message includes the build reason and one
inline field per selected variant with its qBittorrent version, libtorrent
version, and upstream revision. Only changed values show an arrow. Base-image
changes link to the old and new source commits.

Build acceptance uploads `qbittorrent-notification-<run attempt>` containing
`notification.json`. The shared action validates its run ID and attempt before
using it. Successful checks with no build stay quiet; failed runs still notify
when details are unavailable. PR checks do not post to Discord. Build triggers
and scheduled publication recovery are unchanged.
