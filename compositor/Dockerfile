# Multi-flow MXL → GStreamer compositor → mediamtx.
#
# Built on the published mxl-base image (debian trixie + libmxl.so + the
# mxl-* CLI tools). mxl-base does NOT ship libmxl headers — we vendor the
# small set we need under compositor/vendor/mxl/ so the build does not
# depend on a libmxl-dev image existing in GHCR (which it currently
# doesn't, because the upstream CI only pushes :latest on DEPLOY=true).

ARG BASE_TAG=latest
FROM ghcr.io/qvest-digital/mxl-base:${BASE_TAG} AS build

USER 0:0
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        pkg-config \
        libgstreamer1.0-dev \
        libgstreamer-plugins-base1.0-dev \
        libglib2.0-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY CMakeLists.txt /src/
COPY vendor /src/vendor/
COPY src /src/src/

RUN cmake -S /src -B /src/build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build /src/build --parallel \
 && cmake --install /src/build --prefix /opt/mxl-compositor

FROM ghcr.io/qvest-digital/mxl-base:${BASE_TAG}

USER 0:0
ENV DEBIAN_FRONTEND=noninteractive
# Runtime plugins needed by the pipeline: compositor (good), videoconvert /
# videoscale (base — already in mxl-base), x264enc (ugly), mpegtsmux (bad),
# rtspclientsink (bad / rs-rtsp). libav for hardened muxer support.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav \
        gstreamer1.0-rtsp \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/mxl-compositor/bin/mxl-multi-compositor /usr/bin/

USER mxl
WORKDIR /home/mxl
ENTRYPOINT ["/usr/bin/mxl-multi-compositor"]
