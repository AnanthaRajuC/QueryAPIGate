# The official python:3.12-slim recipe (docker history of the pinned digest), with three switches:
#   SHARED=yes|no  --enable-shared (libpython3.12.so) or a statically linked interpreter
#   FP=yes|no      the official -fno-omit-frame-pointer -mno-omit-leaf-frame-pointer
#   DPKG_FLAGS=yes|no  the official dpkg-buildflags CFLAGS/LDFLAGS (-g -O2, stack protectors, -fcf-protection, relro)
#   docker build --build-arg SHARED=no --build-arg FP=no -t py104:cp-static-nofp -f cpython.Dockerfile .
FROM debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4
ARG SHARED=yes
ARG FP=yes
ARG DPKG_FLAGS=yes
ENV PYTHON_VERSION=3.12.15 PYTHON_SHA256=c2c4321961fab0fb999d66e0cecf521c2ab3994c7992873ea99e306c1094fd5a LANG=C.UTF-8
RUN set -eux; apt-get update; apt-get install -y --no-install-recommends ca-certificates dpkg-dev g++ gcc libbluetooth-dev \
      libbz2-dev libc6-dev libdb-dev libffi-dev libgdbm-dev liblzma-dev libncursesw5-dev libreadline-dev libsqlite3-dev \
      libssl-dev make tk-dev uuid-dev wget xz-utils zlib1g-dev; \
    wget -q -O python.tar.xz "https://www.python.org/ftp/python/$PYTHON_VERSION/Python-$PYTHON_VERSION.tar.xz"; \
    echo "$PYTHON_SHA256 *python.tar.xz" | sha256sum -c -; \
    mkdir -p /usr/src/python; tar -x -C /usr/src/python --strip-components=1 -f python.tar.xz; cd /usr/src/python; \
    gnuArch="$(dpkg-architecture --query DEB_BUILD_GNU_TYPE)"; \
    shared=; [ "$SHARED" = yes ] && shared=--enable-shared; \
    ./configure --build="$gnuArch" --enable-loadable-sqlite-extensions --enable-optimizations --enable-option-checking=fatal \
      $shared --with-lto --with-ensurepip; \
    EXTRA_CFLAGS=; LDFLAGS=-Wl,--strip-all; \
    [ "$DPKG_FLAGS" = yes ] && EXTRA_CFLAGS="$(dpkg-buildflags --get CFLAGS)" && LDFLAGS="$(dpkg-buildflags --get LDFLAGS) -Wl,--strip-all"; \
    [ "$FP" = yes ] && EXTRA_CFLAGS="$EXTRA_CFLAGS -fno-omit-frame-pointer -mno-omit-leaf-frame-pointer"; \
    make -j "$(nproc)" "EXTRA_CFLAGS=$EXTRA_CFLAGS" "LDFLAGS=$LDFLAGS"; \
    if [ "$SHARED" = yes ]; then rm python; make -j "$(nproc)" "EXTRA_CFLAGS=$EXTRA_CFLAGS" "LDFLAGS=$LDFLAGS -Wl,-rpath='\$\$ORIGIN/../lib'" python; fi; \
    make install; ldconfig; cd /; rm -rf /usr/src/python python.tar.xz; python3 -VV
