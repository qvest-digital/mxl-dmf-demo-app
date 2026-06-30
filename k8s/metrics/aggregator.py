#!/usr/bin/env python3
# demo-metrics: dependency-free aggregator for the multiviewer client.
#
# Merges, per flow, everything we can cheaply reach:
#   - compositor  : http://composite:9090/ (RDMA-delivered fps/grains/mbps)
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
import json, os, ssl, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NS = os.environ.get("DEMO_NS", "demo-app")
GW_NS = os.environ.get("GW_NS", "mxl-system")
FLOW_PREFIX = os.environ.get("FLOW_PREFIX", "d4d00000-0000-0000-0000-00000000000")
N_FLOWS = int(os.environ.get("N_FLOWS", "4"))

# The compositor that used to measure received fps/Mbit is gone (each flow now
# goes producer -> RDMA mirror -> mediamtx, no central consumer). Derive the
# panel from real CR/pod status plus the nominal grain rate: a Ready mirror
# transfers every 1080p v210 grain at the grain rate.
GRAIN_RATE = 30000.0 / 1001.0   # 29.97 fps
GRAIN_BYTES = 5529600           # 1080p v210 (1920 -> 5120 B/row * 1080)
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
        if fl:
            conds = {c["type"]: c for c in (fl.get("status", {}).get("conditions") or [])}
            of = conds.get("OriginFresh", {})
            flow_info = {
                "originFresh": of.get("status"),
                "originReason": of.get("reason"),
                "locations": [{"node": l.get("nodeName"), "phase": l.get("phase")}
                              for l in (fl.get("status", {}).get("locations") or [])],
            }

        writer_ok = bool(writer and writer.get("ready"))
        mirror_ok = any(m.get("phase") == "Ready" for m in mlist)
        origin_ok = bool(flow_info and flow_info.get("originFresh") == "True")
        live = writer_ok and mirror_ok and origin_ok
        comp = {
            "fps": round(GRAIN_RATE, 1) if live else 0,
            "mbps": round(GRAIN_BYTES * GRAIN_RATE * 8 / 1e6) if live else 0,
            "live": live,
        }

        result.append({
            "n": n,
            "label": f"MXL-{n}",
            "uuid": uuid,
            "compositor": comp,
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
        if self.path.startswith("/api/flows"):
            try:
                self._send(200, build())
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif self.path in ("/healthz", "/api/healthz"):
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
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
