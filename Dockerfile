ARG ALPINE_TAG=3.11
# Build
FROM alpine:${ALPINE_TAG} as build

ARG QBITTORRENT_BRANCH="release-4.2.1"
ARG LIBTORRENT_BRANCH="libtorrent-1_2_5"

RUN \
  apk add --update --no-cache \
    autoconf \
    automake \
    binutils \
    boost-dev \
    boost-python3 \
    build-base \
    cppunit-dev \
    git \
    libtool \
    linux-headers \
    ncurses-dev \
    openssl-dev \
    python3-dev \
    qt5-qtbase \
    qt5-qttools-dev \
    zlib-dev

# Build libtorrent
RUN cd \
  && git clone --depth=1 -b "${LIBTORRENT_BRANCH}" https://github.com/arvidn/libtorrent.git \
  && cd libtorrent \
  && echo "# Using libtorrent branch ${LIBTORRENT_BRANCH} - Commit $(git rev-parse --short HEAD)" \
  && ./autotool.sh \
  && ./configure \
    --with-libiconv \
    --enable-python-binding \
    --with-boost-python="$(ls -1 /usr/lib/libboost_python3*.so* | sort | head -1 | sed 's/.*.\/lib\(.*\)\.so.*/\1/')" \
    PYTHON="$(which python3)" \
  && make -j$(nproc) \
  && make install-strip

# Build qBittorrent
RUN cd \
  && git clone --depth=1 -b "${QBITTORRENT_BRANCH}" https://github.com/qbittorrent/qBittorrent.git \
  && cd qBittorrent \
  && echo "# Using qbittorrent branch ${QBITTORRENT_BRANCH} - Commit $(git rev-parse --short HEAD)" \
  && ./configure --disable-gui \
  && make -j$(nproc) \
  && make install \
  && qbittorrent-nox --version

# Runtime
FROM sc4h/alpine-s6overlay:${ALPINE_TAG}

LABEL maintainer="horjulf"

ENV \
  QBITTORRENT_HOME="/config" \
  # 3 minutes for finish scripts to run
  S6_KILL_FINISH_MAXTIME=180000 \
  S6_SERVICES_GRACETIME=5000 \
  S6_KILL_GRACETIME=5000

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
    qt5-qtbase \
    tar \
    unrar \
    unzip \
    wget \
    zlib \
  && echo "**** cleanup ****" \
  && rm -rf \
    /root/.cache \
    /tmp/*

# Add root files
COPY ["root/", "/"]

# Binaries
COPY --from=build ["/usr/local/bin/qbittorrent-nox", "/usr/bin/qbittorrent-nox"]
COPY --from=build ["/usr/local/lib/libtorrent-rasterbar.so.10.0.0", "/usr/lib/libtorrent-rasterbar.so.10"]

# Ports
EXPOSE 6881 6881/udp 8080

# Volumes
VOLUME ["/config", "/downloads"]
