# Multi-flow MXL → GStreamer compositor → mediamtx.
#
# Built on top of mxl-base because that image already carries libmxl,
# libmxl-dev, gstreamer (good plugins + libav + rtsp), and the v210
# capable plugins (videoconvert/videoscale/compositor) the pipeline
# needs. The published mxl-base image is rebuilt out of mxl-dmf-demo's
# CI on every push to qvest/feature/** branches.

ARG BASE_TAG=latest
FROM ghcr.io/qvest-digital/mxl-base:${BASE_TAG} AS build

USER 0:0
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        pkg-config \
        libgstreamer1.0-dev \
        libgstreamer-plugins-base1.0-dev \
        libgstreamer-plugins-bad1.0-dev \
        libglib2.0-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY CMakeLists.txt /src/
COPY src /src/src/

RUN cmake -S /src -B /src/build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build /src/build --parallel \
 && cmake --install /src/build --prefix /opt/mxl-compositor

FROM ghcr.io/qvest-digital/mxl-base:${BASE_TAG}

USER 0:0
# rtspclientsink lives in gst-plugins-bad's rtsp module; rtspserver in
# gst-plugins-rs. Pull the bad set (rtspclientsink is enough for pushing
# RTSP to mediamtx).
RUN apt-get update && apt-get install -y --no-install-recommends \
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
