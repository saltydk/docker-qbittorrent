ARG OS_TAG=latest

# Runtime
FROM saltydk/alpine-s6overlay:${OS_TAG}

ARG QBITTORRENT_RELEASE="release-4.3.9_v1.2.14"

LABEL maintainer="salty"

ENV \
  HOME="/config" \
  XDG_CONFIG_HOME="/config" \
  XDG_DATA_HOME="/config" \
  # 3 minutes for services to exit
  S6_SERVICES_GRACETIME=180000 \
  S6_KILL_GRACETIME=3000

# Install packages
RUN \
  # Find arch for archive
  ARCH=$(uname -m) && \
  QBIT_ARCH="x86_64" && \
  echo "**** install packages ****" \
  && apk add --no-cache --upgrade \
    bind-tools \
    ca-certificates \
    curl \
    mediainfo \
    openssl \
    procps \
    tar \
    unrar \
    unzip \
    wget \
    zlib \
  && echo "**** install qbittorrent-nox (${QBIT_ARCH})****" \
  && curl -sSf -L -o /usr/bin/qbittorrent-nox \
    "https://github.com/userdocs/qbittorrent-nox-static/releases/download/${QBITTORRENT_RELEASE}/${QBIT_ARCH}-qbittorrent-nox" \
  && chmod 755 /usr/bin/qbittorrent-nox \
  && echo "**** cleanup ****" \
  && rm -rf \
    /root/.cache \
    /tmp/*

# Add root files
COPY ["root/", "/"]

# Volumes
VOLUME ["/config", "/downloads"]

# Expose
EXPOSE 8080/tcp
