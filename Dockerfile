ARG OS_TAG=latest

# Runtime
FROM saltydk/alpine-s6overlay:${OS_TAG}

ARG QBITTORRENT_RELEASE="release-5.2.3_v2.0.14"
ARG QBITTORRENT_REVISION="0"
ARG QBITTORRENT_SHA256="7a93b9ae36b200aff25a074e167aff95cefc78b7d17a963620cd0d095bbbc0e5"

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
    unzip \
    wget \
    zlib

RUN \
  echo "**** install qbittorrent-nox ****" && \
  curl -sSf -L -o /usr/bin/qbittorrent-nox \
    "https://github.com/userdocs/qbittorrent-nox-static/releases/download/${QBITTORRENT_RELEASE}/x86_64-qbittorrent-nox" && \
  echo "${QBITTORRENT_SHA256}  /usr/bin/qbittorrent-nox" | sha256sum -c - && \
  chmod 755 /usr/bin/qbittorrent-nox && \
  echo "**** cleanup ****" && \
  rm -rf \
    /root/.cache \
    /tmp/*

# Add root files
COPY ["root/", "/"]

# Volumes
VOLUME ["/config", "/downloads"]

# Expose
EXPOSE 8080/tcp
