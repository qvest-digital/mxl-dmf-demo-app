#!/usr/bin/env python3
# demo-metrics: dependency-free aggregator for the multiviewer client.
#
# Merges, per flow, everything we can cheaply reach:
#   - compositor  : http://composite:9090/ (mosaic stats; the per-flow panel
#                    numbers are derived instead — see the GRAIN_RATE comment)
#   - writer pod  : k8s API (node, phase, restarts, image, pattern, age)
#   - receiver    : MxlReceiver CR (phase, provider, bound mirror)
#   - mirror      : MxlFlowMirror CR (phase, sourceNode, provider)
#   - flow        : MxlFlow CR (OriginFresh, per-node origin locations)
#   - gateways    : mxl-system gateway pods (node, ready, restarts)
#
# Endpoints:
#   GET  /api/flows      -> combined JSON
#   POST /api/kill/<n>   -> delete the writer-mxl-<n> pod (watch it recover)
#
# Runs in-cluster with a scoped ServiceAccount; talks to the API server with
# the mounted SA token + CA. No pip deps so it runs on stock python:3-slim.
import base64, json, os, re, ssl, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _own_namespace():
    # The app runs in demo-app on the sc cluster but in default on the EKS demo
    # (Flux targetNamespace). Read the pod's own namespace so the namespaced
    # queries (pods/receivers/mirrors) hit the right one on either cluster.
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
            return f.read().strip()
    except OSError:
        return "demo-app"


NS = os.environ.get("DEMO_NS") or _own_namespace()
GW_NS = os.environ.get("GW_NS", "mxl-system")
FLOW_PREFIX = os.environ.get("FLOW_PREFIX", "d4d00000-0000-0000-0000-00000000000")
N_FLOWS = int(os.environ.get("N_FLOWS", "4"))

# No single consumer measures received fps/Mbit anymore: each per-flow tile
# goes producer -> RDMA mirror -> mediamtx, so there is nothing central to
# ask. Derive the panel from real CR/pod status plus the nominal grain rate:
# a Ready mirror transfers every 720p v210 grain at the grain rate.
GRAIN_RATE = 30000.0 / 1001.0   # 29.97 fps
GRAIN_BYTES = 2488320           # 720p v210 (1296 px wide -> 3456 B/row * 720)
COMPOSITOR = os.environ.get("COMPOSITOR_STATS", "http://composite:9090/")

API = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
with open(SA + "/token") as f:
    TOKEN = f.read().strip()
CTX = ssl.create_default_context(cafile=SA + "/ca.crt")


def k8s(path, method="GET"):
    req = urllib.request.Request(API + path, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, context=CTX, timeout=8) as r:
        return json.load(r) if method == "GET" else {"status": r.status}


def safe_k8s(path):
    try:
        return k8s(path)
    except Exception as e:
        return {"_error": str(e)}


def compositor_stats():
    try:
        with urllib.request.urlopen(COMPOSITOR, timeout=5) as r:
            return json.load(r)
    except Exception as e:
        return {"_error": str(e), "flows": []}


def idx_of(uuid):
    # FLOW_PREFIX + single digit
    try:
        return int(uuid[len(FLOW_PREFIX):])
    except Exception:
        return None


def build():
    stats = compositor_stats()
    comp_by_i = {f.get("i"): f for f in stats.get("flows", [])}

    pods = safe_k8s(f"/api/v1/namespaces/{NS}/pods").get("items", [])
    receivers = safe_k8s(
        f"/apis/mxl.qvest-digital.com/v1alpha1/namespaces/{NS}/mxlreceivers").get("items", [])
    mirrors = safe_k8s(
        f"/apis/mxl.qvest-digital.com/v1alpha1/namespaces/{NS}/mxlflowmirrors").get("items", [])
    # MxlFlow is CLUSTER-scoped (MxlReceiver/MxlFlowMirror are namespaced), so
    # it has to be listed at the non-namespaced path — the namespaced URL
    # returns nothing, which is why "origin fresh" read as unknown/no.
    flows = safe_k8s(
        "/apis/mxl.qvest-digital.com/v1alpha1/mxlflows").get("items", [])
    gw_pods = safe_k8s(f"/api/v1/namespaces/{GW_NS}/pods?labelSelector=app.kubernetes.io/component=gateway").get("items", [])

    def find_pod(app):
        for p in pods:
            if p.get("metadata", {}).get("labels", {}).get("app") == app:
                return p
        return None

    def flow_cr(uuid):
        for fl in flows:
            if fl.get("metadata", {}).get("name") == uuid:
                return fl
        return None

    def receiver_for(uuid):
        for rc in receivers:
            if rc.get("spec", {}).get("flowID") == uuid:
                return rc
        return None

    def mirror_for(uuid):
        out = []
        for m in mirrors:
            if m.get("metadata", {}).get("name", "").startswith(uuid):
                out.append(m)
        return out

    gateways = []
    for p in gw_pods:
        cs = (p.get("status", {}).get("containerStatuses") or [{}])[0]
        gateways.append({
            "name": p["metadata"]["name"],
            "node": p.get("spec", {}).get("nodeName"),
            "phase": p.get("status", {}).get("phase"),
            "ready": cs.get("ready"),
            "restarts": cs.get("restartCount"),
            "image": cs.get("image"),
        })

    result = []
    for n in range(1, N_FLOWS + 1):
        uuid = FLOW_PREFIX + str(n)
        app = f"writer-mxl-{n}"
        pod = find_pod(app)
        writer = None
        if pod:
            cs = (pod.get("status", {}).get("containerStatuses") or [{}])[0]
            env = {e["name"]: e.get("value") for e in
                   (pod.get("spec", {}).get("containers", [{}])[0].get("env") or [])}
            writer = {
                "pod": pod["metadata"]["name"],
                "node": pod.get("spec", {}).get("nodeName"),
                "phase": pod.get("status", {}).get("phase"),
                "ready": cs.get("ready"),
                "restarts": cs.get("restartCount"),
                "started": pod.get("status", {}).get("startTime"),
                "image": cs.get("image"),
                "pattern": env.get("MXL_FLOW_PATTERN"),
                "overlay": env.get("MXL_FLOW_OVERLAY"),
                # Overlay compositing format the writer runs (I420 fast path vs
                # the deliberate v210 reference tile). Defaults to I420 like the
                # writer itself when the env is unset.
                "overlayFormat": env.get("MXL_OVERLAY_FORMAT") or "I420",
            }

        rc = receiver_for(uuid)
        receiver = None
        if rc:
            receiver = {
                "name": rc["metadata"]["name"],
                "provider": rc.get("spec", {}).get("provider"),
                "phase": rc.get("status", {}).get("phase"),
                "boundMirror": (rc.get("status", {}).get("boundMirror") or {}).get("name"),
            }

        mlist = []
        for m in mirror_for(uuid):
            mlist.append({
                "name": m["metadata"]["name"],
                "phase": m.get("status", {}).get("phase"),
                "sourceNode": m.get("spec", {}).get("sourceNode"),
                "provider": m.get("spec", {}).get("provider"),
            })

        fl = flow_cr(uuid)
        flow_info = None
        media = None
        if fl:
            conds = {c["type"]: c for c in (fl.get("status", {}).get("conditions") or [])}
            of = conds.get("OriginFresh", {})
            flow_info = {
                "originFresh": of.get("status"),
                "originReason": of.get("reason"),
                "locations": [{"node": l.get("nodeName"), "phase": l.get("phase")}
                              for l in (fl.get("status", {}).get("locations") or [])],
            }
            # Media metadata straight off the flow definition: v210, resolution,
            # bit depth, grain rate, and the resulting uncompressed grain size /
            # RDMA throughput. v210 rows pad to a 48-pixel (128-byte) stride.
            d = fl.get("spec", {}).get("definition", {}) or {}
            if d.get("frame_width"):
                w = d.get("frame_width")
                h = d.get("frame_height") or 0
                gr = d.get("grain_rate", {}) or {}
                num = gr.get("numerator") or 0
                den = gr.get("denominator") or 1
                comps = d.get("components") or []
                stride = ((w + 47) // 48) * 128
                gbytes = stride * h
                rate = (num / den) if num else GRAIN_RATE
                media = {
                    "mediaType": d.get("media_type"),
                    "width": w,
                    "height": h,
                    "bitDepth": (comps[0].get("bit_depth") if comps else None),
                    "colorspace": d.get("colorspace"),
                    "grainRate": (f"{num}/{den}" if num else None),
                    "fps": round(rate, 2),
                    "grainBytes": gbytes,
                    "mbps": round(gbytes * rate * 8 / 1e6),
                }

        writer_ok = bool(writer and writer.get("ready"))
        mirror_ok = any(m.get("phase") == "Ready" for m in mlist)
        origin_ok = bool(flow_info and flow_info.get("originFresh") == "True")
        # A flow is live if its writer is up and its origin lease is fresh, AND
        # it's delivered: either a Ready cross-node mirror, or NO mirror at all
        # (the flow's producer is co-located with mediamtx, so it's read locally
        # — placement is dynamic, so which flow is local varies). Requiring a
        # mirror unconditionally made the local flow show as down.
        live = writer_ok and origin_ok and (mirror_ok or not mlist)
        gbytes = (media or {}).get("grainBytes") or GRAIN_BYTES
        rate = (media or {}).get("fps") or GRAIN_RATE
        comp = {
            "fps": round(rate, 1) if live else 0,
            "mbps": round(gbytes * rate * 8 / 1e6) if live else 0,
            "live": live,
        }

        result.append({
            "n": n,
            "label": f"MXL-{n}",
            "uuid": uuid,
            "compositor": comp,
            "media": media,
            "writer": writer,
            "receiver": receiver,
            "mirrors": mlist,
            "flow": flow_info,
        })

    return {
        "provider": "verbs",
        "grid": {"cols": 1, "rows": len(result)},
        "grainBytes": GRAIN_BYTES,
        "gateways": gateways,
        "flows": result,
    }


# ── Booking lifecycle (DMF-298/303 showcase) ────────────────────────────────
# The MediaOps booking deploys a per-booking txDarwin instance: a
# MediaFunctionInstance CR, from which the DMF operator renders a HelmRelease,
# which Flux turns into a pod. The showcase screen needs all three, plus the
# instance's *wired* sources — that is what tells template-1 (2 sources) from
# template-2 (3), and it lives in the HelmRelease values, not in the CR.
BOOKING_NS = os.environ.get("BOOKING_NS", "txdarwin")
# txDarwin serves its API over a self-signed cert on the pod itself.
INSECURE = ssl._create_unverified_context()


def _age(ts):
    """ISO8601 -> seconds, or None. The UI renders relative times itself."""
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - t).total_seconds())
    except Exception:
        return None


def _cond(obj, typ):
    for c in (obj.get("status", {}) or {}).get("conditions", []) or []:
        if c.get("type") == typ:
            return c
    return {}


def darwin_reader_flow(name):
    """The flow the instance is reading *right now*.

    The HelmRelease values only say where the reader was told to start; an
    operator can switch it in txDarwin's own UI, and the showcase's whole point
    is that this switch is visible. Ask the instance instead. Self-signed cert,
    chart-default credentials, short timeout — a slow or absent instance must
    not stall the screen.
    """
    try:
        # Fully qualified (this aggregator runs in the demo namespace, the
        # instance Services in the booking one) and against the LIST endpoint:
        # this API version 404s on /modules/<id>, it only serves the
        # collection. Pick the reader out of it.
        req = urllib.request.Request(f"https://{name}.{BOOKING_NS}:8002/modules")
        req.add_header("Authorization", "Basic " + base64.b64encode(b"admin:admin").decode())
        with urllib.request.urlopen(req, context=INSECURE, timeout=2) as r:
            mods = (json.load(r) or {}).get("data") or []
        reader = next((m for m in mods if m.get("type") == "MxlReader"), None)
        return ((reader or {}).get("options") or {}).get("flowId")
    except Exception:
        return None


def phase_of(inst, hr_exists, replicas, pod):
    """Where the booking stands, in the words the schedule uses.

    Derived rather than reported: nothing in the cluster knows about pre-roll.
    The CR is the booking's intent, the pod is the workload, and the release
    outlives both under a ScaleToZero reclaim.
    """
    if inst and not (pod and pod.get("phase") == "Running"):
        return "deploying"          # pre-roll: intent exists, workload coming up
    if inst and pod and pod.get("phase") == "Running" and not pod.get("deleting"):
        return "on-air"
    if not inst and pod:
        return "post-roll"          # CR pruned, workload winding down
    if not inst and hr_exists and replicas == 0:
        return "reclaimed"
    if not inst and not hr_exists:
        return "idle"
    return "unknown"


def booking():
    instances = safe_k8s(
        f"/apis/dmf.qvest-digital.com/v1alpha1/namespaces/{BOOKING_NS}/mediafunctioninstances"
    ).get("items", [])
    releases = safe_k8s(
        f"/apis/helm.toolkit.fluxcd.io/v2/namespaces/{BOOKING_NS}/helmreleases"
    ).get("items", [])
    pods = safe_k8s(f"/api/v1/namespaces/{BOOKING_NS}/pods").get("items", [])

    hr_by_name = {h["metadata"]["name"]: h for h in releases}
    # An instance is torn down CR-first, so key the view on the HelmRelease:
    # it outlives the CR (ScaleToZero) and its replicaCount is what actually
    # says whether the workload is meant to be running.
    names = sorted(set(list(hr_by_name) + [i["metadata"]["name"] for i in instances]))

    out = []
    for name in names:
        hr = hr_by_name.get(name, {})
        inst = next((i for i in instances if i["metadata"]["name"] == name), None)
        vals = (hr.get("spec", {}) or {}).get("values", {}) or {}
        flow = vals.get("flow", {}) or {}
        sources = flow.get("readerFlowIds") or ([flow["readerFlowId"]] if flow.get("readerFlowId") else [])
        pod = next((p for p in pods if p["metadata"]["name"].startswith(name + "-")), None)
        ready = _cond(hr, "Ready")
        out.append({
            "name": name,
            # The CR is the booking's intent; once it is gone the instance is
            # being reclaimed even though the release object may linger.
            "type": ((inst or {}).get("spec", {}) or {}).get("typeName"),
            "instancePhase": ((inst or {}).get("status", {}) or {}).get("phase") if inst else "reclaimed",
            "jobRef": (((inst or {}).get("spec", {}) or {}).get("booking", {}) or {}).get("jobRef"),
            "windowEnd": ((((inst or {}).get("spec", {}) or {}).get("booking", {}) or {}).get("window", {}) or {}).get("end"),
            "replicas": vals.get("replicaCount"),
            "helmReady": ready.get("status") == "True",
            "helmMessage": ready.get("message"),
            # Source count IS the template: 2 -> template-1, 3 -> template-2.
            # sources[0] is where the chart starts the reader.
            "sources": sources,
            "readerFlow": sources[0] if sources else None,
            # What it was configured to read vs. what it actually reads.
            "liveReaderFlow": None,
            "outFlow": flow.get("writerFlowId") or None,
            "pod": None if not pod else {
                "name": pod["metadata"]["name"],
                "phase": pod.get("status", {}).get("phase"),
                "node": pod.get("spec", {}).get("nodeName"),
                "ageSeconds": _age(pod["metadata"].get("creationTimestamp")),
                "deleting": bool(pod["metadata"].get("deletionTimestamp")),
            },
        })

    for row, name in ((r, r["name"]) for r in out):
        row["phase"] = phase_of(
            next((i for i in instances if i["metadata"]["name"] == name), None),
            name in hr_by_name, row["replicas"], row["pod"],
        )
        if row["phase"] == "on-air":
            row["liveReaderFlow"] = darwin_reader_flow(name) or row["readerFlow"]
        else:
            row["liveReaderFlow"] = row["readerFlow"]

    # Events carry the visible lifecycle ("Scheduled", "Pulled", "Started",
    # "Killing"). Newest last so the client can append without re-sorting.
    ev = safe_k8s(f"/api/v1/namespaces/{BOOKING_NS}/events?limit=40").get("items", [])
    events = sorted(
        ({
            "at": e.get("lastTimestamp") or e.get("eventTime"),
            "reason": e.get("reason"),
            "object": (e.get("involvedObject", {}) or {}).get("name"),
            "kind": (e.get("involvedObject", {}) or {}).get("kind"),
            "message": (e.get("message") or "")[:160],
        } for e in ev if e.get("lastTimestamp") or e.get("eventTime")),
        key=lambda x: x["at"] or "",
    )[-25:]

    return {"namespace": BOOKING_NS, "instances": out,
            "events": events, "story": storyline(events)}


# Kubernetes narrates its own plumbing: four container images pulled, four
# containers created, four started, per pod. On a stage that is noise. Keep the
# beats an audience can follow, and say them in the language of the schedule.
STORY = {
    "Scheduled":         ("deploy",   "Instance {obj} scheduled"),
    "ScalingReplicaSet": ("deploy",   "Workload scaling up"),
    "InstallSucceeded":  ("live",     "Instance {obj} installed"),
    "UpgradeSucceeded":  ("live",     "Instance {obj} upgraded"),
    "Killing":           ("teardown", "Instance {obj} being reclaimed"),
    "UninstallSucceeded": ("teardown", "Instance {obj} removed"),
}


def storyline(events):
    """The five beats that matter, newest last, one line each."""
    out = []
    for e in events:
        beat = STORY.get(e.get("reason"))
        if not beat:
            continue
        kind, text = beat
        obj = (e.get("object") or "").split("-")[0]
        out.append({"at": e["at"], "kind": kind, "text": text.format(obj=obj)})
    return out[-12:]


# ── Operator flow inventory ─────────────────────────────────────────────────
# Every MXL flow the mxl-k8s operator knows about, straight from the (cluster-
# scoped) MxlFlow CRs — not the hardcoded d4d writer set build() reports. The
# multiviewer's "operator flows" list renders this verbatim, so the demo shows
# whatever is actually registered on the cluster (ST 2110 gateway, tcp-demo,
# audio, ...), each with the media facts and origin health the operator tracks.
def operator_flows():
    items = safe_k8s(
        "/apis/mxl.qvest-digital.com/v1alpha1/mxlflows").get("items", [])
    out = []
    for fl in items:
        d = fl.get("spec", {}).get("definition", {}) or {}
        uuid = fl.get("metadata", {}).get("name") or fl.get("spec", {}).get("id")
        # urn:x-nmos:format:video -> "video"; keep raw if it isn't a URN.
        fmt = (d.get("format") or "").rsplit(":", 1)[-1] or None

        resolution = None
        if d.get("frame_width") and d.get("frame_height"):
            resolution = f"{d['frame_width']}x{d['frame_height']}"

        rate = None
        gr = d.get("grain_rate") or {}
        if gr.get("numerator"):
            rate = f"{gr['numerator']}/{gr.get('denominator', 1)}"
        sr = d.get("sample_rate") or {}
        if sr.get("numerator"):
            rate = f"{sr['numerator'] / max(1, sr.get('denominator', 1)) / 1000:g} kHz"

        origin_fresh = None
        for c in (fl.get("status", {}).get("conditions") or []):
            if c.get("type") == "OriginFresh":
                origin_fresh = c.get("status") == "True"
        locations = [
            {"node": l.get("nodeName"), "phase": l.get("phase")}
            for l in (fl.get("status", {}).get("locations") or [])
        ]

        grouphint = None
        gh = (d.get("tags") or {}).get("urn:x-nmos:tag:grouphint/v1.0")
        if isinstance(gh, list) and gh:
            grouphint = gh[0]

        out.append({
            "id": uuid,
            "label": d.get("label") or uuid,
            "description": d.get("description"),
            "format": fmt,
            "mediaType": d.get("media_type"),
            "resolution": resolution,
            "rate": rate,
            "channels": d.get("channel_count"),
            "colorspace": d.get("colorspace"),
            "grouphint": grouphint,
            "originFresh": origin_fresh,
            "locations": locations,
        })
    # Stable order: group by media format, then by uuid.
    out.sort(key=lambda f: ((f["format"] or "~"), f["id"] or ""))
    return {"flows": out}


# ── Per-flow preview (mediamtx control API) ─────────────────────────────────
# The operator-flows list can open a live preview of any flow: add a mediamtx
# path that reads the flow zero-copy from the local MXL domain, play its HLS,
# and delete the path on close so the reader doesn't linger. Only flows the
# operator actually knows about can be added — we validate the uuid against the
# MxlFlow set first, and the flow must be node-local to mediamtx (its Service
# node) for the mxl source to open.
MEDIAMTX_API = os.environ.get("MEDIAMTX_API", "http://mediamtx:9997")
MXL_DOMAIN = os.environ.get("MXL_DOMAIN", "/run/mxl/domain")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _mtx(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(MEDIAMTX_API + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode().strip()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode()
        except Exception:
            msg = ""
        return e.code, {"error": msg[:200]}


def _known_flow(uuid):
    for fl in safe_k8s(
            "/apis/mxl.qvest-digital.com/v1alpha1/mxlflows").get("items", []):
        if fl.get("metadata", {}).get("name") == uuid:
            return fl
    return None


def preview_add(uuid):
    if not _UUID_RE.match(uuid or ""):
        return 400, {"error": "bad flow id"}
    fl = _known_flow(uuid)
    if not fl:
        return 404, {"error": "flow not known to the operator"}
    name = "preview-" + uuid
    # Idempotent: reuse the path if the card was opened before.
    code, _ = _mtx(f"/v3/config/paths/get/{name}")
    if code != 200:
        d = fl.get("spec", {}).get("definition", {}) or {}
        conf = {"source": f"mxl://{MXL_DOMAIN}/{uuid}", "sourceOnDemand": False}
        if (d.get("format") or "").endswith("video"):
            conf.update({"mxlH264Preset": "veryfast",
                         "mxlH264Profile": "high",
                         "mxlH264Bitrate": 5000000})
        code, res = _mtx(f"/v3/config/paths/add/{name}", "POST", conf)
        if code != 200:
            return code, {"error": res.get("error") or "mediamtx add failed"}
    return 200, {"path": name, "hls": f"/hls/{name}/index.m3u8"}


def preview_del(uuid):
    if not _UUID_RE.match(uuid or ""):
        return 400, {"error": "bad flow id"}
    _mtx(f"/v3/config/paths/delete/preview-{uuid}", "DELETE")
    return 200, {"stopped": "preview-" + uuid}


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/booking"):
            try:
                self._send(200, booking())
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif self.path.startswith("/api/operator-flows"):
            try:
                self._send(200, operator_flows())
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif self.path.startswith("/api/flows"):
            try:
                self._send(200, build())
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif self.path in ("/healthz", "/api/healthz"):
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/api/preview/"):
            uuid = self.path.rstrip("/").rsplit("/", 1)[1]
            code, res = preview_del(uuid)
            return self._send(code, res)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.startswith("/api/preview/"):
            uuid = self.path.rstrip("/").rsplit("/", 1)[1]
            try:
                code, res = preview_add(uuid)
            except Exception as e:
                code, res = 500, {"error": str(e)}
            return self._send(code, res)
        if self.path.startswith("/api/kill/"):
            try:
                n = int(self.path.rsplit("/", 1)[1])
            except Exception:
                return self._send(400, {"error": "bad flow index"})
            if n < 1 or n > N_FLOWS:
                return self._send(400, {"error": "flow out of range"})
            app = f"writer-mxl-{n}"
            killed = []
            for p in safe_k8s(f"/api/v1/namespaces/{NS}/pods?labelSelector=app={app}").get("items", []):
                name = p["metadata"]["name"]
                try:
                    k8s(f"/api/v1/namespaces/{NS}/pods/{name}", method="DELETE")
                    killed.append(name)
                except Exception as e:
                    return self._send(500, {"error": f"delete {name}: {e}"})
            self._send(200, {"killed": killed, "flow": n})
        else:
            self._send(404, {"error": "not found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8088), H).serve_forever()
