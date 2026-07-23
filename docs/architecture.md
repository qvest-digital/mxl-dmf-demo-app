# Architecture

> **Deep-dive companion to [`../README.md`](../README.md).** That file gives the 60-second story; this one explains every component and the design constraints that hold them together.

The signal path in one line: **writer pods → MXL domain (tmpfs) → mxl-k8s gateway/agent RDMA bridge → compositor (zero-copy, x264) → mediamtx (RTSP/WHEP/HLS) → Caddy → browser multiviewer**.

---

## 1. Writers

Four writer Deployments (`writer-mxl-1` through `writer-mxl-4`) each produce exactly one v210 uncompressed 720p test-pattern flow. Each Deployment runs a single replica with `strategy: Recreate`, which avoids two writers racing for the same flow slot on restart.

**Flow identity and pattern mapping:**

| Deployment | Flow UUID | Pattern (`MXL_FLOW_PATTERN`) | Overlay |
|---|---|---|---|
| writer-mxl-1 | `d4d00000-…-000000000001` | smpte | MXL-1 |
| writer-mxl-2 | `d4d00000-…-000000000002` | ball | MXL-2 |
| writer-mxl-3 | `d4d00000-…-000000000003` | gamut | MXL-3 |
| writer-mxl-4 | `d4d00000-…-000000000004` | checkers-8 | MXL-4 |

The last hex digit of the UUID is the tile index (`n`); this is referred to informally as the `d4d0…000n` scheme.

**Placement and RDMA vs local:** Writer placement is left to the scheduler (`nodeSelector` is absent). When a writer lands on the same node as the compositor, its flow is read locally from `/run/mxl/domain` with no gateway mirror. When it lands on a different node, the mxl-k8s gateway bridges the flow grains across the EFA fabric (see §3 below). The per-flow metrics panel exposes `sourceNode` and `provider` so the local-vs-RDMA distinction is always visible.

An init container (`prepare-domain`) clears stale flow directories before each writer start, preventing the new writer from attaching to leftover ring-buffer state.

_Source: `k8s/writer-deployment.yaml`_

---

## 2. mxl-k8s Control Plane

The mxl-k8s control plane is what makes a cross-node write look local to the consumer. It is deployed independently of this demo app (as DaemonSets in `mxl-system`) but is a hard dependency. There are three moving parts:

**Gateway DaemonSet:** Runs on every node. Watches `/run/mxl/domain` on writer nodes via fanotify; when it sees a new `MxlFlow` CR, it reads the raw grain data and bridges it across the EFA fabric via libmxl-fabrics (the EFA provider). The gateway does not need to know in advance which consumer nodes need the flow — it is signalled by the agent.

**Agent DaemonSet:** Runs on every node. On the compositor's node, it receives bridged grains from the gateway and materialises the mirror at `/run/mxl/domain`, making the flow look local. It also manages the lifecycle of `MxlReceiver` and `MxlFlowMirror` CRs: when the intent-shim reports that a pod wants to open a flow, the agent creates the `MxlReceiver`; the operator reconciles that into a `MxlFlowMirror`; and the gateway starts bridging.

**Intent shim (`libmxl-intent.so`):** An init container (`install-intent-shim`) drops the shim into a shared `emptyDir`. The main container loads it via `LD_PRELOAD`. The shim intercepts the first `mxlCreateFlowReader` call and blocks it until the agent signals that the mirror is locally available, preventing `FLOW_NOT_FOUND` races on startup or pod reschedule. The shim communicates with the agent over a Unix socket mounted from the host at `/run/mxl/agent.sock`.

**Custom resources:** Three CRs are used by the operator:
- `MxlFlow` — cluster-scoped; one per UUID; records origin location and `OriginFresh` health condition.
- `MxlReceiver` — namespace-scoped; created by the agent when a consumer opens a flow; references the bound mirror.
- `MxlFlowMirror` — namespace-scoped; one per consumer-node per flow; tracks `phase`, `sourceNode`, and `provider`.

The compositor pod mounts `/run/mxl/domain` as a `hostPath` (same path as the agent and gateway DaemonSets), so reading from domain is identical whether the flow is local or RDMA-mirrored.

_Source: `k8s/composite-deployment.yaml`_

---

## 3. Compositor

The compositor is a C++/GStreamer process. It reads all configured flows from the MXL domain zero-copy via libmxl, composites them into a mosaic, encodes the mosaic once with x264, and pushes the result to mediamtx via RTSP.

**Environment contract:**

| Variable | Purpose |
|---|---|
| `MXL_FLOW_IDS` | Space-separated UUIDs of all flows to read (one per tile) |
| `MXL_DOMAIN` | Path to the MXL domain, default `/domain` |
| `MXL_COMPOSITE_OUT` | RTSP destination, e.g. `rtsp://mediamtx:8554/composite` |
| `MXL_FRAME_WIDTH` | Tile width in pixels (must match the flow definition) |
| `MXL_FRAME_HEIGHT` | Tile height in pixels (must match the flow definition) |
| `MXL_STATS_PORT` | Port for the `/stats.json` HTTP server (set to `9090`) |

**Pipeline structure:** Each flow runs a dedicated `appsrc` worker thread. Workers read the freshest complete grain from the MXL ring buffer at the flow's native grain rate (the compositor paces each worker with a monotonic deadline so it does not busy-poll). The GStreamer `compositor` element receives raw v210 frames from all appsrcs, lays them out in I420 space at their native tile size, and hands the composed frame to a single x264 encoder — no per-flow decode pass. The encoded bitstream goes to `rtspclientsink` via TCP.

**Mosaic geometry:** The grid is computed at startup once the flow count is known:
- `cols = ceil(sqrt(n))`
- `rows = ceil(n / cols)`

Four flows → 2 × 2; nine flows → 3 × 3. The output canvas is `cols × TILE_W` by `rows × TILE_H` (native tile dimensions, no downscaling).

**`/stats.json` server:** A minimal raw-socket HTTP/1.0 server runs on `MXL_STATS_PORT`. It serves a JSON object with per-flow `fps`, `pushed`, `missed`, `mbps`, and `live` fields, plus the global `cols`, `rows`, `outW`, `outH`, and `grainBytes`. The Caddy sidecar proxies requests to `/stats.json` from the browser.

**Capabilities:** The compositor needs `IPC_LOCK` to mlock tmpfs grain pages via the `ibv_reg_mr` path; without it the verbs path fails. `SYS_RESOURCE` is also added to raise the locked-memory rlimit.

_Source: `compositor/src/main.cpp`, `k8s/composite-deployment.yaml`_

---

## 4. mediamtx and Caddy

**mediamtx** is the Qvest fork (`ghcr.io/qvest-digital/mediamtx-mxl`) from the `feature/mxl-static-source` branch — one of the two load-bearing qvest deltas (see §9). It runs as the primary container in the mediamtx Deployment.

**RTSP ingest:** The compositor pushes `rtsp://mediamtx:8554/composite` to a publisher-mode path. The four MXL tiles are configured as static-source paths (`mxl:///run/mxl/domain/d4d00000-…-00000000000n`), which mediamtx reads directly from the local domain using the MXL static-source plugin — the same zero-copy mechanism the compositor uses.

**HLS:** Standard MPEG-TS HLS (not LL-HLS), `hlsAlwaysRemux: true`, 1-second segments, 7-segment window. Low-latency HLS was tried and caused monotonic clock rollback on the burned-in tile clocks; classic segmented HLS is stable and the extra latency is acceptable for a multiviewer. All paths set `mxlH264IDRPeriod: 30` to force a keyframe every 1 second, ensuring the HLS segmenter always has a cut point.

**WebRTC (WHEP):** mediamtx serves WHEP signalling on `:8889`. ICE media goes over a dedicated UDP port (`:8189`) exposed by a separate LoadBalancer (`mediamtx-webrtc-udp.yaml`). `webrtcAdditionalHosts` advertises the LoadBalancer's external address as the ICE candidate so browsers reach the RTP path directly; the Caddy port at `:8080` is only for signalling and static assets.

**Caddy sidecar:** A `caddy:2-alpine` container in the same pod. It:
- Serves `index.html` from `/srv` as the catch-all file server.
- Proxies `/hls/*` to mediamtx on `localhost:8888`.
- Proxies `/webrtc/*` to mediamtx on `localhost:8889`, rewriting `Location` headers to keep the WHEP session resource under `/webrtc`.
- Proxies `/api/*` to the demo-metrics aggregator at `http://demo-metrics:8088`.
- Proxies `/stats.json` to the compositor at `http://composite:9090`.

The mediamtx control API (`:9997`) is never proxied by Caddy; it is only reachable cluster-internally.

_Source: `k8s/mediamtx-deployment.yaml`, `k8s/config/mediamtx.yml`, `k8s/config/Caddyfile`_

---

## 5. Metrics Aggregator

A dependency-free Python HTTP server (`aggregator.py`) that runs as its own Deployment (`demo-metrics`). It requires no pip installs — only the Python standard library — so it runs on `python:3-slim` without a build step.

**Data sources merged per flow:**
- Compositor `/stats.json` — per-flow `fps`, `pushed`, `missed`, `mbps`, `live`.
- Kubernetes pod API — writer pod `node`, `phase`, `ready`, `restarts`, `image`, `pattern`.
- `MxlReceiver` CR — `phase`, `provider`, `boundMirror`.
- `MxlFlowMirror` CR — `phase`, `sourceNode`, `provider`.
- `MxlFlow` CR (cluster-scoped) — `OriginFresh` condition, per-node `locations`.
- Gateway pods in `mxl-system` — `node`, `ready`, `restarts`.

**Endpoints:**
- `GET /api/flows` — returns the merged JSON for all four flows plus gateway status. The frontend polls this every 1.5 seconds.
- `POST /api/kill/<n>` — deletes the `writer-mxl-<n>` pod to demonstrate kill-and-recover resilience. The Deployment's `Recreate` strategy brings it back automatically.

**Namespace autodetect:** The aggregator reads its own service-account namespace from `/var/run/secrets/kubernetes.io/serviceaccount/namespace` at startup, so the same image works in both `demo-app` (sc cluster) and `default` (EKS). `GW_NS` defaults to `mxl-system`. An override is available via `DEMO_NS`.

**Liveness classification:** A flow is considered `live` when its writer pod is ready, its `MxlFlow` `OriginFresh` condition is `True`, and either a `Ready` mirror exists (cross-node RDMA path) or no mirror exists at all (the writer is co-located with the consumer, so the flow is read directly from the local domain).

_Source: `k8s/metrics/aggregator.py`_

---

## 6. Frontend

The frontend is a single `index.html` file served by the Caddy sidecar. It is a Qvest-branded multiviewer with no build step and no bundler; the only external dependency is `hls.js` loaded from a CDN.

**Player strategy per tile:** Each of the four MXL flows is played by its own `<video>` element. On tab activation, each tile attempts WHEP (WebRTC) first. The WHEP path is non-trickle: it gathers all ICE candidates, POSTs the offer to `/webrtc/mxl-<n>/whep`, and applies the answer. An 8-second timeout triggers HLS fallback (`/hls/mxl-<n>/index.m3u8` via hls.js). On clusters without a working ICE UDP path, all four tiles transparently fall back to HLS.

**Visibility gate:** All player resources (WebRTC `PeerConnection` objects, hls.js instances, and polling timers) are tracked in a `MV` registry. On `visibilitychange`, if the tab becomes hidden, `MV.teardownAll()` closes every connection and clears every interval — preventing background WebRTC decode from starving other applications. When the tab becomes visible again, only the currently active scene's players are rebuilt from scratch.

**`window.__mvDebug`:** A verification hook exposed on `window` for console use: `window.__mvDebug.counts()` returns `{pc: N, hls: M}` — the number of live `PeerConnection` and hls.js instances at the time of the call. When the tab is hidden, both counts should be zero.

**RDMA metrics panel:** The right-hand panel polls `GET /api/flows` every 1.5 seconds and renders per-flow grain rate, throughput, mirror status, receiver phase, origin freshness, and source node. Expandable "Details" rows show the full CR and pod state. "Kill" buttons call `POST /api/kill/<n>` to trigger a pod delete.

**Tab structure:** The page has four tabs — Multiviewer (default), txDarwin/SRT, Composite, and Booking. Each tab is a separate scene; switching tears down the previous scene's players before starting the new one.

_Source: `k8s/config/index.html`_

---

## 7. Routing

External traffic reaches the demo via a Gateway API `HTTPRoute` that attaches to the `istio-ingressgateway` Gateway in `istio-system` (sectionName `https`).

**Hostname template:** `demo${hostname_suffix}.${cluster_domain}` — the suffix and domain are injected at kustomize render time, so the same manifests work for both the sc cluster and the EKS demo without editing the route.

**Backend:** All requests on the hostname route to `mediamtx:80` (the Caddy sidecar's port). Caddy dispatches from there: static assets and the multiviewer page are served directly; HLS, WebRTC signalling, API calls, and stats are proxied internally.

_Source: `k8s/httproute.yaml`_

---

## 8. Load-bearing Constraints

Two constraints are architectural — removing or mismatching them breaks the demo at runtime.

### go-mxl lock-step

The compositor, writers, and gateway all link against `libmxl.so`. MXL's domain protocol requires that every reader and writer sharing a domain use a byte-identical `libmxl.so`; a version mismatch between the compositor image and the mxl-k8s gateway DaemonSet causes cross-node mirror reads to fail silently (grains appear present but contain garbage or the reader returns `FLOW_INVALID`).

This means the `go-mxl` tag pinned in the compositor's `Dockerfile` and the tag the mxl-k8s gateway image was built from must match exactly. Updating either one in isolation breaks the other. The CI workflow for the compositor image and the gateway release must stay in lock-step.

### Qvest deltas to upstream mediamtx

The demo depends on two changes that are not in upstream mediamtx:

1. **MXL static-source plugin** (`feature/mxl-static-source` branch) — allows mediamtx paths to declare `source: mxl:///run/mxl/domain/<uuid>`, reading flows zero-copy from the local domain without a separate RTSP publisher. This is what enables the four per-tile mediamtx paths in `mediamtx.yml`.

2. **Producer-pacing** — the upstream mediamtx had no way to pace the MXL reader to the flow's nominal grain rate; the qvest fork adds a pacing mechanism so mediamtx does not busy-read the ring buffer at maximum speed, which would interfere with other consumers on the same node.

Without both deltas, the image referenced in `k8s/mediamtx-deployment.yaml` (`ghcr.io/qvest-digital/mediamtx-mxl`) must be used — switching to upstream mediamtx removes MXL source support entirely.
