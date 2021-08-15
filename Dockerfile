ARG OS_TAG=latest

# Runtime
FROM saltydk/alpine-s6overlay:${OS_TAG}

ARG QBITTORRENT_VERSION="4.3.7"
ARG LIBTORRENT_VERSION="1.2.14"

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
  && echo "**** install qbittorrent-nox ****" \
  # Find arch for archive
  ARCH=$(uname -m) && \
  QBIT_ARCH="" && \
  [ "${ARCH}" = "x86_64" ] && QBIT_ARCH="x86_64" || true && \
  [ "${ARCH}" = "aarch64" ] && QBIT_ARCH="aarch64" || true && \
  [ "${ARCH}" = "armv7l" ] && QBIT_ARCH="armhf" || true \
  && curl -sSf -L -o /usr/bin/qbittorrent-nox \
    "https://github.com/userdocs/qbittorrent-nox-static/releases/download/release-${QBITTORRENT_VERSION}_v${LIBTORRENT_VERSION}/${QBIT_ARCH}-qbittorrent-nox" \
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
