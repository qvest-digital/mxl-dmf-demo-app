package main

import (
	"fmt"
	"net/http"
	"os"
)

var version = "dev"

func main() {
	hostname, _ := os.Hostname()

	http.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprintln(w, "ok")
	})

	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprintf(w, `<!DOCTYPE html>
<html>
<head><title>MXL DMF Demo</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 600px; margin: 80px auto; text-align: center; }
  .version { color: #666; font-size: 0.9em; }
  .hostname { color: #888; font-size: 0.8em; }
</style>
</head>
<body>
  <h1>MXL DMF Demo App 🚀 for Patrick</h1>
  <p>Deployed via Flux GitOps</p>
  <p class="version">Version: %s</p>
  <p class="hostname">Hostname: %s</p>
</body>
</html>`, version, hostname)
	})

	addr := ":8080"
	fmt.Printf("Listening on %s (version %s)\n", addr, version)
	if err := http.ListenAndServe(addr, nil); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}
