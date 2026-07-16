# Buchungs-Demo: Architektur & Live-Stand (DMF-298 / DMF-303)

Stand: 16.07.2026 · alle Zeiten Europe/Berlin · Quellen: Booking-API (`demo.dmf…/api/booking`), Cluster-Events, txDarwin-Module-API — live abgefragt.

Eine Buchung in der MediaOps Scheduling-App deployt automatisch eine txDarwin-Instanz auf dem dev-Cluster — und räumt sie nach dem Nachlauf wieder ab. Alles Folgende wurde am 16.07. vormittags live beobachtet.

> 🎨 Formatierte Version: [`booking-demo-architecture.html`](./booking-demo-architecture.html) (self-contained, lokal im Browser öffnen).

## Heute live gelaufen: Buchung A → t1, Buchung B → t2

Zwei Buchungen aus der Scheduling-App (Job #00053 „Test kurz A" auf Template 1, Job #00054 auf Template 2):

| Zeit | Buchung | Ereignis |
|---|---|---|
| 10:45:06 | A | Pre-Roll feuert `MXL_DMF_ApplyInstance` → [Action-Run ↗](https://github.com/qvest-digital/mxl-dmf-txdarwin-flux/actions/runs/29484578598) → Commit `instance: apply txdarwin/t1` (10:45:13) |
| 10:46:27 | A | Pod `t1` scheduled (Template 1, Chart `txdarwin@0.3.0`); Image aus ECR in 111 ms (Cache) → Container laufen |
| 10:53 | A | t1 **Reachable / on-air**, UI `txdarwin-t1.dmf…` antwortet HTTP 200 (Beobachtungszeitpunkt) |
| 10:56:01 | B | Pre-Roll-Dispatch [Action-Run ↗](https://github.com/qvest-digital/mxl-dmf-txdarwin-flux/actions/runs/29485253080) → Commit `instance: apply txdarwin/t2` (10:56:07) |
| 10:57:45 | B | Pod `t2` scheduled (Template 2) — **beide Instanzen laufen parallel**, getrennte Ressourcen-Pools |
| 11:00:02 | A | Post-Roll-Dispatch [Action-Run ↗](https://github.com/qvest-digital/mxl-dmf-txdarwin-flux/actions/runs/29485496466) → Commit `instance: delete txdarwin/t1` |
| 11:01:14 | A | Teardown: `Helm uninstall t1` + `HelmChart deleted` — automatisch, niemand hat geklickt |
| 11:15:01 | B | Post-Roll-Dispatch [Action-Run ↗](https://github.com/qvest-digital/mxl-dmf-txdarwin-flux/actions/runs/29486439575) → Commit `instance: delete txdarwin/t2` → t2 abgeräumt; `main` läuft durchgehend weiter |

Alle vier Action-Runs (2× Apply, 2× Delete) liefen **success** — die Run-Links sind der exakte Zeitstempel-Beleg für Hoch- und Runterfahren je Buchung.

## Der Lifecycle: von der Buchung zum Pod und zurück

Jeder Schritt ist ein bestehender Mechanismus — DataMiner orchestriert, GitOps deployt. Kein Handgriff zwischen Buchung und Sendung.

**Aufbau (Pre-Roll Start):**

```
MediaOps Job (Scheduling-App, Confirm)
  → Booking / SRM (Pool txDarwin T1 bzw. T2)
  → Pre-Roll START (Automated Action)
  → MXL_DMF_ApplyInstance (DataMiner-Skript → GitHub dispatch)
  → Git-Commit + OCI-Artefakt (mxl-dmf-txdarwin-flux)
  → Flux → MediaFunctionInstance-CR
  → dmf-operator → HelmRelease txdarwin@0.3.0
  → Pod on-air (≈ 2 min nach Dispatch)
```

**Abbau (Post-Roll Stop):**

```
Post-Roll STOP (Automated Action)
  → MXL_DMF_DeleteInstance (DataMiner-Skript → GitHub dispatch)
  → Delete-Commit + Artefakt-Rebuild ("instance: delete txdarwin/t1")
  → Flux prune (CR verschwindet)
  → Helm uninstall (heute 11:01:14 für t1)
```

## Zwei Templates, zwei Pools

Der einzige Unterschied der Templates ist die Zahl der SRT-Quellen. Je Template ein eigener SRM-Ressourcen-Pool (MaxConcurrency 1) — deshalb dürfen sich Buchungen der beiden Templates überlappen, wie heute 10:57–11:01.

| | Template 1 → `t1` | Template 2 → `t2` |
|---|---|---|
| Ticket | DMF-298 | DMF-303 |
| SRT-Quellen | 2 (SRT-1, SRT-2) | 3 (SRT-1, SRT-2, SRT-3) |
| Pool | txDarwin T1 | txDarwin T2 |
| Writer-Flow | `c3…e5353879bd69` | `c3…2a5bd02710e9` |

Writer-Flow-IDs sind deterministisch (`sha1(Instanzname)`) — Viewer-Panels bleiben über Re-Deploys stabil. Die permanente Instanz `main` nutzt `c3…b28b7af69320`.

## txDarwin: Signalweg & SRT-Switch

```
SRT-Quellen (1 = extern via NLB · 2/3 = Cluster-Zuspieler)
  → srt-mxl-bridge (eine je Ingest)
  → MXL-Flows (b2…0001 / 0002 / 0003)
  → MxlReader (flowId = gewählte Quelle)
  → MxlWriter (1296×720 @ 29.97)
  → mediamtx (WHEP/HLS)
  → Demo-App-Kachel (Booking-Tab)
```

**Switch-Mechanik:** Der SRT-n-Button in der txDarwin-UI setzt die `flowId` des Readers und startet das Modul neu. Der „reading"-Chip in der Demo-App folgt live — er liest den tatsächlichen Reader-Zustand über die txDarwin-API (`:8002/modules`), nicht die Konfiguration.

## Ehrlicher Status: bekannte Baustellen

Alles diagnostiziert, nichts davon blockiert den Buchungs-Lifecycle.

- **Writer-Latch (DMF-438):** Nach jedem Quellen-Switch oder Reader-Restart nimmt der MxlWriter 1–15 min keinen Input an (0 bps), Reader meldet derweil grün. Demo-Regel: Switch als letzter Programmpunkt.
- **Periodische Quellen-Lücken:** Cluster-Zuspieler verlieren die SRT-Session periodisch (kube-proxy-UDP-Conntrack), die Bridge beendet sich per Design bei Caller-Disconnect — Kubernetes-Backoff macht aus Sekunden-Lücken Minuten. Vor Demos: Bridge-Pods frisch starten.
- **SRT-1 braucht externen Feed:** Der MXL-Flow `b2…0001` existiert nur, solange ein externer Sender (Kamera/Mac) am öffentlichen Ingest anliegt. Ohne Feed lesen Instanzen auf SRT-1 ins Leere.
