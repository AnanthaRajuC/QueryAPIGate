# ClusterFuzzLite's build image (https://google.github.io/clusterfuzzlite/) - by digest, so a rebuild uses the same
# bytes (ADR 0018); Dependabot proposes each new one.
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:21f6fab17ace4dc76e1c56de91ee95d3d03da021df94b41056ba036f6c6f55e9
COPY . $SRC/queryapigate
WORKDIR $SRC/queryapigate
COPY .clusterfuzzlite/build.sh $SRC/
