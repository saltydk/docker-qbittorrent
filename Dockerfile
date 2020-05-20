ARG ALPINE_TAG=3.11
# Build
FROM alpine:${ALPINE_TAG} as build

ARG QBITTORRENT_BRANCH="release-4.2.5"
ARG LIBTORRENT_BRANCH="libtorrent-1_2_6"

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

COPY ["patches/", "/patches/"]

# Build libtorrent
RUN cd \
  && git clone --depth=1 -b "${LIBTORRENT_BRANCH}" https://github.com/arvidn/libtorrent.git \
  && _py3ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")') \
  && cd libtorrent \
  && echo "# Using libtorrent branch ${LIBTORRENT_BRANCH} - Commit $(git rev-parse --short HEAD) - Python ${_py3ver}" \
  && ./autotool.sh \
  && PYTHON=$(command -v python3) \
    ./configure \
    --prefix=/usr \
    --sysconfdir=/etc \
    --mandir=/usr/share/man \
    --localstatedir=/var \
    --enable-python-binding \
    --with-boost-system="boost_python${_py3ver}" \
    --with-boost-python="boost_python${_py3ver}" \
    --with-libiconv \
    --disable-debug \
    CXXFLAGS="-std=c++14 -O3" \
  && make -j$(nproc) \
  && make install-strip

# Build qBittorrent
RUN cd \
  && git clone --depth=1 -b "${QBITTORRENT_BRANCH}" https://github.com/qbittorrent/qBittorrent.git \
  && cd qBittorrent \
  # Apply patches
  && git apply /patches/qbittorrent_* \
  && echo "# Using qbittorrent branch ${QBITTORRENT_BRANCH} - Commit $(git rev-parse --short HEAD)" \
  && ./configure \
    --disable-gui \
    --disable-debug \
    CXXFLAGS="-std=c++14 -O3" \
  && make -j$(nproc) \
  && make install \
  && qbittorrent-nox --version

# Runtime
FROM sc4h/alpine-s6overlay:${ALPINE_TAG}

LABEL maintainer="horjulf"

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
COPY --from=build ["/usr/lib/libtorrent-rasterbar.so.10", "/usr/lib/"]

# Volumes
VOLUME ["/config", "/downloads"]
