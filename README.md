# Container images for qBittorrent

The repository publishes three image variants for `linux/amd64` and
`linux/arm64`.

| Variant | qBittorrent line | libtorrent line | Moving tags |
| --- | --- | --- | --- |
| `libtorrent1` | Current | 1.2 | `latest`, `libtorrent1` |
| `libtorrent2` | Current | 2.0 | `libtorrent2` |
| `legacy` | 4.3.9 | 1.2 | `legacy` |

Every build also publishes `<release>` and `<release>-<image revision>` tags.
The exact revision tag changes whenever a binary, base image, or installed APK
package changes.

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

Each Dockerfile pins the shared base image by manifest digest and verifies the
qBittorrent binary checksum for both supported architectures.

## Updates and security

The scheduled updater checks qBittorrent releases, both architecture-specific
binary digests, the base-image manifest, and available Alpine package upgrades.
Builds boot each variant, authenticate to its Web API, validate its version and
libtorrent line, and scan the resulting image before publishing. Renovate owns
GitHub Actions and scanner updates; the image updater exclusively owns the base
digest so it can increment all affected image revisions together.
