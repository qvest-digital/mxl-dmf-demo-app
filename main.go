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
  body { font-family: system-ui, sans-serif; max-width: 900px; margin: 40px auto; text-align: center; background: #111; color: #eee; }
  video { width: 100%; max-width: 854px; background: #000; border-radius: 8px; }
  a { color: #88bbff; }
  .status { color: #888; font-size: 0.85em; margin-top: 10px; }
  #error { color: #ff6666; display: none; }
</style>
</head>
<body>
  <h1>📺 MXL Live Stream</h1>
  <p><a href="/">← Back to Demo App</a></p>
  <video id="video" controls autoplay muted></video>
  <p class="status" id="status">Connecting to stream...</p>
  <p id="error">Stream unavailable. The MXL reader/writer pipeline may not be running.</p>
  <script>
    const video = document.getElementById('video');
    const status = document.getElementById('status');
    const error = document.getElementById('error');
    const hlsUrl = '/hls/mxl-stream/index.m3u8';

    if (Hls.isSupported()) {
      const hls = new Hls({
        liveSyncDurationCount: 3,
        liveMaxLatencyDurationCount: 6,
        enableWorker: true,
      });
      hls.loadSource(hlsUrl);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        status.textContent = 'Stream connected';
        video.play();
      });
      hls.on(Hls.Events.ERROR, (event, data) => {
        if (data.fatal) {
          status.style.display = 'none';
          error.style.display = 'block';
          setTimeout(() => {
            error.style.display = 'none';
            status.style.display = 'block';
            status.textContent = 'Reconnecting...';
            hls.loadSource(hlsUrl);
          }, 5000);
        }
      });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = hlsUrl;
      video.addEventListener('loadedmetadata', () => {
        status.textContent = 'Stream connected';
        video.play();
      });
    } else {
      status.textContent = 'HLS not supported in this browser';
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
