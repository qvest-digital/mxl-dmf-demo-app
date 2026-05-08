package main

import (
	"fmt"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
)

var version = "dev"

func main() {
	hostname, _ := os.Hostname()

	mediamtxHLS := os.Getenv("MEDIAMTX_HLS_URL")
	if mediamtxHLS == "" {
		mediamtxHLS = "http://mediamtx:8888"
	}

	http.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprintln(w, "ok")
	})

	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprintf(w, `<!DOCTYPE html>
<html>
<head><title>MXL DMF Demo</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 600px; margin: 80px auto; text-align: center; }
  .version { color: #666; font-size: 0.9em; }
  .hostname { color: #888; font-size: 0.8em; }
  a { color: #0066cc; }
</style>
</head>
<body>
  <h1>MXL DMF Demo App 🚀 for Patrick</h1>
  <p>Deployed via Flux GitOps</p>
  <p class="version">Version: %s</p>
  <p class="hostname">Hostname: %s</p>
  <hr>
  <p><a href="/stream">📺 Live MXL Stream</a></p>
</body>
</html>`, version, hostname)
	})

	http.HandleFunc("/stream", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, `<!DOCTYPE html>
<html>
<head>
<title>MXL Live Stream</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; max-width: 1100px; margin: 30px auto; padding: 0 20px; background: #111; color: #eee; }
  h1 { text-align: center; margin-bottom: 8px; }
  .nav { text-align: center; margin-bottom: 16px; }
  a { color: #88bbff; }
  video { width: 100%; background: #000; border-radius: 8px; }
  .status-bar { text-align: center; color: #888; font-size: 0.85em; margin: 10px 0; }
  #error { color: #ff6666; display: none; text-align: center; }
  .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-top: 16px; }
  .metric { background: #1a1a2e; border: 1px solid #333; border-radius: 8px; padding: 14px; }
  .metric .label { font-size: 0.75em; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }
  .metric .value { font-size: 1.4em; font-weight: 600; margin-top: 4px; font-variant-numeric: tabular-nums; }
  .metric .value.good { color: #4caf50; }
  .metric .value.warn { color: #ff9800; }
  .metric .value.bad { color: #f44336; }
  .metric .value.neutral { color: #eee; }
  .log { margin-top: 16px; background: #0a0a0a; border: 1px solid #333; border-radius: 8px; padding: 12px; font-family: monospace; font-size: 0.8em; max-height: 200px; overflow-y: auto; }
  .log .entry { padding: 2px 0; color: #999; }
  .log .entry.err { color: #f44336; }
  .log .entry.ok { color: #4caf50; }
  .log .entry.info { color: #2196f3; }
</style>
</head>
<body>
  <h1>📺 MXL Live Stream</h1>
  <p class="nav"><a href="/">← Back to Demo App</a></p>
  <video id="video" controls autoplay muted></video>
  <p class="status-bar" id="status">Connecting to stream...</p>
  <p id="error">Stream unavailable. The MXL reader/writer pipeline may not be running.</p>

  <div class="metrics">
    <div class="metric"><div class="label">Stream State</div><div class="value neutral" id="m-state">—</div></div>
    <div class="metric"><div class="label">Resolution</div><div class="value neutral" id="m-resolution">—</div></div>
    <div class="metric"><div class="label">Bitrate</div><div class="value neutral" id="m-bitrate">—</div></div>
    <div class="metric"><div class="label">Buffered</div><div class="value neutral" id="m-buffer">—</div></div>
    <div class="metric"><div class="label">Latency</div><div class="value neutral" id="m-latency">—</div></div>
    <div class="metric"><div class="label">Dropped Frames</div><div class="value neutral" id="m-dropped">—</div></div>
    <div class="metric"><div class="label">Segments Loaded</div><div class="value neutral" id="m-segments">—</div></div>
    <div class="metric"><div class="label">Uptime</div><div class="value neutral" id="m-uptime">—</div></div>
  </div>

  <div class="log" id="log"></div>

  <script>
    const video = document.getElementById('video');
    const statusEl = document.getElementById('status');
    const errorEl = document.getElementById('error');
    const logEl = document.getElementById('log');
    const hlsUrl = '/hls/mxl-stream/index.m3u8';

    let streamStart = null;
    let segmentsLoaded = 0;
    let lastBytes = 0;
    let lastBytesTime = 0;
    let reconnects = 0;

    function log(msg, cls) {
      const d = new Date().toLocaleTimeString();
      const entry = document.createElement('div');
      entry.className = 'entry ' + (cls || '');
      entry.textContent = d + '  ' + msg;
      logEl.prepend(entry);
      if (logEl.children.length > 100) logEl.lastChild.remove();
    }

    function setMetric(id, value, cls) {
      const el = document.getElementById(id);
      el.textContent = value;
      el.className = 'value ' + (cls || 'neutral');
    }

    function fmtDuration(s) {
      const m = Math.floor(s / 60), sec = Math.floor(s % 60);
      return m + ':' + String(sec).padStart(2, '0');
    }

    function updateMetrics() {
      // Resolution
      if (video.videoWidth) {
        setMetric('m-resolution', video.videoWidth + '×' + video.videoHeight, 'good');
      }

      // Buffer
      if (video.buffered.length > 0) {
        const buf = video.buffered.end(video.buffered.length - 1) - video.currentTime;
        const cls = buf > 2 ? 'good' : buf > 0.5 ? 'warn' : 'bad';
        setMetric('m-buffer', buf.toFixed(1) + 's', cls);
      }

      // Dropped frames
      if (video.getVideoPlaybackQuality) {
        const q = video.getVideoPlaybackQuality();
        const cls = q.droppedVideoFrames === 0 ? 'good' : q.droppedVideoFrames < 10 ? 'warn' : 'bad';
        setMetric('m-dropped', q.droppedVideoFrames + ' / ' + q.totalVideoFrames, cls);
      }

      // Uptime
      if (streamStart) {
        setMetric('m-uptime', fmtDuration((Date.now() - streamStart) / 1000), 'good');
      }

      // Segments
      setMetric('m-segments', segmentsLoaded, segmentsLoaded > 0 ? 'good' : 'neutral');
    }

    if (Hls.isSupported()) {
      const hls = new Hls({
        liveSyncDurationCount: 3,
        liveMaxLatencyDurationCount: 6,
        enableWorker: true,
      });

      hls.loadSource(hlsUrl);
      hls.attachMedia(video);
      log('HLS.js initialized, loading source...', 'info');
      setMetric('m-state', 'Connecting', 'warn');

      hls.on(Hls.Events.MANIFEST_PARSED, (e, data) => {
        statusEl.textContent = 'Stream connected';
        streamStart = Date.now();
        setMetric('m-state', 'Live', 'good');
        log('Manifest parsed, ' + data.levels.length + ' quality level(s)', 'ok');
        video.play();
      });

      hls.on(Hls.Events.FRAG_LOADED, (e, data) => {
        segmentsLoaded++;
        const bytes = data.frag.stats.total;
        const now = Date.now();
        if (lastBytesTime > 0) {
          const dt = (now - lastBytesTime) / 1000;
          const bps = (bytes * 8) / dt;
          const mbps = (bps / 1e6).toFixed(2);
          const cls = bps > 5e6 ? 'good' : bps > 1e6 ? 'warn' : 'bad';
          setMetric('m-bitrate', mbps + ' Mbps', cls);
        }
        lastBytes = bytes;
        lastBytesTime = now;
      });

      hls.on(Hls.Events.LEVEL_SWITCHED, (e, data) => {
        const level = hls.levels[data.level];
        if (level) {
          log('Quality: ' + level.width + '×' + level.height + ' @ ' + Math.round(level.bitrate/1000) + ' kbps', 'info');
        }
      });

      hls.on(Hls.Events.ERROR, (event, data) => {
        log('Error: ' + data.type + ' / ' + data.details, 'err');
        if (data.fatal) {
          setMetric('m-state', 'Error', 'bad');
          streamStart = null;
          statusEl.style.display = 'none';
          errorEl.style.display = 'block';
          reconnects++;
          log('Fatal error, reconnecting (#' + reconnects + ') in 5s...', 'err');
          setTimeout(() => {
            errorEl.style.display = 'none';
            statusEl.style.display = 'block';
            statusEl.textContent = 'Reconnecting...';
            setMetric('m-state', 'Reconnecting', 'warn');
            hls.loadSource(hlsUrl);
          }, 5000);
        }
      });

      // Latency tracking
      setInterval(() => {
        if (hls.latency !== undefined && hls.latency > 0) {
          const cls = hls.latency < 3 ? 'good' : hls.latency < 8 ? 'warn' : 'bad';
          setMetric('m-latency', hls.latency.toFixed(1) + 's', cls);
        }
        updateMetrics();
      }, 1000);

    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = hlsUrl;
      log('Using native HLS (Safari)', 'info');
      video.addEventListener('loadedmetadata', () => {
        statusEl.textContent = 'Stream connected';
        streamStart = Date.now();
        video.play();
      });
    } else {
      statusEl.textContent = 'HLS not supported in this browser';
      log('Browser does not support HLS', 'err');
    }
  </script>
</body>
</html>`)
	})

	// Reverse proxy HLS from MediaMTX
	hlsTarget, _ := url.Parse(mediamtxHLS)
	hlsProxy := httputil.NewSingleHostReverseProxy(hlsTarget)
	hlsProxy.ModifyResponse = func(resp *http.Response) error {
		if loc := resp.Header.Get("Location"); loc != "" {
			resp.Header.Set("Location", "/hls"+loc)
		}
		return nil
	}
	http.HandleFunc("/hls/", func(w http.ResponseWriter, r *http.Request) {
		r.URL.Path = r.URL.Path[len("/hls"):]
		r.Host = hlsTarget.Host
		hlsProxy.ServeHTTP(w, r)
	})

	addr := ":8080"
	fmt.Printf("Listening on %s (version %s)\n", addr, version)
	if err := http.ListenAndServe(addr, nil); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}
