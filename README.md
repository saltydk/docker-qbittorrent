# Container images for qBittorrent

```shell
docker run -d \
  --name=qb \
  --net=host \
  -e PUID=1001 \
  -e PGID=1001 \
  -v /path/to/config:/config \
  -v /path/to/downloads:/downloads \
  --restart on-failure \
  --stop-timeout 300 \
  sc4h/qbittorrent:latest
```
