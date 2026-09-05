#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 7 ]]; then
  echo "usage: $0 IMAGE PLATFORM ARCH VARIANT QBITTORRENT_VERSION LIBTORRENT_PREFIX CONFIG_PROFILE" >&2
  exit 2
fi

image=$1
platform=$2
expected_arch=$3
variant=$4
expected_version=$5
libtorrent_prefix=$6
config_profile=$7
container="qbt-acceptance-${variant}-${BASHPID}-${RANDOM}"

cleanup() {
  if docker inspect "$container" >/dev/null 2>&1; then
    docker logs "$container" >&2 || true
    docker rm --force --volumes "$container" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

docker run --detach \
  --name "$container" \
  --platform "$platform" \
  --env PUID=1000 \
  --env PGID=1000 \
  "$image" >/dev/null

password=""
for _ in $(seq 1 60); do
  logs=$(docker logs "$container" 2>&1 || true)
  password=$(printf '%s\n' "$logs" | sed -n 's/.*temporary password is provided for this session: //p' | tail -1)
  if docker exec "$container" curl -sS --output /dev/null http://127.0.0.1:8080/api/v2/app/version 2>/dev/null; then
    if [[ "$variant" == "legacy" || -n "$password" ]]; then
      break
    fi
  fi
  sleep 1
done

if [[ "$variant" == "legacy" ]]; then
  password=adminadmin
elif [[ -z "$password" ]]; then
  echo "qBittorrent did not emit a temporary WebUI password" >&2
  exit 1
fi

docker exec "$container" curl -fsS \
  --header 'Referer: http://127.0.0.1:8080' \
  --cookie-jar /tmp/qbittorrent-cookie \
  --data-urlencode 'username=admin' \
  --data-urlencode "password=$password" \
  http://127.0.0.1:8080/api/v2/auth/login >/dev/null

version=$(docker exec "$container" curl -fsS \
  --header 'Referer: http://127.0.0.1:8080' \
  --cookie /tmp/qbittorrent-cookie \
  http://127.0.0.1:8080/api/v2/app/version)
api_version=$(docker exec "$container" curl -fsS \
  --header 'Referer: http://127.0.0.1:8080' \
  --cookie /tmp/qbittorrent-cookie \
  http://127.0.0.1:8080/api/v2/app/webapiVersion)
build_info=$(docker exec "$container" curl -fsS \
  --header 'Referer: http://127.0.0.1:8080' \
  --cookie /tmp/qbittorrent-cookie \
  http://127.0.0.1:8080/api/v2/app/buildInfo)

[[ "$version" == "v${expected_version}" ]]
[[ "$api_version" =~ ^[0-9]+\.[0-9]+([.][0-9]+)?$ ]]
[[ "$(printf '%s' "$build_info" | jq -r .bitness)" == "64" ]]
[[ "$(printf '%s' "$build_info" | jq -r .libtorrent)" == "${libtorrent_prefix}"* ]]
[[ "$(docker exec "$container" uname -m)" == "$expected_arch" ]]

docker exec "$container" /bin/sh -ec '
  pid=$(pgrep -o -f /usr/bin/qbittorrent-nox)
  test -n "$pid"
  test "$(stat -c %u "/proc/$pid")" = 1000
'

config=/config/qBittorrent/qBittorrent.conf
if [[ "$config_profile" == "legacy" ]]; then
  docker exec "$container" grep -Fq 'Downloads\SavePath=' "$config"
  if docker exec "$container" grep -Fq 'Session\DefaultSavePath=' "$config"; then
    echo "legacy config contains a modern save-path key" >&2
    exit 1
  fi
else
  docker exec "$container" grep -Fq 'Session\DefaultSavePath=' "$config"
  if docker exec "$container" grep -Fq 'Downloads\SavePath=' "$config"; then
    echo "modern config contains a legacy save-path key" >&2
    exit 1
  fi
fi

docker stop --time 20 "$container" >/dev/null
[[ "$(docker inspect --format '{{.State.ExitCode}}' "$container")" == "0" ]]
docker rm --volumes "$container" >/dev/null
trap - EXIT
