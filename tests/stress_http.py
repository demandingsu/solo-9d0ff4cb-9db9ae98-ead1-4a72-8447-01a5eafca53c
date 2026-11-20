"""Real-HTTP concurrency stress test against a running uvicorn instance.

Spawns parallel uploaders, a reader loop, and a withdrawal against the
live server (not the in-process TestClient) to validate WAL + BEGIN
IMMEDIATE serialization over TCP. Run manually against a started server.
"""
from __future__ import annotations

import json
import threading
import urllib.request

BASE = "http://127.0.0.1:8000"
VISITOR = "stress-visitor"


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def event(eid, pv, etype, t):
    return {
        "event_id": eid, "visitor_id": VISITOR, "page_view_id": pv,
        "exhibit_id": f"EX-{pv}", "event_type": etype, "event_time": t,
    }


errors: list[str] = []
barrier = threading.Barrier(5)


def uploader(start: int) -> None:
    barrier.wait()
    try:
        for i in range(start, start + 40):
            pv = f"pv{i}"
            call("POST", "/events", event(f"{i}-o", pv, "open",
                                          f"2026-11-18T08:{i % 60:02d}:00Z"))
            call("POST", "/events", event(f"{i}-c", pv, "close",
                                          f"2026-11-18T08:{i % 60:02d}:45Z"))
    except Exception as exc:
        errors.append(f"uploader: {exc!r}")


def reader() -> None:
    barrier.wait()
    try:
        for _ in range(200):
            path = call("GET", f"/visitors/{VISITOR}/path")
            if path["consent_withdrawn"]:
                if path["visits"]:
                    errors.append("withdrawn but visits visible")
            else:
                seqs = [v["sequence"] for v in path["visits"]]
                if seqs != list(range(1, len(seqs) + 1)):
                    errors.append("gap in sequence")
    except Exception as exc:
        errors.append(f"reader: {exc!r}")


def withdrawer() -> None:
    barrier.wait()
    try:
        call("POST", f"/visitors/{VISITOR}/withdraw",
             {"visitor_id": VISITOR})
    except Exception as exc:
        errors.append(f"withdrawer: {exc!r}")


threads = [
    threading.Thread(target=uploader, args=(0,)),
    threading.Thread(target=uploader, args=(1000,)),
    threading.Thread(target=uploader, args=(2000,)),
    threading.Thread(target=reader),
    threading.Thread(target=withdrawer),
]
for t in threads:
    t.start()
for t in threads:
    t.join()

# Steady state after withdrawal: everything rejected, path empty.
late = call("POST", "/events", event("late", "pvLate", "open",
                                     "2026-11-18T20:00:00Z"))
final = call("GET", f"/visitors/{VISITOR}/path")
print("late upload status:", late["status"])
print("final path:", json.dumps(final, ensure_ascii=False))
assert errors == [], errors
assert late["status"] == "rejected_withdrawn"
assert final["consent_withdrawn"] is True and final["total_visits"] == 0
print("CONCURRENCY STRESS OK")
