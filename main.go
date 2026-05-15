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
  <h1>MXL DMF Demo App 🚀 for Justin</h1>
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
<title>MXL Multiviewer</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0a0a0a; color: #eee; padding: 12px; }
  h1 { text-align: center; margin-bottom: 12px; font-size: 1.4em; }
  .nav { text-align: center; margin-bottom: 12px; font-size: 0.85em; }
  a { color: #88bbff; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; max-width: 1400px; margin: 0 auto; }
  .tile { background: #111; border: 2px solid #222; border-radius: 6px; overflow: hidden; position: relative; }
  .tile.active { border-color: #4caf50; }
  .tile.error { border-color: #f44336; }
  .tile-header { display: flex; justify-content: space-between; align-items: center; padding: 6px 10px; background: #1a1a2e; font-size: 0.8em; }
  .tile-label { font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
  .tile-status { font-size: 0.75em; padding: 2px 8px; border-radius: 10px; }
  .tile-status.live { background: #4caf50; color: #000; }
  .tile-status.connecting { background: #ff9800; color: #000; }
  .tile-status.offline { background: #f44336; color: #fff; }
  .tile video { width: 100%; display: block; background: #000; aspect-ratio: 16/9; }
  .tile-metrics { display: flex; gap: 12px; padding: 6px 10px; background: #0d0d1a; font-size: 0.7em; color: #888; font-variant-numeric: tabular-nums; }
  .tile-metrics span { white-space: nowrap; }
  .tile-metrics .val { color: #ccc; font-weight: 500; }
  .log { max-width: 1400px; margin: 12px auto 0; background: #0a0a0a; border: 1px solid #222; border-radius: 6px; padding: 8px 12px; font-family: monospace; font-size: 0.7em; max-height: 150px; overflow-y: auto; }
  .log .entry { padding: 1px 0; color: #666; }
  .log .entry.err { color: #f44336; }
  .log .entry.ok { color: #4caf50; }
  .log .entry.info { color: #2196f3; }
  @media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
  <h1>📺 MXL Multiviewer</h1>
  <p class="nav"><a href="/">← Back to Demo App</a></p>

  <div class="grid" id="grid"></div>
  <div class="log" id="log"></div>

  <script>
    const sources = [
      { id: 'mxl-srt',    label: 'MXL Live (EFA → mxl-gst-sink → SRT)',   url: '/hls/mxl-stream/index.m3u8' },
      { id: 'mxl-native', label: 'MXL Live (EFA → mediamtx mxl:// PR#1)', url: '/hls/mxl-stream-native/index.m3u8' },
      { id: 'sintel',     label: 'SINTEL 24 (looped open movie)',         url: '/hls/sintel/index.m3u8' },
      { id: 'ball',       label: 'Bouncing Ball',                         url: '/hls/ball/index.m3u8' },
      { id: 'snow',       label: 'Snow / Noise',                          url: '/hls/snow/index.m3u8' },
    ];

    const grid = document.getElementById('grid');
    const logEl = document.getElementById('log');

    function log(msg, cls) {
      const d = new Date().toLocaleTimeString();
      const e = document.createElement('div');
      e.className = 'entry ' + (cls || '');
      e.textContent = d + '  ' + msg;
      logEl.prepend(e);
      if (logEl.children.length > 200) logEl.lastChild.remove();
    }

    function createTile(src) {
      const tile = document.createElement('div');
      tile.className = 'tile';
      tile.id = 'tile-' + src.id;
      tile.innerHTML =
        '<div class="tile-header">' +
          '<span class="tile-label">' + src.label + '</span>' +
          '<span class="tile-status connecting" id="st-' + src.id + '">CONNECTING</span>' +
        '</div>' +
        '<video id="v-' + src.id + '" muted autoplay playsinline></video>' +
        '<div class="tile-metrics">' +
          '<span>Res: <span class="val" id="res-' + src.id + '">—</span></span>' +
          '<span>Bitrate: <span class="val" id="bps-' + src.id + '">—</span></span>' +
          '<span>Buffer: <span class="val" id="buf-' + src.id + '">—</span></span>' +
          '<span>Segs: <span class="val" id="seg-' + src.id + '">0</span></span>' +
        '</div>';
      grid.appendChild(tile);
      return tile;
    }

    function initPlayer(src) {
      const tile = createTile(src);
      const video = document.getElementById('v-' + src.id);
      const stEl = document.getElementById('st-' + src.id);
      let segs = 0, lastBytes = 0, lastTime = 0;

      if (!Hls.isSupported()) return;

      const hls = new Hls({ liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 6, enableWorker: true });
      hls.loadSource(src.url);
      hls.attachMedia(video);

      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        stEl.textContent = 'LIVE';
        stEl.className = 'tile-status live';
        tile.className = 'tile active';
        log('[' + src.id + '] Stream connected', 'ok');
        video.play();
      });

      hls.on(Hls.Events.FRAG_LOADED, (e, data) => {
        segs++;
        document.getElementById('seg-' + src.id).textContent = segs;
        const bytes = data.frag.stats.total;
        const now = Date.now();
        if (lastTime > 0) {
          const mbps = ((bytes * 8) / ((now - lastTime) / 1000) / 1e6).toFixed(1);
          document.getElementById('bps-' + src.id).textContent = mbps + ' Mbps';
        }
        lastBytes = bytes;
        lastTime = now;
      });

      hls.on(Hls.Events.ERROR, (ev, data) => {
        if (data.fatal) {
          stEl.textContent = 'OFFLINE';
          stEl.className = 'tile-status offline';
          tile.className = 'tile error';
          log('[' + src.id + '] ' + data.details, 'err');
          setTimeout(() => {
            stEl.textContent = 'CONNECTING';
            stEl.className = 'tile-status connecting';
            tile.className = 'tile';
            hls.loadSource(src.url);
          }, 5000);
        }
      });

      setInterval(() => {
        if (video.videoWidth) {
          document.getElementById('res-' + src.id).textContent = video.videoWidth + '×' + video.videoHeight;
        }
        if (video.buffered.length > 0) {
          const buf = (video.buffered.end(video.buffered.length - 1) - video.currentTime).toFixed(1);
          document.getElementById('buf-' + src.id).textContent = buf + 's';
        }
      }, 1000);
    }

    sources.forEach(initPlayer);
    log('Multiviewer initialized with ' + sources.length + ' sources', 'info');
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
