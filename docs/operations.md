# Operations Runbook

> **Audience:** Internal Qvest engineers with cluster access who deploy, debug, or demonstrate the app.<br>
> **Companion docs:** [architecture.md](architecture.md) for component deep-dives; [README.md](../README.md) for the 60-second story; [docs/ci-and-release.md](ci-and-release.md) for CI pipeline and release mechanics.

---

## 1. How deployment works

There is no local `kubectl apply -f .` path. Every cluster change goes through CI.

**CI pipeline (`.github/workflows/build.yml`):**

1. On every qualifying push, CI copies the entire `k8s/` tree (manifests plus the `config/` subdirectory) to a staging path.
2. It runs `flux push artifact` to push the staged tree as an OCI artifact to `ghcr.io/qvest-digital/mxl-dmf-demo-app-manifests`, tagged with the short commit SHA.
3. A second `flux tag artifact` call applies a human-readable tag (see tagging logic below).
4. The Flux Kustomization watching that OCI source on the cluster reconciles the new revision and applies the diff.

**Artifact tagging by trigger:**

| Trigger | Tag applied |
|---|---|
| Push to `main` | `latest` |
| Pull request | `pr-<N>` (e.g. `pr-42`) |
| Version tag (`v*`) | The tag name (e.g. `v1.0.0-rc.3`) |
| `workflow_call` / `workflow_dispatch` with `pr_env` input | `<pr_env>-latest` (e.g. `pr-185-latest`) |

**ConfigMap hash-suffix rolling restart (`k8s/kustomization.yaml`):**

The `configMapGenerator` in `k8s/kustomization.yaml` generates hash-suffixed names for three ConfigMaps: `mediamtx-config`, `multiviewer` (Caddyfile + `index.html`), and `demo-metrics-script`. Whenever a file under `config/` changes, kustomize produces a new ConfigMap name; the Deployment's volume reference updates to match, causing a spec diff; Kubernetes rolls the pod. This means changes to `mediamtx.yml`, `Caddyfile`, `index.html`, or `aggregator.py` land on the cluster without any manual `rollout restart`. The mechanism is fully GitOps-safe: a cluster recreate replays the same hashes deterministically.

**To verify reconciliation status:**

```bash
flux get source oci -A
flux get kustomization -A
```

---

## 2. PR preview environments

Two separate mechanisms create PR preview environments; both end up with a tagged OCI artifact that a Flux `OCIRepository` on that environment's cluster can point at.

**Direct PR path** (triggered by opening or updating a pull request against `main`):

- The standard `pull_request` CI path renders `k8s/` and pushes the artifact tagged `pr-<N>`.
- The corresponding Flux `OCIRepository` on the PR environment should be configured to track that tag.

**Terraform-driven PR env path** (called by `mxl-dmf-terraform` via `workflow_call` / `workflow_dispatch`):

- Terraform's `cd-pr-feature-up.yml` invokes this workflow with `pr_env=<name>` and `ref=<feature-branch>`.
- CI checks out the specified `ref`, renders manifests, and tags the artifact `<pr_env>-latest`.
- Terraform repoints its demo-app `OCIRepository` at the matching `<pr_env>-latest` tag on that PR environment's cluster.

**Hostname templating (`k8s/httproute.yaml`):**

The `HTTPRoute` hostname is `demo${hostname_suffix}.${cluster_domain}`. Both variables are injected at kustomize render time via the cluster's kustomize patches, so the same `httproute.yaml` produces the correct hostname for each environment without manual editing.

**WebRTC on PR environments:** The mediamtx `webrtcAdditionalHosts` setting advertises the LoadBalancer address as the ICE candidate. A PR environment has its own WebRTC LoadBalancer; if the cluster's infrastructure is not provisioned with a dedicated UDP load balancer for that environment, WHEP will stall and tiles will fall back to HLS. This is expected and not a bug in the manifests.

---

## 3. Running the demo

**Opening the multiviewer:**

Navigate to the demo hostname in a browser. The default **Multiviewer** tab shows four tiles, one per MXL flow (`mxl-1` through `mxl-4`). Each tile independently attempts WHEP (WebRTC) first; if the ICE handshake does not complete within 8 seconds it falls back to HLS via hls.js. On clusters where the WebRTC UDP path is reachable, tiles connect over WebRTC; on clusters without a working ICE path, all four tiles use HLS transparently.

**Visibility gate (important):** All player resources — WebRTC `PeerConnection` objects, hls.js instances, and polling timers — are torn down when the browser tab is hidden or backgrounded, and rebuilt when the tab becomes visible again. This is intentional: background WebRTC decode would starve other applications. If you switch to another tab and come back, the players rebuild from scratch. **If tiles appear to never start playing, check that the tab is foregrounded and not occluded.**

**Checking live player counts in the browser console:**

```javascript
window.__mvDebug.counts()
// Returns {pc: N, hls: M} — live PeerConnection and hls.js instance counts.
// Both should be 0 when the tab is hidden.
```

**Metrics panel:**

The right-hand panel polls `GET /api/flows` every 1.5 seconds. It shows per-flow:

- Grain rate, throughput, and compositor `live` status (from `/stats.json` via the compositor)
- Mirror `phase` and `sourceNode` — whether the flow is read locally (writer co-located with consumer) or over RDMA
- `MxlReceiver` and `MxlFlowMirror` CR state
- Writer pod status and restart count

Expanding a row shows the full CR and pod state. The `sourceNode` field makes the local-vs-RDMA distinction visible at a glance.

**Kill-and-recover demo:**

1. In the metrics panel, click the **Kill** button for any flow. This calls `POST /api/kill/<n>`, which deletes the `writer-mxl-<n>` pod.
2. The writer Deployment's `Recreate` strategy brings a new pod up automatically.
3. Watch the metrics panel: the flow goes offline, the mirror CR transitions, and the tile recovers once the new writer is ready and the intent-shim unblocks the reader.

The kill endpoint can also be called directly:

```bash
# Delete writer pod N (1–4) via the aggregator API — replace <host> with the demo hostname
curl -X POST https://<host>/api/kill/<n>
```

---

## 4. Troubleshooting

All commands below are generic; substitute the actual namespace, pod name, or resource name as discovered by the lookup commands.

### Symptom → check table

| Symptom | What to check | Where to look |
|---|---|---|
| Manifests not reconciling | Run `flux get kustomization -A` and `flux get source oci -A`; check the artifact digest and reconciliation status. If the source shows an error, verify the OCI tag exists and the pull secret is valid. | `flux get source oci -A`; `kubectl get kustomization -A` |
| Tiles never play (all four stuck) | (1) Check that the tab is foregrounded — the visibility gate tears down all players when the tab is hidden. (2) Check the compositor pod is Running and `/stats.json` is reachable: `kubectl get pod -l app=composite -A`. (3) Check mediamtx WHEP/HLS: `kubectl logs -l app=mediamtx -A --tail=50`. (4) For WebRTC, check the mediamtx `webrtcAdditionalHosts` config and whether the UDP LoadBalancer has healthy targets. | `k8s/composite-deployment.yaml`; `k8s/config/mediamtx.yml`; `k8s/mediamtx-webrtc-udp.yaml` |
| Empty or green tiles | Frame-geometry mismatch: `MXL_FRAME_WIDTH` and `MXL_FRAME_HEIGHT` in `k8s/composite-deployment.yaml` must match the actual v210 grain dimensions the writer flows declare. The compositor defaults to 1920×1080; the flows are 1296×720. A mismatch causes the compositor to wrap each grain with the wrong size, producing "invalid video buffer" errors and green/empty output. Check the env vars on the `composite` Deployment: `kubectl describe deployment composite -A`. | `k8s/composite-deployment.yaml` (env `MXL_FRAME_WIDTH`, `MXL_FRAME_HEIGHT`) |
| Flows missing / `FLOW_NOT_FOUND` | Check in order: (1) Writer pods are Running and Ready: `kubectl get pods -l app in (writer-mxl-1,writer-mxl-2,writer-mxl-3,writer-mxl-4) -A`. (2) mxl-k8s gateway and agent DaemonSets are Ready on all nodes: `kubectl get daemonset -n mxl-system`. (3) `MxlFlow` CRs exist for all four UUIDs: `kubectl get mxlflow -A`. (4) `MxlFlowMirror` CRs are in a Ready/Mirroring phase: `kubectl get mxlflowmirror -A`. (5) Check for go-mxl version skew: the compositor image and the mxl-k8s gateway image must be built from the same `libmxl.so` (see [architecture.md §8 — go-mxl lock-step](architecture.md#go-mxl-lock-step)); a mismatch causes cross-node mirror reads to fail silently or return `FLOW_INVALID`. | `k8s/composite-deployment.yaml`; `kubectl describe mxlflow`; `kubectl describe mxlflowmirror` |
| Metrics panel disagrees with playing tiles | The aggregator (`GET /api/flows`) merges data from multiple sources including the compositor's `/stats.json` and Kubernetes CRs. These can desync transiently — for example, a flow's CR may still show a stale phase after recovery while the compositor has already resumed. **The tiles are authoritative for "is it playing"**: if a tile is showing video, the stream is live regardless of what the panel reports. If the panel is persistently wrong, check the aggregator pod: `kubectl logs -l app=demo-metrics -A --tail=50`. | `k8s/metrics/aggregator.py`; `kubectl logs -l app=demo-metrics -A` |

### Useful one-liners

```bash
# Check all demo-app pods at a glance
kubectl get pods -A -l 'app in (composite,mediamtx,demo-metrics,writer-mxl-1,writer-mxl-2,writer-mxl-3,writer-mxl-4)'

# Compositor stats (from within the cluster)
kubectl exec -n <namespace> <composite-pod> -c compositor -- curl -s localhost:9090/stats.json | jq .

# mxl-k8s control-plane health
kubectl get mxlflow,mxlflowmirror,mxlreceiver -A

# Flux reconcile status
flux get kustomization -A
flux get source oci -A
```
