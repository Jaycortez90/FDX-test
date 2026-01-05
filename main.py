import json
import asyncio
import os
import time
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Body
from fastapi.responses import HTMLResponse, Response

try:
    from pywebpush import webpush
    _PUSH_OK = True
except Exception:
    _PUSH_OK = False

try:
    from dateutil import parser as dtparser
    _DATEUTIL_OK = True
except Exception:
    _DATEUTIL_OK = False


app = FastAPI(title="Driver Status (Geofence)")

# =============================
# Geofence (QAR Duiven)
# =============================
HUB_NAME = "QAR Duiven"
HUB_LAT = 51.9672245
HUB_LON = 6.0205411
GEOFENCE_RADIUS_KM = 30.0
MAX_LOCATION_AGE_SECONDS = 120

# =============================
# Upload secret (required for desktop uploads)
# =============================
ADMIN_UPLOAD_SECRET = os.environ.get("ADMIN_UPLOAD_SECRET", "").strip()

# =============================
# Web Push (optional)
# =============================
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com").strip()
PUSH_ENABLED = bool(_PUSH_OK and VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)

# =============================
# In-memory stores (Render restarts will clear these)
# =============================
SNAPSHOT: Optional[Dict[str, Any]] = None
LAST_STATUS_KEY_BY_PLATE: Dict[str, str] = {}
SUBSCRIPTIONS_BY_PLATE: Dict[str, List[Dict[str, Any]]] = {}

STATUS_POLL_INTERVAL_SECONDS = 60

@app.on_event("startup")
async def _startup_status_poller():
    # Periodically re-evaluate statuses so time-based changes (45 min threshold)
    # can trigger push even without new uploads.
    if not PUSH_ENABLED:
        return

    async def _loop():
        global SNAPSHOT
        while True:
            try:
                if SNAPSHOT:
                    for m in (SNAPSHOT.get("movements", []) or []):
                        plate = normalize_plate(m.get("license_plate", ""))
                        if not plate:
                            continue
                        st = compute_driver_status(m)
                        new_key = st["status_key"]
                        old_key = LAST_STATUS_KEY_BY_PLATE.get(plate)
                        if old_key is None:
                            LAST_STATUS_KEY_BY_PLATE[plate] = new_key
                            continue
                        if new_key != old_key:
                            LAST_STATUS_KEY_BY_PLATE[plate] = new_key
                            _push_to_plate(plate, "Status update", st["status_text"])
            except Exception:
                pass
            await asyncio.sleep(STATUS_POLL_INTERVAL_SECONDS)

    asyncio.create_task(_loop())


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


def geofence_check(lat: float, lon: float, ts: int) -> Dict[str, Any]:
    now = int(time.time())
    if abs(now - int(ts)) > MAX_LOCATION_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Location timestamp too old. Refresh and try again.")

    dist = haversine_km(float(lat), float(lon), HUB_LAT, HUB_LON)
    if dist > float(GEOFENCE_RADIUS_KM):
        raise HTTPException(status_code=403, detail=f"Access denied (outside {GEOFENCE_RADIUS_KM:.0f} km of {HUB_NAME}).")

    return {"hub_name": HUB_NAME, "distance_km": dist, "radius_km": float(GEOFENCE_RADIUS_KM)}


def _parse_dt(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in {"nan", "none", "nat"}:
        return None

    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s[:-1])
        return datetime.fromisoformat(s)
    except Exception:
        pass

    if _DATEUTIL_OK:
        try:
            return dtparser.parse(s, dayfirst=True, fuzzy=True)
        except Exception:
            return None

    return None


def _has(v: Any) -> bool:
    s = str(v or "").strip()
    return bool(s) and s.lower() not in {"nan", "none", "nat"}


def compute_driver_status(m: Dict[str, Any]) -> Dict[str, Any]:
    close_door = m.get("close_door", "")
    location = m.get("location", "")
    trailer = str(m.get("trailer", "") or "").strip()
    sched_raw = m.get("scheduled_departure", "")

    sched_dt = _parse_dt(sched_raw)

    if _has(location):
        if trailer:
            msg = f"Please connect the {trailer} trailer on location: {location} and pick up the CMR documents in the office!"
        else:
            msg = f"Please connect the trailer on location: {location} and pick up the CMR documents in the office!"
        key = "LOCATION"
    elif _has(close_door):
        msg = "Your trailer is ready, please report in the office for further information!"
        key = "CLOSEDOOR_NO_LOCATION"
    else:
        minutes_left = None
        if sched_dt:
            minutes_left = (sched_dt - datetime.now()).total_seconds() / 60.0

        if minutes_left is not None and minutes_left > 45:
            msg = "Your trailer being loaded, please wait!"
            key = "LOADING_WAIT"
        else:
            msg = "Please report in the office!"
            key = "REPORT_OFFICE"

    report_at = ""
    if sched_dt:
        ra = sched_dt - timedelta(minutes=45)
        report_at = ra.strftime("%Y-%m-%d %H:%M")

    return {
        "status_key": key,
        "status_text": msg,
        "report_in_office_at": report_at,
    }


def destination_nav_url(m: Dict[str, Any]) -> Optional[str]:
    lat = m.get("destination_lat")
    lon = m.get("destination_lon")
    try:
        if lat is None or lon is None:
            return None
        latf = float(lat)
        lonf = float(lon)
        return f"https://www.google.com/maps/search/?api=1&query={latf},{lonf}"
    except Exception:
        return None


def _get_plate_record(plate: str) -> Optional[Dict[str, Any]]:
    global SNAPSHOT
    if not SNAPSHOT:
        return None
    moves = SNAPSHOT.get("movements", []) or []
    plate_n = normalize_plate(plate)
    matches = [m for m in moves if normalize_plate(m.get("license_plate", "")) == plate_n]
    if len(matches) == 1:
        return matches[0]
    if len(matches) == 0:
        return None
    raise HTTPException(status_code=409, detail="Multiple movements found for this plate. Contact the office.")


def _push_to_plate(plate: str, title: str, body: str) -> None:
    if not PUSH_ENABLED:
        return
    subs = SUBSCRIPTIONS_BY_PLATE.get(plate, []) or []
    if not subs:
        return

    vapid_claims = {"sub": VAPID_SUBJECT}
    payload = json.dumps({"title": title, "body": body})

    alive = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=vapid_claims,
            )
            alive.append(sub)
        except Exception:
            # Drop dead subscriptions silently
            pass

    SUBSCRIPTIONS_BY_PLATE[plate] = alive


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "push_enabled": PUSH_ENABLED,
        "snapshot_loaded": bool(SNAPSHOT),
    }


@app.post("/api/upload")
async def upload_snapshot(request: Request, secret: str = Query(..., min_length=8)) -> Dict[str, Any]:
    global SNAPSHOT, LAST_STATUS_KEY_BY_PLATE

    if not ADMIN_UPLOAD_SECRET:
        raise HTTPException(status_code=500, detail="Server not configured: ADMIN_UPLOAD_SECRET missing.")
    if secret != ADMIN_UPLOAD_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized.")

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    if not isinstance(body, dict) or "movements" not in body:
        raise HTTPException(status_code=400, detail="Snapshot must contain 'movements'.")

    SNAPSHOT = body

    # Push notifications on status change (best-effort)
    if PUSH_ENABLED:
        try:
            for m in (SNAPSHOT.get("movements", []) or []):
                plate = normalize_plate(m.get("license_plate", ""))
                if not plate:
                    continue
                st = compute_driver_status(m)
                new_key = st["status_key"]
                old_key = LAST_STATUS_KEY_BY_PLATE.get(plate)
                if old_key is None:
                    LAST_STATUS_KEY_BY_PLATE[plate] = new_key
                    continue
                if new_key != old_key:
                    LAST_STATUS_KEY_BY_PLATE[plate] = new_key
                    _push_to_plate(plate, "Status update", st["status_text"])
        except Exception:
            pass

    return {"ok": True, "count": len(SNAPSHOT.get("movements", []) or []), "push_enabled": PUSH_ENABLED}


@app.get("/api/status")
def get_status(
    plate: str = Query(..., min_length=2),
    lat: float = Query(...),
    lon: float = Query(...),
    ts: int = Query(..., description="Unix epoch seconds from the device"),
) -> Dict[str, Any]:
    gf = geofence_check(lat, lon, ts)

    rec = _get_plate_record(plate)
    if rec is None:
        return {
            "plate": normalize_plate(plate),
            "found": False,
            "last_refresh": (SNAPSHOT or {}).get("last_update"),
            "geofence": gf,
        }

    st = compute_driver_status(rec)

    return {
        "plate": normalize_plate(plate),
        "found": True,
        "status_key": st["status_key"],
        "status_text": st["status_text"],
        "destination_text": rec.get("destination_text") or (rec.get("destination_code") or ""),
        "destination_nav_url": destination_nav_url(rec),
        "scheduled_departure": rec.get("scheduled_departure") or "",
        "report_in_office_at": st["report_in_office_at"],
        "last_refresh": (SNAPSHOT or {}).get("last_update"),
        "geofence": gf,
        "push_enabled": PUSH_ENABLED,
        "vapid_public_key": VAPID_PUBLIC_KEY if PUSH_ENABLED else "",
    }


@app.post("/api/subscribe")
def subscribe(
    plate: str = Query(..., min_length=2),
    lat: float = Query(...),
    lon: float = Query(...),
    ts: int = Query(...),
    subscription: Dict[str, Any] = Body(...),
) -> Dict[str, Any]:
    if not PUSH_ENABLED:
        raise HTTPException(status_code=400, detail="Push is not enabled on the server.")

    geofence_check(lat, lon, ts)

    if not isinstance(subscription, dict) or "endpoint" not in subscription:
        raise HTTPException(status_code=400, detail="Invalid subscription.")

    plate_n = normalize_plate(plate)

    subs = SUBSCRIPTIONS_BY_PLATE.get(plate_n, []) or []
    endpoint = subscription.get("endpoint")
    subs = [s for s in subs if s.get("endpoint") != endpoint]
    subs.append(subscription)
    SUBSCRIPTIONS_BY_PLATE[plate_n] = subs

    return {"ok": True, "plate": plate_n, "count": len(subs)}


@app.get("/sw.js")
def sw() -> Response:
    return Response(content=SERVICE_WORKER_JS, media_type="application/javascript")


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Driver Status</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 16px; max-width: 720px; }
    input, button { font-size: 16px; padding: 10px; }
    button { cursor: pointer; }
    .row { display: flex; gap: 8px; }
    .row > * { flex: 1; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 14px; margin-top: 12px; }
    .muted { color: #666; }
    .status-big { font-size: 22px; font-weight: 700; line-height: 1.25; }
    .ok { background: #e9f6ea; border-color: #bfe6c3; }
    .warn { background: #fff5e6; border-color: #ffd18a; }
    .err { background: #fde8e8; border-color: #f5b5b5; }
    a { color: inherit; }
  </style>
</head>
<body>
  <h2>Movement status by license plate</h2>

  <div class="row">
    <input id="plate" placeholder="Enter license plate (e.g. AB-123-CD)" />
    <button id="btn">Check</button>
  </div>

  <div class="row" style="margin-top: 8px;">
    <button id="btnNotify" style="display:none;">Enable notifications</button>
  </div>

  <div id="out" class="card" style="display:none;"></div>

  <script>
    const API_BASE = window.location.origin;

    function normalizePlate(v) {
      return (v || "").toUpperCase().trim().replaceAll(" ", "").replaceAll("-", "");
    }

    function show(html, klass) {
      const out = document.getElementById("out");
      out.className = "card " + (klass || "");
      out.style.display = "block";
      out.innerHTML = html;
    }

    function getLocation() {
      return new Promise((resolve, reject) => {
        if (!navigator.geolocation) {
          reject(new Error("Geolocation not supported on this device."));
          return;
        }
        navigator.geolocation.getCurrentPosition(
          (pos) => {
            resolve({
              lat: pos.coords.latitude,
              lon: pos.coords.longitude,
              ts: Math.floor(Date.now() / 1000)
            });
          },
          (err) => reject(new Error(err.message || "Location denied.")),
          { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
        );
      });
    }

    async function checkStatus() {
      const plate = normalizePlate(document.getElementById("plate").value);
      if (!plate) return;

      show(`<div class="muted">Getting location…</div>`);

      let loc;
      try {
        loc = await getLocation();
      } catch (e) {
        show(`<b>Location error:</b> ${e.message}<div class="muted">Enable GPS and allow location permission.</div>`, "err");
        return;
      }

      show(`<div class="muted">Loading status…</div>`);

      try {
        const url = `${API_BASE}/api/status?plate=${encodeURIComponent(plate)}&lat=${encodeURIComponent(loc.lat)}&lon=${encodeURIComponent(loc.lon)}&ts=${encodeURIComponent(loc.ts)}`;
        const res = await fetch(url);
        const data = await res.json();

        if (!res.ok) {
          show(`<b>Error:</b> ${data.detail || res.statusText}`, "err");
          document.getElementById("btnNotify").style.display = "none";
          return;
        }

        const last = data.last_refresh || "-";
        const gf = data.geofence ? `${data.geofence.hub_name} (${data.geofence.distance_km.toFixed(1)} km)` : "-";

        if (!data.found) {
          show(`
            <div class="status-big">No movement found</div>
            <div class="muted">Geofence: ${gf}</div>
            <div class="muted">Last refresh: ${last}</div>
          `, "warn");
          document.getElementById("btnNotify").style.display = "none";
          return;
        }

        const destText = data.destination_text || "-";
        const destLink = data.destination_nav_url ? `<a href="${data.destination_nav_url}" target="_blank" rel="noopener">${destText}</a>` : destText;

        show(`
          <div class="status-big">"${data.status_text}"</div>
          <hr style="border:none;border-top:1px solid #ddd;margin:12px 0;">
          <div><b>Destination:</b> ${destLink}</div>
          <div><b>Scheduled departure date/time:</b> ${data.scheduled_departure || "-"}</div>
          <div><b>Report in the office:</b> ${data.report_in_office_at || "-"}</div>
          <div class="muted" style="margin-top:8px;">Geofence: ${gf}</div>
          <div class="muted">Last refresh: ${last}</div>
        `, "ok");

        if (data.push_enabled && data.vapid_public_key) {
          const bn = document.getElementById("btnNotify");
          bn.style.display = "block";
          bn.onclick = () => enableNotifications(plate, loc, data.vapid_public_key);
        } else {
          document.getElementById("btnNotify").style.display = "none";
        }

      } catch (e) {
        show(`<b>Network error:</b> ${e}`, "err");
        document.getElementById("btnNotify").style.display = "none";
      }
    }

    function urlBase64ToUint8Array(base64String) {
      const padding = '='.repeat((4 - base64String.length % 4) % 4);
      const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
      const rawData = window.atob(base64);
      const outputArray = new Uint8Array(rawData.length);
      for (let i = 0; i < rawData.length; ++i) outputArray[i] = rawData.charCodeAt(i);
      return outputArray;
    }

    async function enableNotifications(plate, loc, vapidPublicKey) {
      try {
        if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
          show(`<b>Notifications not supported</b><div class="muted">Use Chrome/Edge on Android. iOS requires adding the site to Home Screen.</div>`, "warn");
          return;
        }

        const perm = await Notification.requestPermission();
        if (perm !== 'granted') {
          show(`<b>Notifications denied</b><div class="muted">Allow notifications in browser settings.</div>`, "warn");
          return;
        }

        const reg = await navigator.serviceWorker.register('/sw.js');
        const sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
        });

        const resp = await fetch(`${API_BASE}/api/subscribe?plate=${encodeURIComponent(plate)}&lat=${encodeURIComponent(loc.lat)}&lon=${encodeURIComponent(loc.lon)}&ts=${encodeURIComponent(loc.ts)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(sub),
        });

        const data = await resp.json();
        if (!resp.ok) {
          show(`<b>Subscribe failed:</b> ${data.detail || resp.statusText}`, "err");
          return;
        }

        show(`<b>Notifications enabled</b><div class="muted">You will receive a push when your status changes.</div>`, "ok");
      } catch (e) {
        show(`<b>Subscribe error:</b> ${e}`, "err");
      }
    }

    document.getElementById("btn").addEventListener("click", checkStatus);
    document.getElementById("plate").addEventListener("keydown", (e) => {
      if (e.key === "Enter") checkStatus();
    });
  </script>
</body>
</html>"""

SERVICE_WORKER_JS = r"""
self.addEventListener('push', function(event) {
  let data = {};
  try { data = event.data.json(); } catch (e) { data = { title: 'Update', body: event.data && event.data.text() }; }
  const title = data.title || 'Status update';
  const options = { body: data.body || '' };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  event.waitUntil(clients.openWindow('/'));
});
"""