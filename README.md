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
APK-only rebuilds republish the same versioned tag and moving aliases without
changing that upstream revision.

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

## Updates and security

The scheduled updater checks qBittorrent releases, upstream build revisions,
both architecture-specific binary digests, the base-image manifest, and
available Alpine package upgrades. Tracked input changes are committed; an
APK-only update dispatches a rebuild without changing the upstream revision.
Every build verifies the tracked release, revision, and architecture checksums
against upstream metadata before it can publish.
Builds boot each variant, authenticate to its Web API, validate its version and
libtorrent line, and scan the resulting image before publishing. Trivy applies
the Alpine vendor advisory policy; Docker Scout reports broader advisory data
and blocks fixable HIGH/CRITICAL or CISA KEV findings. Renovate owns GitHub
Actions and scanner updates; the image updater exclusively owns the base source
tag and digest.
