import json
import os
import time
import math
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

app = FastAPI(title="Driver Status (Geofence)")

# =========================
# Geofence (QAR Duiven)
# =========================
HUB_NAME = "QAR Duiven"
HUB_LAT = 51.9672245
HUB_LON = 6.0205411
GEOFENCE_RADIUS_KM = 30.0
MAX_LOCATION_AGE_SECONDS = 120

# =========================
# Snapshot source: R2 (Option A)
# Desktop app uploads status.json to R2 using /api/upload-url presigned link
# =========================
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
SNAPSHOT_KEY = os.environ.get("SNAPSHOT_KEY", "status.json")
ADMIN_UPLOAD_SECRET = os.environ.get("ADMIN_UPLOAD_SECRET", "")
LOCAL_SNAPSHOT_PATH = os.environ.get("LOCAL_SNAPSHOT_PATH", "local_status.json")

R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""

s3 = None
if R2_ENDPOINT and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY:
    s3 = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
    )

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Movement status</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 16px; max-width: 700px; }
    input, button { font-size: 16px; padding: 10px; }
    button { cursor: pointer; }
    .row { display: flex; gap: 8px; }
    .row > * { flex: 1; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 12px; margin-top: 12px; }
    .muted { color: #666; }
  </style>
</head>
<body>
  <h2>Movement status by license plate</h2>

  <div class="row">
    <input id="plate" placeholder="License plate (e.g. AB-123-CD)" />
    <button id="btn">Check</button>
  </div>

  <div id="out" class="card" style="display:none;"></div>

  <script>
    function normalizePlate(v) {
      return (v || "").toUpperCase().trim().replaceAll(" ", "").replaceAll("-", "");
    }

    function show(msg) {
      const out = document.getElementById("out");
      out.style.display = "block";
      out.innerHTML = msg;
    }

    function getLocation() {
      return new Promise((resolve, reject) => {
        if (!navigator.geolocation) {
          reject(new Error("Geolocation not supported."));
          return;
        }
        navigator.geolocation.getCurrentPosition(
          (pos) => resolve({
            lat: pos.coords.latitude,
            lon: pos.coords.longitude,
            ts: Math.floor(Date.now() / 1000)
          }),
          (err) => reject(new Error(err.message || "Location denied.")),
          { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
        );
      });
    }

    async function run() {
      const plate = normalizePlate(document.getElementById("plate").value);
      if (!plate) return;

      show('<div class="muted">Getting location…</div>');

      let loc;
      try { loc = await getLocation(); }
      catch (e) {
        show(`<b>Location error:</b> ${e.message}<div class="muted">Enable GPS and allow location permission.</div>`);
        return;
      }

      show('<div class="muted">Loading…</div>');

      try {
        const url = `/api/status?plate=${encodeURIComponent(plate)}&lat=${encodeURIComponent(loc.lat)}&lon=${encodeURIComponent(loc.lon)}&ts=${encodeURIComponent(loc.ts)}`;
        const res = await fetch(url);
        const data = await res.json();

        if (!res.ok) {
          show(`<b>Error:</b> ${data.detail || res.statusText}`);
          return;
        }

        const m = data.match;
        show(`
          <div><b>${data.plate}</b></div>
          <div class="muted">Access: within ${data.geofence.distance_km.toFixed(2)} km of ${data.geofence.hub_name} (radius ${data.geofence.radius_km} km)</div>
          <div class="muted">Last update: ${data.last_update || "-"}</div>

          <div class="card">
            <div><b>Movement:</b> ${m.movement_id || "-"}</div>
            <div><b>Route:</b> ${(m.origin_code || "-")} → ${(m.destination_code || "-")}</div>
            <div><b>Status:</b> ${m.status || "-"}</div>
            <div><b>Scheduled:</b> ${m.scheduled_departure || "-"}</div>
            <div><b>Actual:</b> ${m.actual_departure || "-"}</div>
          </div>
        `);
      } catch (e) {
        show(`<b>Network error:</b> ${e}`);
      }
    }

    document.getElementById("btn").addEventListener("click", run);
    document.getElementById("plate").addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  </script>
</body>
</html>
"""

def normalize_plate(value: str) -> str:
    v = (value or "").upper().strip()
    v = v.replace(" ", "").replace("-", "")
    return v

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c

def check_geofence(lat: float, lon: float) -> Dict[str, Any]:
    dist = haversine_km(lat, lon, HUB_LAT, HUB_LON)
    if dist > GEOFENCE_RADIUS_KM:
        raise HTTPException(status_code=403, detail="Access denied (outside allowed area).")
    return {"hub_name": HUB_NAME, "distance_km": dist, "radius_km": GEOFENCE_RADIUS_KM}

def read_snapshot() -> Dict[str, Any]:
    # Preferred: R2 (Option A). Fallback: local file (for quick testing).
    if s3 is not None and R2_BUCKET:
        try:
            obj = s3.get_object(Bucket=R2_BUCKET, Key=SNAPSHOT_KEY)
            raw = obj["Body"].read().decode("utf-8")
            return json.loads(raw)
        except s3.exceptions.NoSuchKey:
            raise HTTPException(status_code=503, detail="Snapshot not available yet.")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Snapshot read error: {e}")

    # Fallback: local file in the service container (NOT persistent on free tiers)
    if not os.path.exists(LOCAL_SNAPSHOT_PATH):
        raise HTTPException(status_code=503, detail="Snapshot not available yet (no R2; local snapshot missing).")
    try:
        with open(LOCAL_SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Local snapshot read error: {e}")

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True}

@app.get("/api/status")
def api_status(
    plate: str = Query(..., min_length=2),
    lat: float = Query(...),
    lon: float = Query(...),
    ts: int = Query(..., description="Unix epoch seconds from the device"),
) -> Dict[str, Any]:
    now = int(time.time())
    if abs(now - int(ts)) > MAX_LOCATION_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Location timestamp too old. Refresh and try again.")

    geo = check_geofence(float(lat), float(lon))

    plate_n = normalize_plate(plate)
    snap = read_snapshot()

    movements: List[Dict[str, Any]] = snap.get("movements", [])
    matches = [m for m in movements if normalize_plate(m.get("license_plate", "")) == plate_n]

    if len(matches) == 0:
        raise HTTPException(status_code=404, detail="No movement found for this license plate.")
    if len(matches) != 1:
        raise HTTPException(status_code=409, detail="Multiple movements found for this license plate. Contact dispatcher.")

    return {
        "plate": plate_n,
        "last_update": snap.get("last_update"),
        "geofence": geo,
        "match": matches[0],
    }


@app.post("/api/upload")
def upload_snapshot_local(secret: str = Query(..., min_length=8), payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Quick test endpoint (no R2 required):
    POST /api/upload?secret=...
    Body: JSON snapshot {"last_update": "...", "movements": [...]}

    Stores snapshot into LOCAL_SNAPSHOT_PATH inside the service.
    """
    if not ADMIN_UPLOAD_SECRET:
        raise HTTPException(status_code=500, detail="Server not configured: ADMIN_UPLOAD_SECRET missing.")
    if secret != ADMIN_UPLOAD_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized.")
    if payload is None:
        raise HTTPException(status_code=400, detail="Missing JSON body.")
    try:
        with open(LOCAL_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return {"ok": True, "stored": LOCAL_SNAPSHOT_PATH}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Local snapshot write error: {e}")


@app.get("/api/upload-url")
def upload_url(secret: str = Query(..., min_length=8)) -> Dict[str, Any]:
    if not ADMIN_UPLOAD_SECRET:
        raise HTTPException(status_code=500, detail="Server not configured: ADMIN_UPLOAD_SECRET missing.")
    if secret != ADMIN_UPLOAD_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized.")

    if s3 is None or not R2_BUCKET:
        raise HTTPException(status_code=500, detail="Server not configured: R2 variables missing.")

    try:
        url = s3.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": R2_BUCKET,
                "Key": SNAPSHOT_KEY,
                "ContentType": "application/json",
            },
            ExpiresIn=120,
        )
        return {"put_url": url, "expires_in": 120, "key": SNAPSHOT_KEY}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Presign error: {e}")
