# python-build-standalone, as uv installs it: docker build --build-arg PY=3.12 -t py104:pbs-3.12 -f standalone.Dockerfile .
FROM debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ARG PY=3.12
RUN UV_PYTHON_INSTALL_DIR=/opt/python uv python install "$PY" && ln -s "$(UV_PYTHON_INSTALL_DIR=/opt/python uv python find "$PY")" /usr/local/bin/python
