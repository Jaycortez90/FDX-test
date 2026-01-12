import json
import asyncio
import os
import time
import math
import urllib.parse
import urllib.request
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query, Request, Body
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

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

try:
    import openpyxl  # type: ignore
    _OPENPYXL_OK = True
except Exception:
    openpyxl = None  # type: ignore
    _OPENPYXL_OK = False


app = FastAPI(title="Driver Status")

# =============================
# Paths (data + static)
# =============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(BASE_DIR, "static")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

# Background image path used by the website:
# Put your uploaded image into: static/bg.png
# (same folder as this main.py, inside "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Excel lookup files (server-side destination calculation)
# Optional env overrides:
#   - LOCATIONS_XLSX: path to FedEx_locations.xlsx / .xlsm
#   - DEST_LAND_XLSX: path to dest-land.xlsx / .xlsm
LOCATIONS_XLSX_ENV = os.environ.get("LOCATIONS_XLSX", "").strip()
DEST_LAND_XLSX_ENV = os.environ.get("DEST_LAND_XLSX", "").strip()


def _pick_existing_path(candidates: List[str]) -> str:
    first_non_empty = ""
    for p in candidates:
        p = (p or "").strip()
        if p and not first_non_empty:
            first_non_empty = p
        if p and os.path.exists(p):
            return p
    return first_non_empty


def _locations_path() -> str:
    # Prefer env var, otherwise try common locations
    candidates = [
        LOCATIONS_XLSX_ENV,
        os.path.join(DATA_DIR, "FedEx_locations.xlsx"),
        os.path.join(DATA_DIR, "FedEx_locations.xlsm"),
        os.path.join(BASE_DIR, "FedEx_locations.xlsx"),
        os.path.join(os.getcwd(), "data", "FedEx_locations.xlsx"),
        os.path.join(os.getcwd(), "FedEx_locations.xlsx"),
    ]
    return _pick_existing_path(candidates)


def _dest_land_path() -> str:
    candidates = [
        DEST_LAND_XLSX_ENV,
        os.path.join(DATA_DIR, "dest-land.xlsx"),
        os.path.join(DATA_DIR, "dest-land.xlsm"),
        os.path.join(DATA_DIR, "dest_land.xlsx"),
        os.path.join(DATA_DIR, "dest_land.xlsm"),
        os.path.join(BASE_DIR, "dest-land.xlsx"),
        os.path.join(BASE_DIR, "dest-land.xlsm"),
        os.path.join(os.getcwd(), "data", "dest-land.xlsx"),
        os.path.join(os.getcwd(), "dest-land.xlsx"),
    ]
    return _pick_existing_path(candidates)


LOCATIONS_XLSX = _locations_path()
DEST_LAND_XLSX = _dest_land_path()

# Loaded at startup
LOCATION_BY_CODE: Dict[str, Dict[str, Any]] = {}
DESTLAND_BY_CODE: Dict[str, Dict[str, Any]] = {}

# =============================
# Geofence (QAR Duiven) - still enforced, but NOT displayed on website
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
# Routing (optional) - OpenRouteService (truck route geometry)
# =============================
ORS_API_KEY = os.environ.get("ORS_API_KEY", "").strip()
ORS_DIRECTIONS_URL = "https://api.openrouteservice.org/v2/directions/driving-hgv/geojson"

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
MANUAL_STATUS_BY_PLATE: Dict[str, str] = {}
VIEWED_BY_PLATE: Dict[str, Dict[str, Any]] = {}  # plate -> {count:int, last_view:str}
STATUS_POLL_INTERVAL_SECONDS = 30


# -----------------------------
# Startup
# -----------------------------
@app.on_event("startup")
async def _startup():
    _load_destination_lookups()

    # Periodically re-evaluate statuses so time-based changes (45 min threshold)
    # can trigger push even without new uploads.
    if not PUSH_ENABLED:
        return

    async def _loop():
        global SNAPSHOT
        while True:
            try:
                if SNAPSHOT:
                    for m in _snapshot_movements():
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


# -----------------------------
# Helpers
# -----------------------------
def normalize_plate(value: str) -> str:
    v = (value or "").upper().strip()
    v = v.replace(" ", "").replace("-", "")
    return v


def _norm_code(value: Any) -> str:
    s = str(value or "").strip().upper()
    s = s.replace(" ", "").replace("-", "")
    return s


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


def geofence_check(lat: float, lon: float, ts: int) -> None:
    now = int(time.time())
    if abs(now - int(ts)) > MAX_LOCATION_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Location timestamp too old. Refresh and try again.")

    dist = haversine_km(float(lat), float(lon), HUB_LAT, HUB_LON)
    if dist > float(GEOFENCE_RADIUS_KM):
        raise HTTPException(status_code=403, detail=f"Access denied (outside {GEOFENCE_RADIUS_KM:.0f} km of {HUB_NAME}).")


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

def _format_dt_like(dt: datetime, sample: Any) -> str:
    """Format dt to match the date/time style of sample (scheduled_departure string)."""
    try:
        s = str(sample or "").strip()
    except Exception:
        s = ""

    # Default (ISO-like)
    date_fmt = "%Y-%m-%d"
    sep = " "

    # Preserve separator if sample uses 'T'
    if "T" in s and " " not in s:
        sep = "T"

    # Pick date format based on sample
    if re.search(r"\b\d{2}\.\d{2}\.\d{4}\b", s):
        date_fmt = "%d.%m.%Y"
    elif re.search(r"\b\d{2}/\d{2}/\d{4}\b", s):
        date_fmt = "%d/%m/%Y"
    elif re.search(r"\b\d{2}-\d{2}-\d{4}\b", s):
        date_fmt = "%d-%m-%Y"
    elif re.search(r"\b\d{4}/\d{2}/\d{2}\b", s):
        date_fmt = "%Y/%m/%d"
    elif re.search(r"\b\d{4}\.\d{2}\.\d{2}\b", s):
        date_fmt = "%Y.%m.%d"
    elif re.search(r"\b\d{4}-\d{2}-\d{2}\b", s):
        date_fmt = "%Y-%m-%d"

    has_seconds = bool(re.search(r":\d{2}:\d{2}(?!\d)", s))
    time_fmt = "%H:%M:%S" if has_seconds else "%H:%M"

    try:
        return dt.strftime(f"{date_fmt}{sep}{time_fmt}")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M")


def _format_scheduled_departure(sched_raw: Any) -> str:
    """Format scheduled_departure so it matches report_in_office_at style."""
    dt = _parse_dt(sched_raw)
    if dt:
        return _format_dt_like(dt, sched_raw)
    try:
        return str(sched_raw or "").strip()
    except Exception:
        return ""


def _has(v: Any) -> bool:
    s = str(v or "").strip()
    return bool(s) and s.lower() not in {"nan", "none", "nat"}


def compute_driver_status(m: Dict[str, Any]) -> Dict[str, Any]:
    # Dispatcher manual status (Driver message) overrides computed status
    plate_n = ""
    try:
        plate_n = normalize_plate(m.get("license_plate", ""))
    except Exception:
        plate_n = ""

    msg = (MANUAL_STATUS_BY_PLATE.get(plate_n) if plate_n else "") or ""
    msg = str(msg).strip()
    if msg:
        try:
            import hashlib
            key = "driver_message:" + hashlib.sha1(msg.encode("utf-8", "ignore")).hexdigest()[:12]
        except Exception:
            key = "driver_message"
        return {"status_key": key, "status_text": msg, "report_in_office_at": ""}

    # Departed override (after manual status)
    departed = m.get("departed", False)
    if isinstance(departed, str):
        departed = departed.strip().lower() in {"1", "true", "yes", "y"}
    if not departed:
        departed = _has(m.get("departed_at", ""))
    if departed:
        return {"status_key": "DEPARTED", "status_text": "Drive safe, we wait you back!", "report_in_office_at": ""}

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
        report_at = _format_dt_like(ra, sched_raw)

    return {
        "status_key": key,
        "status_text": msg,
        "report_in_office_at": report_at,
    }


def destination_nav_url(lat: Optional[float], lon: Optional[float], fallback_text: str = "") -> Optional[str]:
    """Return a Google Maps navigation URL.
    - Prefer coordinates (lat/lon) if available.
    - Fallback to destination text search if coordinates are missing.
    """
    try:
        if lat is not None and lon is not None:
            latf = float(lat)
            lonf = float(lon)
            return f"https://www.google.com/maps/dir/?api=1&destination={latf},{lonf}&travelmode=driving"

        fb = (fallback_text or "").strip()
        if fb:
            return f"https://www.google.com/maps/dir/?api=1&destination={urllib.parse.quote(fb)}&travelmode=driving"
        return None
    except Exception:
        return None


def _fetch_ors_route_coords(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
) -> Optional[List[Tuple[float, float]]]:
    """
    Return route coordinates as (lat, lon) pairs using OpenRouteService.
    Returns None if ORS is not configured or on any failure.
    """
    key = (ORS_API_KEY or "").strip()
    if not key:
        return None

    try:
        qs = urllib.parse.urlencode({
            "start": f"{origin_lon:.6f},{origin_lat:.6f}",
            "end": f"{dest_lon:.6f},{dest_lat:.6f}",
        })
        url = f"{ORS_DIRECTIONS_URL}?{qs}"

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": key,
                "Accept": "application/json",
            },
            method="GET",
        )

        with urllib.request.urlopen(req, timeout=7) as resp:
            raw = resp.read()

        data = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
        feats = data.get("features") or []
        if not feats:
            return None

        coords = (feats[0].get("geometry") or {}).get("coordinates") or []
        if not coords:
            return None

        # coords are [lon, lat]
        pts = [(float(lat), float(lon)) for lon, lat in coords]

        # Downsample if extremely dense (keep max ~1200 points)
        if len(pts) > 1200:
            step = int(math.ceil(len(pts) / 1200.0))
            pts = pts[::step]
            if pts and pts[-1] != (float(dest_lat), float(dest_lon)):
                pts.append((float(dest_lat), float(dest_lon)))

        return pts
    except Exception:
        return None

def _fetch_osrm_route_coords(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
) -> Optional[List[Tuple[float, float]]]:
    """
    Return route coordinates as (lat, lon) pairs using OSRM (public demo).
    This does NOT require an API key.
    Returns None on any failure.
    """
    try:
        base = (os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org") or "").strip().rstrip("/")
        if not base:
            base = "https://router.project-osrm.org"

        url = (
            f"{base}/route/v1/driving/"
            f"{origin_lon:.6f},{origin_lat:.6f};{dest_lon:.6f},{dest_lat:.6f}"
            f"?overview=full&geometries=geojson"
        )

        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "DriverStatus/1.0",
            },
            method="GET",
        )

        with urllib.request.urlopen(req, timeout=7) as resp:
            raw = resp.read()

        data = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
        routes = data.get("routes") or []
        if not routes:
            return None

        geom = routes[0].get("geometry") or {}
        coords = geom.get("coordinates") or []
        if not coords:
            return None

        # coords are [lon, lat]
        pts = [(float(lat), float(lon)) for lon, lat in coords]

        # Downsample if extremely dense (keep max ~1200 points)
        if len(pts) > 1200:
            step = int(math.ceil(len(pts) / 1200.0))
            pts = pts[::step]
            if pts and pts[-1] != (float(dest_lat), float(dest_lon)):
                pts.append((float(dest_lat), float(dest_lon)))

        return pts
    except Exception:
        return None

def build_route_points(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
) -> Tuple[List[List[float]], str]:
    """Return polyline as [[lat, lon], ...] and a short note."""
    pts = _fetch_ors_route_coords(origin_lat, origin_lon, dest_lat, dest_lon)
    if pts:
        return [[lat, lon] for (lat, lon) in pts], "Route source: OpenRouteService"

    pts2 = _fetch_osrm_route_coords(origin_lat, origin_lon, dest_lat, dest_lon)
    if pts2:
        return [[lat, lon] for (lat, lon) in pts2], "Route source: OSRM"

    return [
        [float(origin_lat), float(origin_lon)],
        [float(dest_lat), float(dest_lon)],
    ], "Route source: direct line"


def _snapshot_movements() -> List[Dict[str, Any]]:
    """Return a sanitized list of movement dicts from SNAPSHOT['movements'].

    Accepts either:
      - list[dict]
      - dict[Any, dict] (values will be used)
    Any non-dict items are ignored.
    """
    if not SNAPSHOT or not isinstance(SNAPSHOT, dict):
        return []

    moves = SNAPSHOT.get("movements")
    if isinstance(moves, dict):
        moves = list(moves.values())

    if not isinstance(moves, list):
        return []

    out: List[Dict[str, Any]] = []
    for m in moves:
        if isinstance(m, dict):
            out.append(m)
    return out


def _get_plate_record(plate: str) -> Optional[Dict[str, Any]]:
    moves = _snapshot_movements()
    if not moves:
        return None

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
    payload = json.dumps({"title": title, "body": body, "url": f"/?plate={urllib.parse.quote(plate)}"})

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
            pass

    SUBSCRIPTIONS_BY_PLATE[plate] = alive


# -----------------------------
# Excel lookup loading (server-side destination calc)
# -----------------------------
def _clean_header(v: Any) -> str:
    s = str(v or "").strip().lower()
    for ch in [" ", "-", "_", "/", "\\", "(", ")", "[", "]", "{", "}", ".", ",", ":"]:
        s = s.replace(ch, "")
    return s


def _find_col(headers: List[str], candidates: List[str]) -> Optional[int]:
    # exact
    for c in candidates:
        if c in headers:
            return headers.index(c)
    # contains
    for i, h in enumerate(headers):
        for c in candidates:
            if c in h:
                return i
    return None


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "nat"}:
            return None
        return float(s)
    except Exception:
        return None


def _load_xlsx_map_locations(path: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not _OPENPYXL_OK or not os.path.exists(path):
        return out

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)  # type: ignore
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header_row = next(rows, None)
    if not header_row:
        return out

    headers = [_clean_header(h) for h in header_row]

    code_i = _find_col(headers, ["dest", "code", "locationcode", "loccode", "stationcode", "facilitycode", "destcode"])
    city_i = _find_col(headers, ["city", "town", "name", "locationname"])
    country_i = _find_col(headers, ["country", "land"])
    lat_i = _find_col(headers, ["lat", "latitude"])
    lon_i = _find_col(headers, ["lon", "lng", "long", "longitude"])

    if code_i is None:
        return out

    for r in rows:
        try:
            code = _norm_code(r[code_i] if code_i < len(r) else "")
            if not code:
                continue

            city = str(r[city_i]).strip() if (city_i is not None and city_i < len(r) and r[city_i] is not None) else ""
            country = str(r[country_i]).strip() if (country_i is not None and country_i < len(r) and r[country_i] is not None) else ""

            lat = _safe_float(r[lat_i] if (lat_i is not None and lat_i < len(r)) else None)
            lon = _safe_float(r[lon_i] if (lon_i is not None and lon_i < len(r)) else None)

            out[code] = {"code": code, "city": city, "country": country, "lat": lat, "lon": lon}
        except Exception:
            continue

    return out


def _load_xlsx_map_destland(path: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not _OPENPYXL_OK or not os.path.exists(path):
        return out

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)  # type: ignore
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header_row = next(rows, None)
    if not header_row:
        return out

    headers = [_clean_header(h) for h in header_row]
    code_i = _find_col(headers, ["dest", "code", "locationcode", "loccode", "stationcode", "facilitycode", "destcode"])
    city_i = _find_col(headers, ["city", "town", "name", "locationname"])
    country_i = _find_col(headers, ["country", "land"])

    if code_i is None:
        return out

    for r in rows:
        try:
            code = _norm_code(r[code_i] if code_i < len(r) else "")
            if not code:
                continue

            city = str(r[city_i]).strip() if (city_i is not None and city_i < len(r) and r[city_i] is not None) else ""
            country = str(r[country_i]).strip() if (country_i is not None and country_i < len(r) and r[country_i] is not None) else ""

            out[code] = {"code": code, "city": city, "country": country}
        except Exception:
            continue

    return out


def _load_destination_lookups() -> None:
    global LOCATION_BY_CODE, DESTLAND_BY_CODE
    try:
        LOCATION_BY_CODE = _load_xlsx_map_locations(LOCATIONS_XLSX)
    except Exception:
        LOCATION_BY_CODE = {}

    try:
        DESTLAND_BY_CODE = _load_xlsx_map_destland(DEST_LAND_XLSX)
    except Exception:
        DESTLAND_BY_CODE = {}


def _extract_code_from_text(v: Any) -> str:
    s = str(v or "").strip().upper()
    if not s or s.lower() in {"nan", "none", "nat"}:
        return ""

    # If it ends like "... (QAR)" take inside ()
    if "(" in s and s.endswith(")"):
        inside = s.split("(")[-1].replace(")", "").strip()
        inside = _norm_code(inside)
        if 2 <= len(inside) <= 10:
            return inside

    # If the whole thing looks like a code
    compact = _norm_code(s)
    if 2 <= len(compact) <= 10 and any(ch.isalpha() for ch in compact):
        return compact

    # Otherwise take last token if it looks like a code
    parts = [p for p in s.replace(",", " ").replace("/", " ").split() if p]
    if parts:
        last = _norm_code(parts[-1])
        if 2 <= len(last) <= 10 and any(ch.isalpha() for ch in last):
            return last

    return ""


def _first_nonempty(rec: Dict[str, Any], keys: List[str]) -> Any:
    for k in keys:
        if k in rec and _has(rec.get(k)):
            return rec.get(k)
    return None


def resolve_destination(rec: Dict[str, Any]) -> Tuple[str, Optional[float], Optional[float]]:
    """
    Returns: (destination_text, lat, lon)
    destination_text should be: "City, Country (CODE)" when possible.
    """

    # 1) Determine destination code from the snapshot (many possible field names)
    raw_code = _first_nonempty(rec, [
        "dest_code", "DestCode", "DEST_CODE",
        "destination_code", "DestinationCode", "DESTINATION_CODE",
        "dest", "Dest", "DEST",
        "destination", "Destination",
        "destination_text", "DestinationText",
        "dest_text", "DestText", "DEST_TEXT",
    ])

    code = _extract_code_from_text(raw_code)
    code_n = _norm_code(code)

    # 2) Coordinates: prefer snapshot coordinates if provided, otherwise lookup
    lat = _safe_float(_first_nonempty(rec, ["dest_lat", "DestLat", "destination_lat", "DestinationLat", "lat_dest", "LatDest"]))
    lon = _safe_float(_first_nonempty(rec, ["dest_lon", "DestLon", "destination_lon", "DestinationLon", "lon_dest", "LonDest"]))

    city = ""
    country = ""

    # 3) Lookups
    loc_row = LOCATION_BY_CODE.get(code_n) if code_n else None
    dl_row = DESTLAND_BY_CODE.get(code_n) if code_n else None

    # Coordinates: best source is FedEx_locations.xlsx
    if loc_row:
        if lat is None:
            lat = loc_row.get("lat")
        if lon is None:
            lon = loc_row.get("lon")

    # City/Country: prefer dest-land.xlsx because it contains clean city names
    if dl_row:
        city = str(dl_row.get("city") or "").strip()
        country = str(dl_row.get("country") or "").strip()

    # Fallback for city/country (if dest-land missing)
    if loc_row:
        if not city:
            city = str(loc_row.get("city") or "").strip()
            # common pattern: "ARH Depot Elst" -> remove leading "ARH "
            if code_n and city.upper().startswith(code_n + " "):
                city = city[len(code_n) + 1:].strip()
        if not country:
            country = str(loc_row.get("country") or "").strip()

    # 5) Build display text
    if city and country and code_n:
        dest_text = f"{city}, {country} ({code_n})"
    elif city and country:
        dest_text = f"{city}, {country}"
    elif code_n:
        dest_text = code_n
    else:
        # absolute fallback: keep whatever came from snapshot
        dest_text = str(_first_nonempty(rec, ["destination_text", "dest_text", "destination", "Destination"]) or "-")

    return dest_text, lat, lon


# -----------------------------
# API
# -----------------------------
@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "push_enabled": PUSH_ENABLED,
        "snapshot_loaded": bool(SNAPSHOT),
        "lookup_locations_loaded": len(LOCATION_BY_CODE),
        "lookup_destland_loaded": len(DESTLAND_BY_CODE),
        "openpyxl_ok": _OPENPYXL_OK,
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

    # Normalize movements to a list[dict] (client might send a dict/map)
    try:
        SNAPSHOT["movements"] = _snapshot_movements()
    except Exception:
        SNAPSHOT["movements"] = []

    # Push notifications on status change (best-effort)
    if PUSH_ENABLED:
        try:
            for m in _snapshot_movements():
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

    return {"ok": True, "count": len(_snapshot_movements()), "push_enabled": PUSH_ENABLED}

@app.post("/api/driver_message")
async def driver_message(request: Request, secret: str = Query(..., min_length=8)) -> Dict[str, Any]:
    global LAST_STATUS_KEY_BY_PLATE

    if not ADMIN_UPLOAD_SECRET:
        raise HTTPException(status_code=500, detail="Server not configured: ADMIN_UPLOAD_SECRET missing.")
    if secret != ADMIN_UPLOAD_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized.")

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    plate = normalize_plate(str(body.get("plate", "")))
    message = str(body.get("message", "") or "").strip()

    if not plate:
        raise HTTPException(status_code=400, detail="Missing 'plate'.")
    if not message:
        raise HTTPException(status_code=400, detail="Missing 'message'.")

    # Save manual message
    MANUAL_STATUS_BY_PLATE[plate] = message

    # Force immediate push + update last key
    st = compute_driver_status({"license_plate": plate})
    try:
        LAST_STATUS_KEY_BY_PLATE[plate] = st["status_key"]
    except Exception:
        pass

    _push_to_plate(plate, "Message from dispatch", message)

    return {"ok": True, "plate": plate, "message": message}


@app.get("/api/status")
def get_status(
    plate: str = Query(..., min_length=2),
    lat: float = Query(...),
    lon: float = Query(...),
    ts: int = Query(..., description="Unix epoch seconds from the device"),
) -> Dict[str, Any]:
    # Enforce geofence, but we do NOT return geofence data anymore
    geofence_check(lat, lon, ts)

    rec = _get_plate_record(plate)
    if rec is None:
        return {
            "plate": normalize_plate(plate),
            "found": False,
            "last_refresh": (SNAPSHOT or {}).get("last_update"),
        }

    st = compute_driver_status(rec)

    dest_text, dlat, dlon = resolve_destination(rec)
    nav = destination_nav_url(dlat, dlon, dest_text)

    sched_raw = rec.get("scheduled_departure") or ""
    sched_disp = _format_scheduled_departure(sched_raw)


    # Mark that this plate was checked on the website (used by desktop for 👁 icon)
    try:
        p = normalize_plate(plate)
        prev = VIEWED_BY_PLATE.get(p) or {}
        VIEWED_BY_PLATE[p] = {
            "count": int(prev.get("count", 0)) + 1,
            "last_view": datetime.utcnow().isoformat() + "Z",
        }
    except Exception:
        pass

    return {
        "plate": normalize_plate(plate),
        "found": True,
        "status_key": st["status_key"],
        "status_text": st["status_text"],
        "destination_text": dest_text,
        "destination_nav_url": nav,
        "scheduled_departure": sched_disp,
        "report_in_office_at": st["report_in_office_at"],
        "trailer": rec.get("trailer") or "",
        "location": rec.get("location") or "",
        "last_refresh": (SNAPSHOT or {}).get("last_update"),
        "push_enabled": PUSH_ENABLED,
        "vapid_public_key": VAPID_PUBLIC_KEY if PUSH_ENABLED else "",
    }


@app.get("/api/admin/plate_flags")
def get_plate_flags(
    secret: str = Query(..., min_length=8),
    plates: str = Query("", description="Comma-separated list of plates"),
) -> Dict[str, Any]:
    if not ADMIN_UPLOAD_SECRET:
        raise HTTPException(status_code=500, detail="Server not configured: ADMIN_UPLOAD_SECRET missing.")
    if secret != ADMIN_UPLOAD_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized.")

    out: Dict[str, Any] = {}
    raw = (plates or "").strip()
    plate_list = [p for p in (raw.split(",") if raw else []) if p.strip()]

    for p in plate_list:
        np = normalize_plate(p)
        v = VIEWED_BY_PLATE.get(np) or {}
        out[np] = {
            "viewed": bool(v),
            "last_view": v.get("last_view", "") if isinstance(v, dict) else "",
            "count": int(v.get("count", 0)) if isinstance(v, dict) else 0,
            "push_enabled": bool(SUBSCRIPTIONS_BY_PLATE.get(np)),
        }

    return {"ok": True, "plates": out}


@app.get("/api/route")
def get_route(
    plate: str = Query(..., min_length=2),
    lat: float = Query(...),
    lon: float = Query(...),
    ts: int = Query(..., description="Unix epoch seconds from the device"),
) -> Dict[str, Any]:
    """Return a zoomable route map polyline for the website."""
    geofence_check(lat, lon, ts)

    rec = _get_plate_record(plate)
    if rec is None:
        raise HTTPException(status_code=404, detail="No movement found for this plate.")

    dest_text, dlat, dlon = resolve_destination(rec)
    if dlat is None or dlon is None:
        raise HTTPException(status_code=404, detail="Destination coordinates not available for this movement.")

    origin_lat = float(HUB_LAT)
    origin_lon = float(HUB_LON)
    dest_lat = float(dlat)
    dest_lon = float(dlon)

    route_pts, note = build_route_points(origin_lat, origin_lon, dest_lat, dest_lon)

    return {
        "plate": normalize_plate(plate),
        "origin": {"lat": origin_lat, "lon": origin_lon},
        "dest": {"lat": dest_lat, "lon": dest_lon},
        "route": route_pts,
        "note": note,
        "destination_text": dest_text,
        "last_refresh": (SNAPSHOT or {}).get("last_update"),
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


# -----------------------------
# Website (no geofence shown + background image + destination is server-calculated)
# -----------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Driver Status</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    body {
      font-family: Arial, sans-serif;
      margin: 0;
      min-height: 100vh;
      background: url('/static/bg.png') no-repeat center center fixed;
      background-size: cover;
    }
    .wrap {
      max-width: 720px;
      margin: 0 auto;
      padding: 18px 16px 24px;
    }
    .topcard {
      background: rgba(255,255,255,0.45);
      border-radius: 16px;
      padding: 14px;
      border: 1px solid rgba(0,0,0,0.08);
      box-shadow: 0 10px 30px rgba(0,0,0,0.10);
    }
    input {
      font-size: 16px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid rgba(0,0,0,0.18);
      background: rgba(255,255,255,0.60);
      outline: none;
    }
    input:focus {
      border-color: rgba(77,20,140,0.55);
      box-shadow: 0 0 0 3px rgba(77,20,140,0.18);
    }

    .btn {
      font-size: 16px;
      padding: 10px 16px;
      border-radius: 12px;
      border: none;
      cursor: pointer;
      font-weight: 700;
      letter-spacing: 0.2px;
      box-shadow: 0 10px 18px rgba(0,0,0,0.18);
      transition: transform 0.08s ease, filter 0.15s ease, box-shadow 0.15s ease;
      user-select: none;
      -webkit-tap-highlight-color: transparent;
    }
    .btn:hover {
      filter: brightness(1.08);
      transform: translateY(-1px);
      box-shadow: 0 12px 22px rgba(0,0,0,0.22);
    }
    .btn:active {
      transform: translateY(0px);
      filter: brightness(0.98);
      box-shadow: 0 8px 14px rgba(0,0,0,0.18);
    }
    .btn:focus-visible {
      outline: none;
      box-shadow: 0 0 0 3px rgba(77,20,140,0.24), 0 10px 18px rgba(0,0,0,0.18);
    }

    .btn-primary {
      color: #ffffff;
      background: linear-gradient(180deg, rgba(104,68,232,1) 0%, rgba(60,32,170,1) 100%);
    }

    .btn-secondary {
      color: rgba(25,25,35,1);
      background: linear-gradient(180deg, rgba(245,245,255,0.70) 0%, rgba(220,220,235,0.45) 100%);
      border: 1px solid rgba(255,255,255,0.35);
    }
    .row { display: flex; gap: 8px; }
    .row > * { flex: 1; }
    .row-main > input { flex: 1 1 auto; }
    .row-main > button { flex: 0 0 120px; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 14px; margin-top: 12px; background: rgba(255,255,255,0.45); }
    .muted { color: #666; }
    .status-big { font-size: 22px; font-weight: 700; line-height: 1.25; }
    .ok { border-color: #bfe6c3; }
    .warn { border-color: #ffd18a; }
    .err { border-color: #f5b5b5; }

    #map {
      height: 320px;
      width: 100%;
      border-radius: 12px;
      border: 1px solid rgba(0,0,0,0.18);
      overflow: hidden;
      background: rgba(255,255,255,0.35);
    }

    a { color: inherit; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topcard">
      <h2 style="margin: 6px 0 12px;">Movement status by license plate</h2>

      <div class="row row-main">
        <input id="plate" placeholder="Enter license plate (e.g. AB-123-CD)" />
        <button id="btn" class="btn btn-primary">Check</button>
      </div>

      <div class="row" style="margin-top: 8px;">
        <button id="btnNotify" class="btn btn-secondary" style="display:none;">Enable notifications</button>
      </div>

      <div id="out" class="card" style="display:none;"></div>
    </div>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

  <script>
    const API_BASE = window.location.origin;

    function normalizePlate(v) {
      return (v || "").toUpperCase().trim().replaceAll(" ", "").replaceAll("-", "");
    }

    function getInitialPlate() {
      try {
        const p = new URLSearchParams(window.location.search).get("plate") || "";
        const pn = normalizePlate(p);
        if (pn) return pn;
      } catch (e) {}
      try {
        return normalizePlate(localStorage.getItem("last_plate") || "");
      } catch (e) {
        return "";
      }
    }

    function setCurrentPlate(p) {
      const pn = normalizePlate(p);
      if (!pn) return;
      try { localStorage.setItem("last_plate", pn); } catch (e) {}
      try {
        const u = new URL(window.location.href);
        u.searchParams.set("plate", pn);
        history.replaceState(null, "", u.toString());
      } catch (e) {}
    }

    async function readJsonOrText(res) {
      const ct = (res.headers.get("content-type") || "").toLowerCase();
      if (ct.includes("application/json")) {
        return await res.json();
      }
      const txt = await res.text();
      return { detail: txt };
    }

    let _map = null;
    let _routeLine = null;

    function destroyMap() {
      try {
        if (_map) _map.remove();
      } catch (e) {}
      _map = null;
      _routeLine = null;
    }

    function setMapNote(msg, isErr) {
      const el = document.getElementById("mapNote");
      if (!el) return;
      el.textContent = msg || "";
      el.style.color = isErr ? "#a00000" : "";
    }

    async function renderRouteMap(plate, loc) {
      const mapDiv = document.getElementById("map");
      if (!mapDiv || typeof L === "undefined") return;

      destroyMap();

      _map = L.map("map", { zoomControl: true, scrollWheelZoom: true });
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap contributors",
      }).addTo(_map);

      setMapNote("Loading route…", false);

      try {
        const url = `${API_BASE}/api/route?plate=${encodeURIComponent(plate)}&lat=${encodeURIComponent(loc.lat)}&lon=${encodeURIComponent(loc.lon)}&ts=${encodeURIComponent(loc.ts)}`;
        const res = await fetch(url);
        const data = await readJsonOrText(res);

        if (!res.ok) {
          setMapNote(data.detail || res.statusText, true);
          _map.setView([loc.lat, loc.lon], 10);
          setTimeout(() => { if (_map) _map.invalidateSize(); }, 80);
          return;
        }

        const pts = data.route || [];
        if (pts.length >= 2) {
          _routeLine = L.polyline(pts, { color: "#4D148C", weight: 4, opacity: 0.9 }).addTo(_map);
          _map.fitBounds(_routeLine.getBounds(), { padding: [12, 12] });
        } else {
          _map.setView([loc.lat, loc.lon], 10);
        }

        if (data.origin && data.origin.lat != null && data.origin.lon != null) {
          L.marker([data.origin.lat, data.origin.lon]).addTo(_map).bindPopup("Origin");
        }
        if (data.dest && data.dest.lat != null && data.dest.lon != null) {
          L.marker([data.dest.lat, data.dest.lon]).addTo(_map).bindPopup("Destination");
        }

        setMapNote(data.note || "", false);
        setTimeout(() => { if (_map) _map.invalidateSize(); }, 80);
      } catch (e) {
        setMapNote("Route error: " + e, true);
        try { _map.setView([loc.lat, loc.lon], 10); } catch (e2) {}
        setTimeout(() => { if (_map) _map.invalidateSize(); }, 80);
      }
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

      setCurrentPlate(plate);

      destroyMap();

      show(`<div class="muted">Getting location…</div>`);

      let loc;
      try {
        loc = await getLocation();
      } catch (e) {
        destroyMap();
        show(`<b>Location error:</b> ${e.message}<div class="muted">Enable GPS and allow location permission.</div>`, "err");
        return;
      }

      show(`<div class="muted">Loading status…</div>`);

      try {
        const url = `${API_BASE}/api/status?plate=${encodeURIComponent(plate)}&lat=${encodeURIComponent(loc.lat)}&lon=${encodeURIComponent(loc.lon)}&ts=${encodeURIComponent(loc.ts)}`;
        const res = await fetch(url);
        const data = await readJsonOrText(res);

        if (!res.ok) {
          destroyMap();
          show(`<b>Error:</b> ${data.detail || res.statusText}`, "err");
          document.getElementById("btnNotify").style.display = "none";
          return;
        }

        const last = data.last_refresh || "-";

        if (!data.found) {
          destroyMap();
          show(`
            <div class="status-big">No movement found</div>
            <div class="muted">Last refresh: ${last}</div>
          `, "warn");
          document.getElementById("btnNotify").style.display = "none";
          return;
        }

        const destText = data.destination_text || "-";
        const destLink = data.destination_nav_url ? `<a href="${data.destination_nav_url}" target="_blank" rel="noopener">${destText}</a>` : destText;

        const trailerVal = (data.trailer || "").trim();
        const locVal = (data.location || "").trim();

        let extraTrailerPlace = "";
        if (locVal) {
          let placeText = locVal;

          // If location begins with "P" → show "Parking <number>"
          if (/^[Pp]/.test(locVal)) {
            let rest = locVal.slice(1).trim();

            // Prefer digits (e.g. "P12" / "P 012")
            const digits = (rest.match(/\d+/g) || []).join("");
            if (digits) {
              const num = digits.replace(/^0+/, "") || "0";
              placeText = `Parking ${num}`;
            } else if (rest) {
              placeText = `Parking ${rest}`;
            } else {
              placeText = "Parking";
            }
          }
          // If location begins with a number → show "Dock <location>"
          else if (/^\d/.test(locVal)) {
            placeText = `Dock ${locVal}`;
          }

          extraTrailerPlace = `
          <div style="margin-top:6px;"><b>Trailer:</b> ${trailerVal || "-"}</div>
          <div><b>Place:</b> ${placeText}</div>`;
        }

        show(`
          <div class="status-big">"${data.status_text}"</div>
          <hr style="border:none;border-top:1px solid #ddd;margin:12px 0;">
          <div><b>Destination:</b> ${destLink}</div>
          <div><b>Departure time:</b> ${data.scheduled_departure || "-"}</div>
          <div><b>Report in the office:</b> ${data.report_in_office_at || "-"}</div>
          ${extraTrailerPlace}
          <div class="muted" style="margin-top:8px;">Last refresh: ${last}</div>

          <div style="margin-top:12px;"><b>Route map:</b></div>
          <div id="map"></div>
          <div id="mapNote" class="muted" style="margin-top:6px;"></div>
        `, "ok");

        setTimeout(() => renderRouteMap(plate, loc), 0);

        if (data.push_enabled && data.vapid_public_key) {
          const bn = document.getElementById("btnNotify");
          bn.style.display = "block";
          bn.onclick = () => enableNotifications(plate, loc, data.vapid_public_key);
        } else {
          document.getElementById("btnNotify").style.display = "none";
        }

      } catch (e) {
        destroyMap();
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

        // Wait until the service worker is activated (otherwise PushManager.subscribe can fail with "no active Service Worker")
        const sw = reg.installing || reg.waiting || reg.active;
        if (!sw) throw new Error("Service Worker registration failed.");
        await new Promise((resolve, reject) => {
          if (sw.state === 'activated') return resolve();
          const t = setTimeout(() => reject(new Error("Service Worker activation timeout.")), 8000);
          sw.addEventListener('statechange', () => {
            if (sw.state === 'activated') {
              clearTimeout(t);
              resolve();
            }
          });
        });

        const existing = await reg.pushManager.getSubscription();
        const sub = existing || await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
        });

        const resp = await fetch(`${API_BASE}/api/subscribe?plate=${encodeURIComponent(plate)}&lat=${encodeURIComponent(loc.lat)}&lon=${encodeURIComponent(loc.lon)}&ts=${encodeURIComponent(loc.ts)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(sub),
        });

        const data = await readJsonOrText(resp);
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

    // Restore plate from URL or last usage and auto-run once
    (function initPlate() {
      const p = getInitialPlate();
      if (p) {
        document.getElementById("plate").value = p;
        setTimeout(() => { checkStatus(); }, 50);
      }
    })();
  </script>
</body>
</html>"""


SERVICE_WORKER_JS = r"""
self.addEventListener('install', function(event) {
  // Activate immediately on first load
  self.skipWaiting();
});

self.addEventListener('activate', function(event) {
  // Take control without requiring a reload
  event.waitUntil(clients.claim());
});

self.addEventListener('push', function(event) {
  let data = {};
  try { data = event.data.json(); } catch (e) { data = { title: 'Update', body: event.data && event.data.text() }; }
  const title = data.title || 'Status update';
  const options = { body: data.body || '', data: { url: (data.url || '/') } };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  const url = (event.notification && event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.openWindow(url));
});
"""
