# Multi-flow MXL → GStreamer compositor → mediamtx.
#
# Built on top of libmxl-dev (debian trixie + libmxl headers + lib +
# build-essential) — that image already carries everything needed to
# link against libmxl. Adds gstreamer dev + runtime plugins on top.

ARG BASE_TAG=latest
FROM ghcr.io/qvest-digital/libmxl-dev:${BASE_TAG} AS build

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        cmake \
        libgstreamer1.0-dev \
        libgstreamer-plugins-base1.0-dev \
        libglib2.0-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY CMakeLists.txt /src/
COPY src /src/src/

RUN cmake -S /src -B /src/build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build /src/build --parallel \
 && cmake --install /src/build --prefix /opt/mxl-compositor

# Runtime: same libmxl-dev image. Smaller would be libmxl-runtime but it
# lacks the gst plugins. libmxl-dev already has libmxl.so so the binary
# links cleanly without copying libs around. Add gst runtime plugins
# (good/bad/ugly/rtsp) needed by the pipeline at runtime.
FROM ghcr.io/qvest-digital/libmxl-dev:${BASE_TAG}

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav \
        gstreamer1.0-rtsp \
        gstreamer1.0-x \
        libgstrtspserver-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/mxl-compositor/bin/mxl-multi-compositor /usr/bin/

ENTRYPOINT ["/usr/bin/mxl-multi-compositor"]
