# Debian trixie's own python3 (3.13.5-2+deb13u5 on 2026-10-31): docker build -t py104:debian -f debian.Dockerfile .
FROM debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4
RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
