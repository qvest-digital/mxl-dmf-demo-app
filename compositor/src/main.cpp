// mxl-multi-compositor
//
// Reads N MXL flows directly from /dev/shm via libmxl (zero-copy, no
// per-flow decode pass), lays them out in a single GStreamer compositor
// element, encodes the mosaic once with x264, and pushes the result to
// mediamtx via RTSP. Bypasses mediamtx's per-flow encoder backlog so the
// wall-clocks burned in by the writers (mxl-gst-testsrc's clockoverlay
// element) actually agree across tiles at composite time.
//
// Flow list comes from the MXL_FLOW_IDS env var (space-separated UUIDs).
// Output destination from MXL_COMPOSITE_OUT (default rtsp://mediamtx:8554/composite).
// MXL domain from MXL_DOMAIN (default /domain).
//
// Each flow runs a worker thread that drives a dedicated appsrc with v210
// grains. The compositor element does the layout in I420 space; encoding
// happens once at the output, not per-flow.

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <gst/app/gstappsrc.h>
#include <gst/gst.h>

#include <mxl/flow.h>
#include <mxl/mxl.h>
#include <mxl/time.h>

namespace
{
    std::atomic<bool> g_exit{false};

    void on_signal(int) { g_exit.store(true, std::memory_order_relaxed); }

    std::string env_or(char const* key, char const* fallback)
    {
        char const* v = std::getenv(key);
        return v ? std::string{v} : std::string{fallback};
    }

    std::vector<std::string> split_ws(std::string const& s)
    {
        std::vector<std::string> out;
        std::istringstream is{s};
        std::string tok;
        while (is >> tok) out.push_back(tok);
        return out;
    }

    // Re-implementation of the Cursor pattern from mxl-gst-sink. Keeps each
    // reader pinned to the live grain index for its flow's rate; next()
    // sleeps until the next grain's delivery deadline so workers don't busy-
    // poll. readDelay=0 because the compositor wants the freshest frame.
    struct Cursor
    {
        mxlRational rate;
        std::uint32_t windowSize;
        std::int64_t readDelayGrains;
        std::uint64_t currentIndex;
        std::uint64_t requestedIndex;
        std::uint64_t deliveryDeadline;

        Cursor(mxlRational r, std::uint32_t w, std::int64_t readDelayNs)
            : rate{r}
            , windowSize{w}
            , readDelayGrains{((::mxlTimestampToIndex(&r, readDelayNs) + w - 1) / w) * w}
        {
            realign(::mxlGetTime());
        }

        void realign(std::uint64_t now)
        {
            currentIndex = ((::mxlTimestampToIndex(&rate, now) + (windowSize / 2)) / windowSize) * windowSize;
            requestedIndex = currentIndex - readDelayGrains;
            deliveryDeadline = ::mxlIndexToTimestamp(&rate, currentIndex + windowSize);
        }

        void next()
        {
            ::mxlSleepUntil(deliveryDeadline);
            currentIndex += windowSize;
            requestedIndex += windowSize;
            deliveryDeadline = ::mxlIndexToTimestamp(&rate, currentIndex + windowSize);
        }
    };

    struct FlowWorker
    {
        std::size_t index;
        std::string flowId;
        std::string domain;
        ::mxlInstance instance{nullptr};
        ::mxlFlowReader reader{nullptr};
        ::mxlFlowConfigInfo config{};
        GstElement* appsrc{nullptr};
        std::thread thread;
        std::atomic<std::uint64_t> framesPushed{0};
        std::atomic<std::uint64_t> framesMissed{0};
    };

    void worker_loop(FlowWorker* w)
    {
        auto rate = w->config.common.grainRate;
        Cursor cursor{rate, 1U, 0};

        g_print("[%zu] %s starting at rate %d/%d\n",
            w->index, w->flowId.c_str(),
            rate.numerator, rate.denominator);

        // Push the appsrc caps once before the first buffer. The capsfilter
        // downstream in the pipeline asserts the format, but appsrc needs an
        // explicit set so videoconvert can negotiate without a stall.
        auto* caps = ::gst_caps_new_simple("video/x-raw",
            "format", G_TYPE_STRING, "v210",
            "width", G_TYPE_INT, 1920,
            "height", G_TYPE_INT, 1080,
            "framerate", GST_TYPE_FRACTION, rate.numerator, rate.denominator,
            nullptr);
        ::g_object_set(G_OBJECT(w->appsrc), "caps", caps, nullptr);
        ::gst_caps_unref(caps);

        while (!g_exit.load(std::memory_order_relaxed))
        {
            ::mxlGrainInfo info{};
            std::uint8_t* payload = nullptr;

            // 100 ms grain wait covers any one-off jitter; longer waits trip
            // the supervisor / pipeline live latency.
            auto ret = ::mxlFlowReaderGetGrain(w->reader, cursor.requestedIndex, 100'000'000ULL, &info, &payload);
            if (ret != MXL_STATUS_OK)
            {
                w->framesMissed.fetch_add(1, std::memory_order_relaxed);
                if (ret == MXL_ERR_FLOW_INVALID)
                {
                    // Flow disappeared (writer restarted, RDMA pair reset).
                    // Realign to current head and try again next tick.
                    cursor.realign(::mxlGetTime());
                }
                // Tiny backoff so a dead flow doesn't burn CPU.
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
                continue;
            }

            if (info.validSlices < info.totalSlices || (info.flags & MXL_GRAIN_FLAG_INVALID) != 0)
            {
                cursor.next();
                continue;
            }

            // Zero-copy would mean handing the payload pointer to gst with a
            // custom GstAllocator that knows libmxl's ring buffer lifetime;
            // copy is sufficient for v1 — payload at 1080p v210 is ~5 MB and
            // gst's slab allocator absorbs it cheaply.
            auto* buf = ::gst_buffer_new_allocate(nullptr, info.grainSize, nullptr);
            GstMapInfo map{};
            ::gst_buffer_map(buf, &map, GST_MAP_WRITE);
            std::memcpy(map.data, payload, info.grainSize);
            ::gst_buffer_unmap(buf, &map);

            // Don't set PTS — appsrc has do-timestamp=true so gst stamps
            // each buffer with the pipeline running clock on push. mxl's
            // timestamps are a monotonic ns counter from a different
            // epoch, and compositor (which uses pipeline running-time as
            // its reference) would queue everything as "future" and never
            // mix it onto its canvas. Keep duration so the compositor can
            // estimate cadence.
            GST_BUFFER_DURATION(buf) =
                ::gst_util_uint64_scale_int(GST_SECOND, rate.denominator, rate.numerator);

            GstFlowReturn pushRet = GST_FLOW_OK;
            ::g_signal_emit_by_name(w->appsrc, "push-buffer", buf, &pushRet);
            ::gst_buffer_unref(buf);

            if (pushRet != GST_FLOW_OK && pushRet != GST_FLOW_FLUSHING)
            {
                g_printerr("[%zu] push-buffer returned %d, exiting worker\n",
                    w->index, static_cast<int>(pushRet));
                break;
            }

            w->framesPushed.fetch_add(1, std::memory_order_relaxed);
            cursor.next();
        }

        g_print("[%zu] %s worker exiting (pushed=%llu missed=%llu)\n",
            w->index, w->flowId.c_str(),
            static_cast<unsigned long long>(w->framesPushed.load()),
            static_cast<unsigned long long>(w->framesMissed.load()));
    }
}

int main(int argc, char** argv)
{
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);

    ::gst_init(&argc, &argv);

    auto domain = env_or("MXL_DOMAIN", "/domain");
    auto outUrl = env_or("MXL_COMPOSITE_OUT", "rtsp://mediamtx:8554/composite");
    auto flowIdsStr = env_or("MXL_FLOW_IDS", "");
    auto flowIds = split_ws(flowIdsStr);

    if (flowIds.empty())
    {
        g_printerr("MXL_FLOW_IDS empty — set it to a space-separated list of UUIDs\n");
        return 2;
    }
    if (flowIds.size() > 9)
    {
        g_printerr("Only the first 9 flows fit a 3x3 grid; ignoring overflow\n");
        flowIds.resize(9);
    }

    // Output is 1280x720 with 3x3 of 426x238 tiles. 1280/3 != 426*3 exactly
    // (1278 vs 1280) — a 1-pixel right/bottom black band is fine for a demo.
    constexpr int OUT_W = 1280;
    constexpr int OUT_H = 720;
    constexpr int TILE_W = 426;
    constexpr int TILE_H = 238;

    // Build the pipeline as a single gst-launch-style description. compositor
    // has per-sink xpos/ypos/width/height set via pad properties; we write
    // them inline here so the result is one parse_launch call (no manual pad
    // linking).
    // Bounded leaky queues are mandatory at every fan-in/fan-out point.
    // Without them gst's default unbounded queueing piles up buffers when
    // any downstream element stalls (rtspclientsink handshake, encoder
    // catch-up after pipeline state change) and the pod OOM-kills inside
    // seconds — what was killing the 3 Gi / 8 Gi attempts.
    std::string pipelineDesc =
        "compositor name=comp background=black ";
    for (std::size_t i = 0; i < flowIds.size(); ++i)
    {
        int row = static_cast<int>(i) / 3;
        int col = static_cast<int>(i) % 3;
        pipelineDesc += "sink_" + std::to_string(i) +
            "::xpos=" + std::to_string(col * TILE_W) +
            " sink_" + std::to_string(i) +
            "::ypos=" + std::to_string(row * TILE_H) +
            " sink_" + std::to_string(i) +
            "::width=" + std::to_string(TILE_W) +
            " sink_" + std::to_string(i) +
            "::height=" + std::to_string(TILE_H) + " ";
    }
    pipelineDesc +=
        "! video/x-raw,format=I420,width=" + std::to_string(OUT_W) +
        ",height=" + std::to_string(OUT_H) +
        ",framerate=30000/1001 "
        // Drop-old leaky queue right after the compositor so a brief
        // encoder hiccup doesn't backpropagate to the readers.
        "! queue leaky=downstream max-size-buffers=4 max-size-bytes=0 max-size-time=0 "
        "! videoconvert "
        // Single composite-time clock for the title bar — useful as a global
        // reference vs the per-tile clocks burned in at the writer (they
        // should agree to within microseconds after this refactor).
        "! clockoverlay font-desc=\"Sans Bold 20\" "
            "time-format=\"%H:%M:%S\" "
            "halignment=right valignment=top color=0xff00ff66 "
        "! x264enc speed-preset=ultrafast tune=zerolatency bframes=0 key-int-max=30 "
        "! video/x-h264,profile=baseline "
        // RTSP carries codec frames over RTP, not MPEG-TS. h264parse with
        // config-interval=-1 republishes SPS/PPS on every IDR so a viewer
        // joining mid-stream can decode without waiting for the next key
        // frame.
        "! h264parse config-interval=-1 "
        // Drop-old leaky queue right before the RTSP sink: if mediamtx
        // momentarily stalls accepting RTP, throw frames away rather
        // than queue them indefinitely.
        "! queue leaky=downstream max-size-buffers=8 max-size-bytes=0 max-size-time=0 "
        "! rtspclientsink protocols=tcp location=\"" + outUrl + "\" ";

    for (std::size_t i = 0; i < flowIds.size(); ++i)
    {
        pipelineDesc +=
            "appsrc name=src" + std::to_string(i) +
            " is-live=true format=time do-timestamp=true "
            " max-bytes=20971520 block=true "
            // Per-tile leaky queue absorbs short scheduler jitter without
            // unbounded growth. 3 buffers ≈ 100 ms at 30 fps.
            "! queue leaky=downstream max-size-buffers=3 max-size-bytes=0 max-size-time=0 "
            "! videoconvert "
            "! videoscale "
            "! video/x-raw,format=I420,width=" + std::to_string(TILE_W) +
            ",height=" + std::to_string(TILE_H) + " "
            "! comp.sink_" + std::to_string(i) + " ";
    }

    g_print("Pipeline: %s\n", pipelineDesc.c_str());

    GError* parseErr = nullptr;
    auto* pipeline = ::gst_parse_launch(pipelineDesc.c_str(), &parseErr);
    if (pipeline == nullptr)
    {
        g_printerr("gst_parse_launch failed: %s\n", parseErr ? parseErr->message : "unknown");
        if (parseErr) ::g_error_free(parseErr);
        return 1;
    }

    // Open every MXL flow before going live so any missing flow fails fast
    // (better than letting the pipeline run with one tile black forever).
    std::vector<FlowWorker> workers(flowIds.size());
    for (std::size_t i = 0; i < flowIds.size(); ++i)
    {
        auto& w = workers[i];
        w.index = i;
        w.flowId = flowIds[i];
        w.domain = domain;

        w.instance = ::mxlCreateInstance(domain.c_str(), "");
        if (w.instance == nullptr)
        {
            g_printerr("mxlCreateInstance failed for flow %s\n", w.flowId.c_str());
            return 3;
        }

        if (auto ret = ::mxlCreateFlowReader(w.instance, w.flowId.c_str(), "", &w.reader);
            ret != MXL_STATUS_OK)
        {
            g_printerr("mxlCreateFlowReader %s failed: %d\n", w.flowId.c_str(), static_cast<int>(ret));
            return 4;
        }

        if (auto ret = ::mxlFlowReaderGetConfigInfo(w.reader, &w.config);
            ret != MXL_STATUS_OK)
        {
            g_printerr("mxlFlowReaderGetConfigInfo %s failed: %d\n", w.flowId.c_str(), static_cast<int>(ret));
            return 5;
        }

        auto srcName = std::string{"src"} + std::to_string(i);
        w.appsrc = ::gst_bin_get_by_name(GST_BIN(pipeline), srcName.c_str());
        if (w.appsrc == nullptr)
        {
            g_printerr("appsrc %s not found in pipeline\n", srcName.c_str());
            return 6;
        }
    }

    if (::gst_element_set_state(pipeline, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE)
    {
        g_printerr("Failed to set pipeline PLAYING\n");
        return 7;
    }

    for (auto& w : workers)
    {
        w.thread = std::thread{worker_loop, &w};
    }

    // Diagnostic ticker: every 5s, report per-flow push/miss counters.
    // Without this it's impossible to tell whether the pipeline is silent
    // because workers aren't getting grains, or because grains are being
    // pushed but dropped downstream.
    std::thread stats{[&workers]() {
        std::uint64_t last[16] = {0};
        while (!g_exit.load(std::memory_order_relaxed))
        {
            std::this_thread::sleep_for(std::chrono::seconds(5));
            std::string line = "STATS";
            for (std::size_t i = 0; i < workers.size(); ++i)
            {
                auto cur = workers[i].framesPushed.load();
                auto delta = cur - last[i];
                last[i] = cur;
                line += " [" + std::to_string(i) + "]=" + std::to_string(delta) + "fps";
            }
            g_print("%s\n", line.c_str());
        }
    }};

    // Block on bus until ERROR / EOS / SIGTERM.
    auto* bus = ::gst_element_get_bus(pipeline);
    while (!g_exit.load(std::memory_order_relaxed))
    {
        auto* msg = ::gst_bus_timed_pop_filtered(bus, 500 * GST_MSECOND,
            static_cast<GstMessageType>(GST_MESSAGE_ERROR | GST_MESSAGE_EOS | GST_MESSAGE_WARNING));
        if (msg == nullptr) continue;
        switch (GST_MESSAGE_TYPE(msg))
        {
            case GST_MESSAGE_ERROR:
            {
                GError* err = nullptr;
                gchar* dbg = nullptr;
                ::gst_message_parse_error(msg, &err, &dbg);
                g_printerr("BUS ERROR %s: %s\n", err ? err->message : "?", dbg ? dbg : "");
                if (err) ::g_error_free(err);
                ::g_free(dbg);
                g_exit.store(true);
                break;
            }
            case GST_MESSAGE_EOS:
                g_print("BUS EOS\n");
                g_exit.store(true);
                break;
            case GST_MESSAGE_WARNING:
            {
                GError* err = nullptr;
                gchar* dbg = nullptr;
                ::gst_message_parse_warning(msg, &err, &dbg);
                g_print("BUS WARN %s: %s\n", err ? err->message : "?", dbg ? dbg : "");
                if (err) ::g_error_free(err);
                ::g_free(dbg);
                break;
            }
            default: break;
        }
        ::gst_message_unref(msg);
    }
    ::gst_object_unref(bus);

    g_print("Shutting down workers...\n");
    if (stats.joinable()) stats.join();
    for (auto& w : workers)
    {
        if (w.thread.joinable()) w.thread.join();
    }

    ::gst_element_set_state(pipeline, GST_STATE_NULL);
    ::gst_object_unref(pipeline);

    for (auto& w : workers)
    {
        if (w.appsrc) ::gst_object_unref(w.appsrc);
        if (w.reader) ::mxlReleaseFlowReader(w.instance, w.reader);
        if (w.instance) ::mxlDestroyInstance(w.instance);
    }

    return 0;
}
