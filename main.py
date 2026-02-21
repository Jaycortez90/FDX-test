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
# Live Traffic (optional) - HERE Routing v8 (server-side)
# =============================
HERE_API_KEY = os.environ.get("HERE_API_KEY", "").strip()
HERE_ROUTING_URL = "https://router.hereapi.com/v8/routes"

# In-memory cache for traffic delay (Render restarts will clear these)
_TRAFFIC_CACHE: Dict[Tuple[float, float, float, float, str], Tuple[float, Dict[str, Any]]] = {}
_TRAFFIC_TTL_SEC = 90

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
                            _push_status_change_to_plate(plate, m)
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


def _clean_location_value(v: Any) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    if s.lower() == "wait":
        return ""
    return s


def _traffic_cache_get(key: Tuple[float, float, float, float, str]) -> Optional[Dict[str, Any]]:
    item = _TRAFFIC_CACHE.get(key)
    if not item:
        return None
    ts, payload = item
    if (time.time() - float(ts)) > float(_TRAFFIC_TTL_SEC):
        _TRAFFIC_CACHE.pop(key, None)
        return None
    return payload


def _traffic_cache_set(key: Tuple[float, float, float, float, str], payload: Dict[str, Any]) -> None:
    _TRAFFIC_CACHE[key] = (time.time(), payload)


def _here_fetch_delay_minutes(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    departure_iso: str = "",
) -> Tuple[Optional[int], Optional[str]]:
    """Return (delay_minutes, error). delay_minutes is >=0 on success."""
    key = (HERE_API_KEY or "").strip()
    if not key:
        return None, "HERE_API_KEY missing on server"

    params = {
        "transportMode": "truck",
        "origin": f"{origin_lat:.6f},{origin_lon:.6f}",
        "destination": f"{dest_lat:.6f},{dest_lon:.6f}",
        "return": "summary",
        "apiKey": key,
    }
    if departure_iso:
        params["departureTime"] = departure_iso

    try:
        qs = urllib.parse.urlencode(params)
        url = f"{HERE_ROUTING_URL}?{qs}"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "DriverStatus/TrafficDelay"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()

        data = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
        routes = data.get("routes") or []
        if not routes:
            return None, "HERE: no routes"
        sections = routes[0].get("sections") or []
        if not sections:
            return None, "HERE: no sections"
        summary = sections[0].get("summary") or {}

        base_sec = summary.get("baseDuration")
        traffic_sec = summary.get("duration")
        if base_sec is None or traffic_sec is None:
            return None, "HERE: missing duration fields"

        delay_sec = max(0, int(traffic_sec) - int(base_sec))
        delay_min = int(round(delay_sec / 60.0))
        if delay_min < 0:
            delay_min = 0
        return delay_min, None
    except Exception as e:
        # If the request is rejected, HERE often returns JSON with 'title'/'message',
        # but urllib raises HTTPError; keep it simple.
        return None, f"HERE request failed ({type(e).__name__})"

SUPPORTED_LANGS = {"en", "de", "nl", "es", "it", "ro", "ru", "lt", "kk", "hi", "pl", "hu", "uz", "tg", "ky", "be"}

def normalize_lang(value: Any) -> str:
    """Return one of: en, de, nl, es, it, ro, ru, lt, kk, hi, pl, hu, uz, tg, ky, be."""
    s = str(value or "").strip().lower()
    if not s:
        return "en"
    # normalize common forms: en-US, de_DE, etc.
    s = s.replace("_", "-")
    base = s.split("-", 1)[0]
    if base in SUPPORTED_LANGS:
        return base
    # allow common aliases
    if base in {"eng"}:
        return "en"
    if base in {"ger", "deu"}:
        return "de"
    if base in {"dut", "nld"}:
        return "nl"
    if base in {"rus"}:
        return "ru"
    if base in {"lit"}:
        return "lt"
    if base in {"kaz", "kz"}:
        return "kk"
    if base in {"hin"}:
        return "hi"
    if base in {"pol"}:
        return "pl"
    if base in {"hun"}:
        return "hu"
    if base in {"uzb"}:
        return "uz"
    if base in {"tgk", "taj", "tj"}:
        return "tg"
    if base in {"kir", "kg"}:
        return "ky"
    if base in {"bel", "by"}:
        return "be"

    if base in {"spa", "esp"}:
        return "es"
    if base in {"ita"}:
        return "it"
    if base in {"rom", "ron", "rum"}:
        return "ro"

    return "en"


_I18N_STATUS: Dict[str, Dict[str, str]] = {
    "en": {
        "DEPARTED": "Drive safe, we wait you back!",
        "LOCATION_WITH_TRAILER": "Please connect the {trailer} trailer on location: {location} and pick up the CMR documents in the office!",
        "LOCATION_NO_TRAILER": "Please connect the trailer on location: {location} and pick up the CMR documents in the office!",
        "CLOSEDOOR_NO_LOCATION": "Your trailer is ready, please report in the office for further information!",
        "LOADING_WAIT": "Your trailer is being loaded, please wait!",
        "REPORT_OFFICE": "Please report in the office!",
    },
    "de": {
        "DEPARTED": "Fahren Sie vorsichtig – wir erwarten Sie zurück!",
        "LOCATION_WITH_TRAILER": "Bitte koppeln Sie den Anhänger {trailer} am Standort: {location} an und holen Sie die CMR-Dokumente im Büro ab!",
        "LOCATION_NO_TRAILER": "Bitte koppeln Sie den Anhänger am Standort: {location} an und holen Sie die CMR-Dokumente im Büro ab!",
        "CLOSEDOOR_NO_LOCATION": "Ihr Anhänger ist fertig. Bitte melden Sie sich im Büro für weitere Informationen!",
        "LOADING_WAIT": "Ihr Anhänger wird beladen – bitte warten!",
        "REPORT_OFFICE": "Bitte melden Sie sich im Büro!",
    },
    "nl": {
        "DEPARTED": "Rij veilig – we wachten op je terugkeer!",
        "LOCATION_WITH_TRAILER": "Koppel de trailer {trailer} op locatie: {location} en haal de CMR-documenten op in het kantoor!",
        "LOCATION_NO_TRAILER": "Koppel de trailer op locatie: {location} en haal de CMR-documenten op in het kantoor!",
        "CLOSEDOOR_NO_LOCATION": "Je trailer is gereed. Meld je in het kantoor voor verdere informatie!",
        "LOADING_WAIT": "Je trailer wordt geladen – even wachten!",
        "REPORT_OFFICE": "Meld je in het kantoor!",
    },

    "es": {
        "DEPARTED": "Buen viaje — ¡te esperamos de vuelta!",
        "LOCATION_WITH_TRAILER": "Por favor conecta el remolque {trailer} en la ubicación: {location} y recoge los documentos CMR en la oficina!",
        "LOCATION_NO_TRAILER": "Por favor conecta el remolque en la ubicación: {location} y recoge los documentos CMR en la oficina!",
        "CLOSEDOOR_NO_LOCATION": "Tu remolque está listo. Preséntate en la oficina para más información!",
        "LOADING_WAIT": "Tu remolque se está cargando — espera, por favor!",
        "REPORT_OFFICE": "¡Preséntate en la oficina!",
    },
    "it": {
        "DEPARTED": "Buon viaggio — ti aspettiamo di nuovo!",
        "LOCATION_WITH_TRAILER": "Collega il rimorchio {trailer} alla posizione: {location} e ritira i documenti CMR in ufficio!",
        "LOCATION_NO_TRAILER": "Collega il rimorchio alla posizione: {location} e ritira i documenti CMR in ufficio!",
        "CLOSEDOOR_NO_LOCATION": "Il tuo rimorchio è pronto. Presentati in ufficio per ulteriori informazioni!",
        "LOADING_WAIT": "Il tuo rimorchio è in carico — attendi!",
        "REPORT_OFFICE": "Presentati in ufficio!",
    },
    "ro": {
        "DEPARTED": "Drum bun — te așteptăm înapoi!",
        "LOCATION_WITH_TRAILER": "Vă rugăm să conectați remorca {trailer} la locația: {location} și să ridicați documentele CMR din birou!",
        "LOCATION_NO_TRAILER": "Vă rugăm să conectați remorca la locația: {location} și să ridicați documentele CMR din birou!",
        "CLOSEDOOR_NO_LOCATION": "Remorca ta este gata. Te rugăm să te prezinți la birou pentru informații suplimentare!",
        "LOADING_WAIT": "Remorca ta este în curs de încărcare — te rugăm să aștepți!",
        "REPORT_OFFICE": "Te rugăm să te prezinți la birou!",
    },
    "ru": {
        "DEPARTED": "Счастливого пути — ждём вас обратно!",
        "LOCATION_WITH_TRAILER": "Пожалуйста, прицепите прицеп {trailer} на месте: {location} и заберите документы CMR в офисе!",
        "LOCATION_NO_TRAILER": "Пожалуйста, прицепите прицеп на месте: {location} и заберите документы CMR в офисе!",
        "CLOSEDOOR_NO_LOCATION": "Ваш прицеп готов. Пожалуйста, подойдите в офис за дальнейшей информацией!",
        "LOADING_WAIT": "Ваш прицеп загружается — пожалуйста, подождите!",
        "REPORT_OFFICE": "Пожалуйста, подойдите в офис!",
    },
    "lt": {
        "DEPARTED": "Saugios kelionės – laukiame jūsų sugrįžtant!",
        "LOCATION_WITH_TRAILER": "Prašome prijungti priekabą {trailer} vietoje: {location} ir pasiimti CMR dokumentus biure!",
        "LOCATION_NO_TRAILER": "Prašome prijungti priekabą vietoje: {location} ir pasiimti CMR dokumentus biure!",
        "CLOSEDOOR_NO_LOCATION": "Jūsų priekaba paruošta. Prašome užsukti į biurą dėl tolimesnės informacijos!",
        "LOADING_WAIT": "Jūsų priekaba kraunama – prašome palaukti!",
        "REPORT_OFFICE": "Prašome užsukti į biurą!",
    },
    "kk": {
        "DEPARTED": "Сәтті жол — сізді қайта күтеміз!",
        "LOCATION_WITH_TRAILER": "{location} орнында {trailer} тіркемесін қосып, CMR құжаттарын кеңседен алыңыз!",
        "LOCATION_NO_TRAILER": "{location} орнында тіркемені қосып, CMR құжаттарын кеңседен алыңыз!",
        "CLOSEDOOR_NO_LOCATION": "Тіркеме дайын. Қосымша ақпарат үшін кеңсеге келіңіз!",
        "LOADING_WAIT": "Тіркеме тиелуде — күтіңіз!",
        "REPORT_OFFICE": "Кеңсеге келіңіз!",
    },
    "hi": {
        "DEPARTED": "सुरक्षित यात्रा करें — हम आपका वापस इंतज़ार करेंगे!",
        "LOCATION_WITH_TRAILER": "कृपया {location} स्थान पर ट्रेलर {trailer} जोड़ें और CMR दस्तावेज़ कार्यालय से लें!",
        "LOCATION_NO_TRAILER": "कृपया {location} स्थान पर ट्रेलर जोड़ें और CMR दस्तावेज़ कार्यालय से लें!",
        "CLOSEDOOR_NO_LOCATION": "आपका ट्रेलर तैयार है। आगे की जानकारी के लिए कृपया कार्यालय में रिपोर्ट करें!",
        "LOADING_WAIT": "आपका ट्रेलर लोड हो रहा है — कृपया प्रतीक्षा करें!",
        "REPORT_OFFICE": "कृपया कार्यालय में रिपोर्ट करें!",
    },
    "pl": {
        "DEPARTED": "Szerokiej drogi — czekamy na Twój powrót!",
        "LOCATION_WITH_TRAILER": "Proszę podpiąć naczepę {trailer} na lokalizacji: {location} i odebrać dokumenty CMR w biurze!",
        "LOCATION_NO_TRAILER": "Proszę podpiąć naczepę na lokalizacji: {location} i odebrać dokumenty CMR w biurze!",
        "CLOSEDOOR_NO_LOCATION": "Twoja naczepa jest gotowa. Proszę zgłosić się do biura po dalsze informacje!",
        "LOADING_WAIT": "Twoja naczepa jest ładowana — proszę czekać!",
        "REPORT_OFFICE": "Proszę zgłosić się do biura!",
    },
    "hu": {
        "DEPARTED": "Vezess óvatosan – várunk vissza!",
        "LOCATION_WITH_TRAILER": "Kérjük, csatlakoztasd a(z) {trailer} pótkocsit a következő helyen: {location}, és vedd fel a CMR dokumentumokat az irodában!",
        "LOCATION_NO_TRAILER": "Kérjük, csatlakoztasd a pótkocsit a következő helyen: {location}, és vedd fel a CMR dokumentumokat az irodában!",
        "CLOSEDOOR_NO_LOCATION": "A pótkocsid kész. További információért jelentkezz az irodában!",
        "LOADING_WAIT": "A pótkocsid rakodás alatt – kérjük, várj!",
        "REPORT_OFFICE": "Kérjük, jelentkezz az irodában!",
    },
    "uz": {
        "DEPARTED": "Xavfsiz haydang — sizni qaytib kelishingizni kutamiz!",
        "LOCATION_WITH_TRAILER": "Iltimos, {location} joyida {trailer} treylerini ulang va CMR hujjatlarini ofisdan oling!",
        "LOCATION_NO_TRAILER": "Iltimos, {location} joyida treylerini ulang va CMR hujjatlarini ofisdan oling!",
        "CLOSEDOOR_NO_LOCATION": "Treyleringiz tayyor. Qo‘shimcha ma’lumot uchun ofisga murojaat qiling!",
        "LOADING_WAIT": "Treyleringiz yuklanmoqda — iltimos, kuting!",
        "REPORT_OFFICE": "Iltimos, ofisga murojaat qiling!",
    },
    "tg": {
        "DEPARTED": "Сафар ба хайр — мо шуморо боз интизорем!",
        "LOCATION_WITH_TRAILER": "Лутфан прицепи {trailer}-ро дар ҷойи {location} васл кунед ва ҳуҷҷатҳои CMR-ро аз офис гиред!",
        "LOCATION_NO_TRAILER": "Лутфан прицепро дар ҷойи {location} васл кунед ва ҳуҷҷатҳои CMR-ро аз офис гиред!",
        "CLOSEDOOR_NO_LOCATION": "Прицепи шумо тайёр аст. Барои маълумоти бештар ба офис ҳозир шавед!",
        "LOADING_WAIT": "Прицепи шумо бор карда мешавад — лутфан интизор шавед!",
        "REPORT_OFFICE": "Лутфан ба офис ҳозир шавед!",
    },
    "ky": {
        "DEPARTED": "Жолуңуз болсун — кайра келишиңизди күтөбүз!",
        "LOCATION_WITH_TRAILER": "Сураныч, {location} жерде {trailer} чиркегичин туташтырып, CMR документтерин кеңседен алыңыз!",
        "LOCATION_NO_TRAILER": "Сураныч, {location} жерде чиркегичти туташтырып, CMR документтерин кеңседен алыңыз!",
        "CLOSEDOOR_NO_LOCATION": "Чиркегичиңиз даяр. Кошумча маалымат үчүн кеңсеге келиңиз!",
        "LOADING_WAIT": "Чиркегичиңиз жүктөлүүдө — сураныч, күтө туруңуз!",
        "REPORT_OFFICE": "Сураныч, кеңсеге келиңиз!",
    },
    "be": {
        "DEPARTED": "Шчаслівай дарогі — чакаем вас назад!",
        "LOCATION_WITH_TRAILER": "Калі ласка, прычапіце прычэп {trailer} у месцы: {location} і забярыце дакументы CMR у офісе!",
        "LOCATION_NO_TRAILER": "Калі ласка, прычапіце прычэп у месцы: {location} і забярыце дакументы CMR у офісе!",
        "CLOSEDOOR_NO_LOCATION": "Ваш прычэп гатовы. Калі ласка, зайдзіце ў офіс для далейшай інфармацыі!",
        "LOADING_WAIT": "Ваш прычэп загружаецца — калі ласка, пачакайце!",
        "REPORT_OFFICE": "Калі ласка, зайдзіце ў офіс!",
    },
}

_I18N_PUSH_TITLES: Dict[str, Dict[str, str]] = {
    "en": {"STATUS_UPDATE": "Status update", "MESSAGE_FROM_DISPATCH": "Message from dispatch"},
    "de": {"STATUS_UPDATE": "Status-Update", "MESSAGE_FROM_DISPATCH": "Nachricht von der Disposition"},
    "nl": {"STATUS_UPDATE": "Statusupdate", "MESSAGE_FROM_DISPATCH": "Bericht van de planning"},

    "es": {"STATUS_UPDATE": "Actualización de estado", "MESSAGE_FROM_DISPATCH": "Mensaje del despacho"},
    "it": {"STATUS_UPDATE": "Aggiornamento stato", "MESSAGE_FROM_DISPATCH": "Messaggio dal dispatch"},
    "ro": {"STATUS_UPDATE": "Actualizare status", "MESSAGE_FROM_DISPATCH": "Mesaj de la dispecerat"},
    "ru": {"STATUS_UPDATE": "Обновление статуса", "MESSAGE_FROM_DISPATCH": "Сообщение от диспетчера"},
    "lt": {"STATUS_UPDATE": "Būsenos atnaujinimas", "MESSAGE_FROM_DISPATCH": "Žinutė iš dispečerio"},
    "kk": {"STATUS_UPDATE": "Күй жаңартуы", "MESSAGE_FROM_DISPATCH": "Диспетчерден хабарлама"},
    "hi": {"STATUS_UPDATE": "स्थिति अपडेट", "MESSAGE_FROM_DISPATCH": "डिस्पैच से संदेश"},
    "pl": {"STATUS_UPDATE": "Aktualizacja statusu", "MESSAGE_FROM_DISPATCH": "Wiadomość od dyspozytora"},
    "hu": {"STATUS_UPDATE": "Státusz frissítés", "MESSAGE_FROM_DISPATCH": "Üzenet a diszpécsertől"},
    "uz": {"STATUS_UPDATE": "Holat yangilanishi", "MESSAGE_FROM_DISPATCH": "Dispetcherdan xabar"},
    "tg": {"STATUS_UPDATE": "Навсозии ҳолат", "MESSAGE_FROM_DISPATCH": "Паём аз диспетчер"},
    "ky": {"STATUS_UPDATE": "Абалды жаңыртуу", "MESSAGE_FROM_DISPATCH": "Диспетчерден билдирүү"},
    "be": {"STATUS_UPDATE": "Абнаўленне статусу", "MESSAGE_FROM_DISPATCH": "Паведамленне ад дыспетчара"},
}

_I18N_ROUTE_NOTE: Dict[str, Dict[str, str]] = {
    "en": {"ORS": "Route source: OpenRouteService", "OSRM": "Route source: OSRM", "DIRECT": "Route source: direct line"},
    "de": {"ORS": "Routenquelle: OpenRouteService", "OSRM": "Routenquelle: OSRM", "DIRECT": "Routenquelle: direkte Linie"},
    "nl": {"ORS": "Routebron: OpenRouteService", "OSRM": "Routebron: OSRM", "DIRECT": "Routebron: rechte lijn"},

    "es": {"ORS": "Fuente de ruta: OpenRouteService", "OSRM": "Fuente de ruta: OSRM", "DIRECT": "Fuente de ruta: línea directa"},
    "it": {"ORS": "Fonte percorso: OpenRouteService", "OSRM": "Fonte percorso: OSRM", "DIRECT": "Fonte percorso: linea diretta"},
    "ro": {"ORS": "Sursa rutei: OpenRouteService", "OSRM": "Sursa rutei: OSRM", "DIRECT": "Sursa rutei: linie directă"},
    "ru": {"ORS": "Источник маршрута: OpenRouteService", "OSRM": "Источник маршрута: OSRM", "DIRECT": "Источник маршрута: прямая линия"},
    "lt": {"ORS": "Maršruto šaltinis: OpenRouteService", "OSRM": "Maršruto šaltinis: OSRM", "DIRECT": "Maršruto šaltinis: tiesi linija"},
    "kk": {"ORS": "Маршрут көзі: OpenRouteService", "OSRM": "Маршрут көзі: OSRM", "DIRECT": "Маршрут көзі: түзу сызық"},
    "hi": {"ORS": "मार्ग स्रोत: OpenRouteService", "OSRM": "मार्ग स्रोत: OSRM", "DIRECT": "मार्ग स्रोत: सीधी रेखा"},
    "pl": {"ORS": "Źródło trasy: OpenRouteService", "OSRM": "Źródło trasy: OSRM", "DIRECT": "Źródło trasy: linia prosta"},
    "hu": {"ORS": "Útvonal forrása: OpenRouteService", "OSRM": "Útvonal forrása: OSRM", "DIRECT": "Útvonal forrása: egyenes vonal"},
    "uz": {"ORS": "Marshrut manbai: OpenRouteService", "OSRM": "Marshrut manbai: OSRM", "DIRECT": "Marshrut manbai: to‘g‘ri chiziq"},
    "tg": {"ORS": "Манбаи масир: OpenRouteService", "OSRM": "Манбаи масир: OSRM", "DIRECT": "Манбаи масир: хатти рост"},
    "ky": {"ORS": "Маршрут булагы: OpenRouteService", "OSRM": "Маршрут булагы: OSRM", "DIRECT": "Маршрут булагы: түз сызык"},
    "be": {"ORS": "Крыніца маршруту: OpenRouteService", "OSRM": "Крыніца маршруту: OSRM", "DIRECT": "Крыніца маршруту: прамая лінія"},
}

def route_note_text(route_key: str, lang: str = "en") -> str:
    l = normalize_lang(lang)
    rk = str(route_key or "").strip().upper()
    table = _I18N_ROUTE_NOTE.get(l) or _I18N_ROUTE_NOTE["en"]
    return table.get(rk, _I18N_ROUTE_NOTE["en"].get(rk, ""))


def push_title_text(title_key: str, lang: str = "en") -> str:
    l = normalize_lang(lang)
    tk = str(title_key or "").strip().upper()
    table = _I18N_PUSH_TITLES.get(l) or _I18N_PUSH_TITLES["en"]
    return table.get(tk, _I18N_PUSH_TITLES["en"].get(tk, ""))


def compute_driver_status(m: Dict[str, Any], lang: str = "en") -> Dict[str, Any]:
    """Compute driver-facing status with localization."""
    lang_n = normalize_lang(lang)
    tmpl = _I18N_STATUS.get(lang_n) or _I18N_STATUS["en"]

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
        # Manual message is NOT translated (dispatcher text)
        return {"status_key": key, "status_text": msg, "report_in_office_at": ""}

    # Departed override (after manual status)
    departed = m.get("departed", False)
    if isinstance(departed, str):
        departed = departed.strip().lower() in {"1", "true", "yes", "y"}
    if not departed:
        departed = _has(m.get("departed_at", ""))
    if departed:
        return {"status_key": "DEPARTED", "status_text": tmpl["DEPARTED"], "report_in_office_at": ""}

    close_door = m.get("close_door", "")
    location = _clean_location_value(m.get("location", ""))
    trailer = str(m.get("trailer", "") or "").strip()
    sched_raw = m.get("scheduled_departure", "")

    sched_dt = _parse_dt(sched_raw)

    if _has(location):
        if trailer:
            msg2 = tmpl["LOCATION_WITH_TRAILER"].format(trailer=trailer, location=location)
        else:
            msg2 = tmpl["LOCATION_NO_TRAILER"].format(location=location)
        key2 = "LOCATION"
    elif _has(close_door):
        msg2 = tmpl["CLOSEDOOR_NO_LOCATION"]
        key2 = "CLOSEDOOR_NO_LOCATION"
    else:
        minutes_left = None
        if sched_dt:
            minutes_left = (sched_dt - datetime.now()).total_seconds() / 60.0

        if minutes_left is not None and minutes_left > 45:
            msg2 = tmpl["LOADING_WAIT"]
            key2 = "LOADING_WAIT"
        else:
            msg2 = tmpl["REPORT_OFFICE"]
            key2 = "REPORT_OFFICE"

    report_at = ""
    if sched_dt:
        ra = sched_dt - timedelta(minutes=45)
        report_at = _format_dt_like(ra, sched_raw)

    return {
        "status_key": key2,
        "status_text": msg2,
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
    """Return polyline as [[lat, lon], ...] and a short route-source key."""
    pts = _fetch_ors_route_coords(origin_lat, origin_lon, dest_lat, dest_lon)
    if pts:
        return [[lat, lon] for (lat, lon) in pts], "ORS"

    pts2 = _fetch_osrm_route_coords(origin_lat, origin_lon, dest_lat, dest_lon)
    if pts2:
        return [[lat, lon] for (lat, lon) in pts2], "OSRM"

    return [
        [float(origin_lat), float(origin_lon)],
        [float(dest_lat), float(dest_lon)],
    ], "DIRECT"


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


def _push_to_plate_localized(plate: str, title_key: str, body_by_lang: Dict[str, str]) -> None:
    """Send localized push to each subscription (best-effort)."""
    if not PUSH_ENABLED:
        return
    subs = SUBSCRIPTIONS_BY_PLATE.get(plate, []) or []
    if not subs:
        return

    vapid_claims = {"sub": VAPID_SUBJECT}
    alive = []

    for sub in subs:
        try:
            lang = normalize_lang((sub or {}).get("lang", "en"))
            title = push_title_text(title_key, lang)
            body = body_by_lang.get(lang) or body_by_lang.get("en") or ""

            payload = json.dumps({
                "title": title,
                "body": body,
                "url": f"/?plate={urllib.parse.quote(plate)}&lang={urllib.parse.quote(lang)}",
            })

            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=vapid_claims,
            )
            alive.append(sub)
        except Exception:
            # Drop dead subscriptions
            pass

    SUBSCRIPTIONS_BY_PLATE[plate] = alive


def _push_status_change_to_plate(plate: str, movement: Dict[str, Any]) -> None:
    """Push a status update to a plate, in each subscriber's language."""
    try:
        bodies: Dict[str, str] = {}
        for l in SUPPORTED_LANGS:
            bodies[l] = compute_driver_status(movement, lang=l).get("status_text", "")
        _push_to_plate_localized(plate, "STATUS_UPDATE", bodies)
    except Exception:
        return


def _push_driver_message_to_plate(plate: str, message: str) -> None:
    """Push dispatcher message to a plate (message text is not translated)."""
    try:
        bodies = {l: str(message or "") for l in SUPPORTED_LANGS}
        _push_to_plate_localized(plate, "MESSAGE_FROM_DISPATCH", bodies)
    except Exception:
        return


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
                    _push_status_change_to_plate(plate, m)
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

    _push_driver_message_to_plate(plate, message)

    return {"ok": True, "plate": plate, "message": message}


@app.get("/api/status")
def get_status(
    plate: str = Query(..., min_length=2),
    lat: float = Query(...),
    lon: float = Query(...),
    ts: int = Query(..., description="Unix epoch seconds from the device"),
    lang: str = Query("en", description="Language: en, de, nl, es, it, ro, ru, lt, kk, hi, pl, hu, uz, tg, ky, be"),
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

    st = compute_driver_status(rec, lang=lang)

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
        "location": _clean_location_value(rec.get("location") or ""),
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



@app.get("/api/traffic_delay")
def traffic_delay(
    secret: str = Query(..., min_length=8),
    o: str = Query(..., description="origin as 'lat,lon'"),
    d: str = Query(..., description="destination as 'lat,lon'"),
    depart: str = Query("", description="optional ISO-8601 departure time"),
) -> Dict[str, Any]:
    """Return live-traffic delay minutes (server-side)."""
    if not ADMIN_UPLOAD_SECRET:
        raise HTTPException(status_code=500, detail="Server not configured: ADMIN_UPLOAD_SECRET missing.")
    if secret != ADMIN_UPLOAD_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized.")

    try:
        o_lat_s, o_lon_s = [x.strip() for x in (o or "").split(",", 1)]
        d_lat_s, d_lon_s = [x.strip() for x in (d or "").split(",", 1)]
        o_lat = float(o_lat_s)
        o_lon = float(o_lon_s)
        d_lat = float(d_lat_s)
        d_lon = float(d_lon_s)
    except Exception:
        return {"ok": False, "error": "invalid o/d coords"}

    depart_bucket = (depart or "").strip()[:16]  # minute bucket
    cache_key = (round(o_lat, 4), round(o_lon, 4), round(d_lat, 4), round(d_lon, 4), depart_bucket)

    cached = _traffic_cache_get(cache_key)
    if cached is not None:
        return cached

    delay_min, err = _here_fetch_delay_minutes(o_lat, o_lon, d_lat, d_lon, (depart or "").strip())
    if err:
        payload = {"ok": False, "error": err}
        _traffic_cache_set(cache_key, payload)
        return payload

    payload = {"ok": True, "delay_min": int(delay_min or 0)}
    _traffic_cache_set(cache_key, payload)
    return payload


@app.get("/api/route")
def get_route(
    plate: str = Query(..., min_length=2),
    lat: float = Query(...),
    lon: float = Query(...),
    ts: int = Query(..., description="Unix epoch seconds from the device"),
    lang: str = Query("en", description="Language: en, de, nl, es, it, ro, ru, lt, kk, hi, pl, hu, uz, tg, ky, be"),
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

    route_pts, route_key = build_route_points(origin_lat, origin_lon, dest_lat, dest_lon)
    note = route_note_text(route_key, lang)

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
    lang: str = Query("en", description="Language: en, de, nl, es, it, ro, ru, lt, kk, hi, pl, hu, uz, tg, ky, be"),
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

    sub_rec = dict(subscription)
    sub_rec["lang"] = normalize_lang(lang)
    subs.append(sub_rec)

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
      width: min(860px, calc(100% - 24px));
      margin: 0 auto;
      padding: 18px 0 24px;
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
      height: clamp(240px, 42vh, 460px);
      width: 100%;
      border-radius: 12px;
      border: 1px solid rgba(0,0,0,0.18);
      overflow: hidden;
      background: rgba(255,255,255,0.35);
    }

    .langbar {
      display: flex;
      gap: 10px;
      align-items: center;
      margin: 8px 0 10px;
      flex-wrap: wrap;
    }
    .flagbtn {
      width: 38px;
      height: 38px;
      border-radius: 999px;
      border: 1px solid rgba(0,0,0,0.16);
      background: rgba(255,255,255,0.55);
      cursor: pointer;
      font-size: 22px;
      line-height: 1;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 8px 16px rgba(0,0,0,0.12);
      transition: transform 0.08s ease, filter 0.15s ease, box-shadow 0.15s ease;
      user-select: none;
      -webkit-tap-highlight-color: transparent;
      padding: 0;
    }
    .flagbtn:hover { filter: brightness(1.08); transform: translateY(-1px); box-shadow: 0 10px 18px rgba(0,0,0,0.16); }
    .flagbtn:active { transform: translateY(0px); filter: brightness(0.98); box-shadow: 0 7px 14px rgba(0,0,0,0.12); }
    .flagbtn.active {
      outline: none;
      border-color: rgba(77,20,140,0.55);
      box-shadow: 0 0 0 3px rgba(77,20,140,0.18), 0 8px 16px rgba(0,0,0,0.12);
    }

    @media (max-width: 420px) {
      .row { flex-direction: column; }
      .row-main > button { flex: 0 0 auto; width: 100%; }
      body { background-attachment: scroll; }
    }
    a { color: inherit; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topcard">
      <h2 id="titleH2" style="margin: 6px 0 6px;">Movement status by license plate</h2>
      <div class="langbar" id="langbar" aria-label="Language">
        <button class="flagbtn" data-lang="en" title="English" aria-label="English">🇬🇧</button>
        <button class="flagbtn" data-lang="de" title="Deutsch" aria-label="Deutsch">🇩🇪</button>
        <button class="flagbtn" data-lang="nl" title="Nederlands" aria-label="Nederlands">🇳🇱</button>
        <button class="flagbtn" data-lang="es" title="Español" aria-label="Español">🇪🇸</button>
        <button class="flagbtn" data-lang="it" title="Italiano" aria-label="Italiano">🇮🇹</button>
        <button class="flagbtn" data-lang="ro" title="Română" aria-label="Română">🇷🇴</button>
        <button class="flagbtn" data-lang="ru" title="Русский" aria-label="Русский">🇷🇺</button>
        <button class="flagbtn" data-lang="lt" title="Lietuvių" aria-label="Lietuvių">🇱🇹</button>
        <button class="flagbtn" data-lang="kk" title="Қазақша" aria-label="Қазақша">🇰🇿</button>
        <button class="flagbtn" data-lang="hi" title="हिन्दी" aria-label="हिन्दी">🇮🇳</button>
        <button class="flagbtn" data-lang="pl" title="Polski" aria-label="Polski">🇵🇱</button>
        <button class="flagbtn" data-lang="hu" title="Magyar" aria-label="Magyar">🇭🇺</button>
        <button class="flagbtn" data-lang="uz" title="O‘zbek" aria-label="O‘zbek">🇺🇿</button>
        <button class="flagbtn" data-lang="tg" title="Тоҷикӣ" aria-label="Тоҷикӣ">🇹🇯</button>
        <button class="flagbtn" data-lang="ky" title="Кыргызча" aria-label="Кыргызча">🇰🇬</button>
        <button class="flagbtn" data-lang="be" title="Беларуская" aria-label="Беларуская">🇧🇾</button>
      </div>

      <div class="row row-main">
        <input id="plate" placeholder="Enter license plate (e.g. AB-123-CD)" />
        <button id="btn" class="btn btn-primary">Check</button>
      </div>

      <div class="row" style="margin-top: 8px;">
        <button id="btnNotify" class="btn btn-secondary" style="display:none;">Enable notifications</button>
      </div>
      <div id="notifyMsg" class="muted" style="margin-top:6px; display:none;"></div>

      <div id="out" class="card" style="display:none;"></div>
    </div>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

  <script>
    const API_BASE = window.location.origin;    const SUPPORTED_LANGS = ["en", "de", "nl", "es", "it", "ro", "ru", "lt", "kk", "hi", "pl", "hu", "uz", "tg", "ky", "be"];
    const UI = {
      en: {
        title: "Movement status by license plate",
        plate_ph: "Enter license plate (e.g. AB-123-CD)",
        btn_check: "Check",
        btn_notify: "Enable notifications",
        btn_enabling: "Enabling...",
        btn_enabled: "Notifications enabled",

        getting_location: "Getting location…",
        loading_status: "Loading status…",
        loading_route: "Loading route…",

        no_movement: "No movement found",
        last_refresh: "Last refresh",
        destination: "Destination",
        departure_time: "Departure time",
        report_office: "Report in the office",
        trailer: "Trailer",
        place: "Place",
        route_map: "Route map",
        origin: "Origin",
        destination_pin: "Destination",

        parking: "Parking",
        dock: "Dock",

        err_location: "Location error",
        err_network: "Network error",
        err_error: "Error",
        help_location: "Enable GPS and allow location permission.",

        notify_not_supported: "Notifications not supported",
        notify_not_supported_help: "Use Chrome/Edge on Android. iOS requires adding the site to Home Screen.",
        notify_denied: "Notifications denied",
        notify_denied_help: "Allow notifications in browser settings.",
        notify_failed: "Subscribe failed",
        notify_enabled_msg: "Notifications enabled",
        notify_enabled_help: "You will receive a push when your status changes.",
        subscribe_error: "Subscribe error",
        route_error: "Route error"
      },
      de: {
        title: "Bewegungsstatus nach Kennzeichen",
        plate_ph: "Kennzeichen eingeben (z. B. AB-123-CD)",
        btn_check: "Prüfen",
        btn_notify: "Benachrichtigungen aktivieren",
        btn_enabling: "Aktiviere…",
        btn_enabled: "Benachrichtigungen aktiv",

        getting_location: "Standort wird abgerufen…",
        loading_status: "Status wird geladen…",
        loading_route: "Route wird geladen…",

        no_movement: "Keine Bewegung gefunden",
        last_refresh: "Letzte Aktualisierung",
        destination: "Ziel",
        departure_time: "Abfahrtszeit",
        report_office: "Im Büro melden",
        trailer: "Anhänger",
        place: "Ort",
        route_map: "Routenkarte",
        origin: "Start",
        destination_pin: "Ziel",

        parking: "Parkplatz",
        dock: "Tor",

        err_location: "Standortfehler",
        err_network: "Netzwerkfehler",
        err_error: "Fehler",
        help_location: "GPS aktivieren und Standortzugriff erlauben.",

        notify_not_supported: "Benachrichtigungen nicht unterstützt",
        notify_not_supported_help: "Nutze Chrome/Edge auf Android. Unter iOS muss die Seite zum Home-Bildschirm hinzugefügt werden.",
        notify_denied: "Benachrichtigungen abgelehnt",
        notify_denied_help: "Benachrichtigungen in den Browser-Einstellungen erlauben.",
        notify_failed: "Abonnement fehlgeschlagen",
        notify_enabled_msg: "Benachrichtigungen aktiv",
        notify_enabled_help: "Du erhältst eine Push-Nachricht, wenn sich dein Status ändert.",
        subscribe_error: "Abo-Fehler",
        route_error: "Routenfehler"
      },
      nl: {
        title: "Bewegingsstatus op kenteken",
        plate_ph: "Kenteken invoeren (bv. AB-123-CD)",
        btn_check: "Check",
        btn_notify: "Meldingen inschakelen",
        btn_enabling: "Inschakelen…",
        btn_enabled: "Meldingen ingeschakeld",

        getting_location: "Locatie ophalen…",
        loading_status: "Status laden…",
        loading_route: "Route laden…",

        no_movement: "Geen beweging gevonden",
        last_refresh: "Laatste update",
        destination: "Bestemming",
        departure_time: "Vertrektijd",
        report_office: "Melden op kantoor",
        trailer: "Trailer",
        place: "Plek",
        route_map: "Routekaart",
        origin: "Start",
        destination_pin: "Bestemming",

        parking: "Parkeerplaats",
        dock: "Dock",

        err_location: "Locatiefout",
        err_network: "Netwerkfout",
        err_error: "Fout",
        help_location: "Zet GPS aan en sta locatie-toestemming toe.",

        notify_not_supported: "Meldingen niet ondersteund",
        notify_not_supported_help: "Gebruik Chrome/Edge op Android. Op iOS moet de site aan het beginscherm worden toegevoegd.",
        notify_denied: "Meldingen geweigerd",
        notify_denied_help: "Sta meldingen toe in de browserinstellingen.",
        notify_failed: "Abonneren mislukt",
        notify_enabled_msg: "Meldingen ingeschakeld",
        notify_enabled_help: "Je ontvangt een push als je status verandert.",
        subscribe_error: "Abonneerfout",
        route_error: "Routefout"
      },

      es: {
        title: "Estado del movimiento por matrícula",
        plate_ph: "Introduce la matrícula (p. ej. AB-123-CD)",
        btn_check: "Comprobar",
        btn_notify: "Activar notificaciones",
        btn_enabling: "Activando…",
        btn_enabled: "Notificaciones activadas",

        getting_location: "Obteniendo ubicación…",
        loading_status: "Cargando estado…",
        loading_route: "Cargando ruta…",

        no_movement: "No se encontró movimiento",
        last_refresh: "Última actualización",
        destination: "Destino",
        departure_time: "Hora de salida",
        report_office: "Presentarse en la oficina",
        trailer: "Remolque",
        place: "Lugar",
        route_map: "Mapa de ruta",
        origin: "Origen",
        destination_pin: "Destino",

        parking: "Parking",
        dock: "Muelle",

        err_location: "Error de ubicación",
        err_network: "Error de red",
        err_error: "Error",
        help_location: "Activa el GPS y permite el acceso a la ubicación.",

        notify_not_supported: "Notificaciones no compatibles",
        notify_not_supported_help: "Usa Chrome/Edge en Android. En iOS hay que añadir el sitio a la pantalla de inicio.",
        notify_denied: "Notificaciones denegadas",
        notify_denied_help: "Permite las notificaciones en la configuración del navegador.",
        notify_failed: "Fallo al suscribirse",
        notify_enabled_msg: "Notificaciones activadas",
        notify_enabled_help: "Recibirás un push cuando cambie tu estado.",
        subscribe_error: "Error de suscripción",
        route_error: "Error de ruta"
      },
      it: {
        title: "Stato del movimento per targa",
        plate_ph: "Inserisci la targa (es. AB-123-CD)",
        btn_check: "Verifica",
        btn_notify: "Abilita notifiche",
        btn_enabling: "Abilitazione…",
        btn_enabled: "Notifiche abilitate",

        getting_location: "Rilevamento posizione…",
        loading_status: "Caricamento stato…",
        loading_route: "Caricamento percorso…",

        no_movement: "Nessun movimento trovato",
        last_refresh: "Ultimo aggiornamento",
        destination: "Destinazione",
        departure_time: "Ora di partenza",
        report_office: "Presentarsi in ufficio",
        trailer: "Rimorchio",
        place: "Luogo",
        route_map: "Mappa percorso",
        origin: "Origine",
        destination_pin: "Destinazione",

        parking: "Parcheggio",
        dock: "Dock",

        err_location: "Errore posizione",
        err_network: "Errore di rete",
        err_error: "Errore",
        help_location: "Attiva il GPS e consenti l'accesso alla posizione.",

        notify_not_supported: "Notifiche non supportate",
        notify_not_supported_help: "Usa Chrome/Edge su Android. Su iOS aggiungi il sito alla schermata Home.",
        notify_denied: "Notifiche negate",
        notify_denied_help: "Consenti le notifiche nelle impostazioni del browser.",
        notify_failed: "Iscrizione non riuscita",
        notify_enabled_msg: "Notifiche abilitate",
        notify_enabled_help: "Riceverai un push quando cambia lo stato.",
        subscribe_error: "Errore iscrizione",
        route_error: "Errore percorso"
      },
      ro: {
        title: "Starea mișcării după numărul de înmatriculare",
        plate_ph: "Introdu numărul (ex. AB-123-CD)",
        btn_check: "Verifică",
        btn_notify: "Activează notificări",
        btn_enabling: "Se activează…",
        btn_enabled: "Notificări active",

        getting_location: "Se obține locația…",
        loading_status: "Se încarcă statusul…",
        loading_route: "Se încarcă ruta…",

        no_movement: "Nu s-a găsit mișcarea",
        last_refresh: "Ultima actualizare",
        destination: "Destinație",
        departure_time: "Ora plecării",
        report_office: "Prezintă-te la birou",
        trailer: "Remorcă",
        place: "Loc",
        route_map: "Harta rutei",
        origin: "Origine",
        destination_pin: "Destinație",

        parking: "Parcare",
        dock: "Rampă",

        err_location: "Eroare locație",
        err_network: "Eroare de rețea",
        err_error: "Eroare",
        help_location: "Activează GPS-ul și permite accesul la locație.",

        notify_not_supported: "Notificări neacceptate",
        notify_not_supported_help: "Folosește Chrome/Edge pe Android. Pe iOS trebuie adăugat site-ul pe ecranul principal.",
        notify_denied: "Notificări refuzate",
        notify_denied_help: "Permite notificările în setările browserului.",
        notify_failed: "Abonarea a eșuat",
        notify_enabled_msg: "Notificări activate",
        notify_enabled_help: "Vei primi un push când se schimbă statusul.",
        subscribe_error: "Eroare abonare",
        route_error: "Eroare rută"
      },
      ru: {
        title: "Статус рейса по номеру",
        plate_ph: "Введите номер (например AB-123-CD)",
        btn_check: "Проверить",
        btn_notify: "Включить уведомления",
        btn_enabling: "Включение…",
        btn_enabled: "Уведомления включены",

        getting_location: "Получаем геолокацию…",
        loading_status: "Загружаем статус…",
        loading_route: "Загружаем маршрут…",

        no_movement: "Рейс не найден",
        last_refresh: "Последнее обновление",
        destination: "Пункт назначения",
        departure_time: "Время выезда",
        report_office: "Подойти в офис",
        trailer: "Прицеп",
        place: "Место",
        route_map: "Карта маршрута",
        origin: "Старт",
        destination_pin: "Назначение",

        parking: "Парковка",
        dock: "Док",

        err_location: "Ошибка геолокации",
        err_network: "Ошибка сети",
        err_error: "Ошибка",
        help_location: "Включите GPS и разрешите доступ к геолокации.",

        notify_not_supported: "Уведомления не поддерживаются",
        notify_not_supported_help: "Используйте Chrome/Edge на Android. На iOS добавьте сайт на главный экран.",
        notify_denied: "Уведомления запрещены",
        notify_denied_help: "Разрешите уведомления в настройках браузера.",
        notify_failed: "Подписка не удалась",
        notify_enabled_msg: "Уведомления включены",
        notify_enabled_help: "Вы получите push, когда статус изменится.",
        subscribe_error: "Ошибка подписки",
        route_error: "Ошибка маршрута"
      },
      lt: {
        title: "Judėjimo būsena pagal valstybinį numerį",
        plate_ph: "Įveskite numerį (pvz. AB-123-CD)",
        btn_check: "Tikrinti",
        btn_notify: "Įjungti pranešimus",
        btn_enabling: "Įjungiama…",
        btn_enabled: "Pranešimai įjungti",

        getting_location: "Gaunama vieta…",
        loading_status: "Įkeliama būsena…",
        loading_route: "Įkeliama trasa…",

        no_movement: "Judėjimas nerastas",
        last_refresh: "Paskutinis atnaujinimas",
        destination: "Paskirtis",
        departure_time: "Išvykimo laikas",
        report_office: "Atsižymėti biure",
        trailer: "Priekaba",
        place: "Vieta",
        route_map: "Maršruto žemėlapis",
        origin: "Pradžia",
        destination_pin: "Paskirtis",

        parking: "Parkingas",
        dock: "Dokas",

        err_location: "Vietos klaida",
        err_network: "Tinklo klaida",
        err_error: "Klaida",
        help_location: "Įjunkite GPS ir leiskite vietos leidimą.",

        notify_not_supported: "Pranešimai nepalaikomi",
        notify_not_supported_help: "Naudokite Chrome/Edge Android. iOS reikalauja pridėti svetainę į pagrindinį ekraną.",
        notify_denied: "Pranešimai atmesti",
        notify_denied_help: "Leiskite pranešimus naršyklės nustatymuose.",
        notify_failed: "Prenumerata nepavyko",
        notify_enabled_msg: "Pranešimai įjungti",
        notify_enabled_help: "Gausite push pranešimą, kai pasikeis būsena.",
        subscribe_error: "Prenumeratos klaida",
        route_error: "Maršruto klaida"
      },
      kk: {
        title: "Көлік нөмірі бойынша қозғалыс күйі",
        plate_ph: "Нөмірді енгізіңіз (мысалы AB-123-CD)",
        btn_check: "Тексеру",
        btn_notify: "Хабарландыруларды қосу",
        btn_enabling: "Қосылуда…",
        btn_enabled: "Хабарландырулар қосулы",

        getting_location: "Орналасу анықталуда…",
        loading_status: "Күй жүктелуде…",
        loading_route: "Маршрут жүктелуде…",

        no_movement: "Қозғалыс табылмады",
        last_refresh: "Соңғы жаңарту",
        destination: "Бағыт",
        departure_time: "Жөнелу уақыты",
        report_office: "Кеңсеге келу",
        trailer: "Тіркеме",
        place: "Орын",
        route_map: "Маршрут картасы",
        origin: "Бастау",
        destination_pin: "Бағыт",

        parking: "Тұрақ",
        dock: "Док",

        err_location: "Орналасу қатесі",
        err_network: "Желі қатесі",
        err_error: "Қате",
        help_location: "GPS-ті қосыңыз және геолокацияға рұқсат беріңіз.",

        notify_not_supported: "Хабарландырулар қолдау көрсетілмейді",
        notify_not_supported_help: "Android-та Chrome/Edge қолданыңыз. iOS-та сайтты Home Screen-ге қосу керек.",
        notify_denied: "Хабарландыруларға тыйым салынған",
        notify_denied_help: "Браузер баптауларында хабарландыруларды рұқсат етіңіз.",
        notify_failed: "Жазылу сәтсіз",
        notify_enabled_msg: "Хабарландырулар қосылды",
        notify_enabled_help: "Күй өзгерсе, push хабарлама аласыз.",
        subscribe_error: "Жазылу қатесі",
        route_error: "Маршрут қатесі"
      },
      hi: {
        title: "लाइसेंस प्लेट के अनुसार मूवमेंट स्टेटस",
        plate_ph: "लाइसेंस प्लेट दर्ज करें (जैसे AB-123-CD)",
        btn_check: "जाँचें",
        btn_notify: "सूचनाएँ सक्षम करें",
        btn_enabling: "सक्षम किया जा रहा है…",
        btn_enabled: "सूचनाएँ सक्षम",

        getting_location: "लोकेशन प्राप्त की जा रही है…",
        loading_status: "स्टेटस लोड हो रहा है…",
        loading_route: "रूट लोड हो रहा है…",

        no_movement: "कोई मूवमेंट नहीं मिला",
        last_refresh: "अंतिम अपडेट",
        destination: "गंतव्य",
        departure_time: "प्रस्थान समय",
        report_office: "ऑफिस में रिपोर्ट करें",
        trailer: "ट्रेलर",
        place: "स्थान",
        route_map: "रूट मैप",
        origin: "प्रारंभ",
        destination_pin: "गंतव्य",

        parking: "पार्किंग",
        dock: "डॉक",

        err_location: "लोकेशन त्रुटि",
        err_network: "नेटवर्क त्रुटि",
        err_error: "त्रुटि",
        help_location: "GPS चालू करें और लोकेशन अनुमति दें।",

        notify_not_supported: "सूचनाएँ समर्थित नहीं हैं",
        notify_not_supported_help: "Android पर Chrome/Edge उपयोग करें। iOS के लिए साइट को Home Screen पर जोड़ना आवश्यक है।",
        notify_denied: "सूचनाएँ अस्वीकृत",
        notify_denied_help: "ब्राउज़र सेटिंग्स में सूचनाएँ अनुमति दें।",
        notify_failed: "सब्सक्राइब विफल",
        notify_enabled_msg: "सूचनाएँ सक्षम",
        notify_enabled_help: "स्टेटस बदलने पर आपको push सूचना मिलेगी।",
        subscribe_error: "सब्सक्राइब त्रुटि",
        route_error: "रूट त्रुटि"
      },
      pl: {
        title: "Status ruchu według tablicy rejestracyjnej",
        plate_ph: "Wpisz rejestrację (np. AB-123-CD)",
        btn_check: "Sprawdź",
        btn_notify: "Włącz powiadomienia",
        btn_enabling: "Włączanie…",
        btn_enabled: "Powiadomienia włączone",

        getting_location: "Pobieranie lokalizacji…",
        loading_status: "Ładowanie statusu…",
        loading_route: "Ładowanie trasy…",

        no_movement: "Nie znaleziono ruchu",
        last_refresh: "Ostatnie odświeżenie",
        destination: "Cel",
        departure_time: "Czas odjazdu",
        report_office: "Zgłoś się do biura",
        trailer: "Naczepa",
        place: "Miejsce",
        route_map: "Mapa trasy",
        origin: "Start",
        destination_pin: "Cel",

        parking: "Parking",
        dock: "Dok",

        err_location: "Błąd lokalizacji",
        err_network: "Błąd sieci",
        err_error: "Błąd",
        help_location: "Włącz GPS i zezwól na dostęp do lokalizacji.",

        notify_not_supported: "Powiadomienia nieobsługiwane",
        notify_not_supported_help: "Użyj Chrome/Edge na Androidzie. iOS wymaga dodania strony do ekranu początkowego.",
        notify_denied: "Powiadomienia odrzucone",
        notify_denied_help: "Zezwól na powiadomienia w ustawieniach przeglądarki.",
        notify_failed: "Subskrypcja nie powiodła się",
        notify_enabled_msg: "Powiadomienia włączone",
        notify_enabled_help: "Otrzymasz push, gdy status się zmieni.",
        subscribe_error: "Błąd subskrypcji",
        route_error: "Błąd trasy"
      },
      hu: {
        title: "Mozgás státusz rendszám alapján",
        plate_ph: "Add meg a rendszámot (pl. AB-123-CD)",
        btn_check: "Ellenőrzés",
        btn_notify: "Értesítések bekapcsolása",
        btn_enabling: "Bekapcsolás…",
        btn_enabled: "Értesítések bekapcsolva",

        getting_location: "Helyzet lekérése…",
        loading_status: "Státusz betöltése…",
        loading_route: "Útvonal betöltése…",

        no_movement: "Nincs találat",
        last_refresh: "Utolsó frissítés",
        destination: "Célállomás",
        departure_time: "Indulási idő",
        report_office: "Jelentkezz az irodában",
        trailer: "Pótkocsi",
        place: "Hely",
        route_map: "Útvonal térkép",
        origin: "Kiindulás",
        destination_pin: "Cél",

        parking: "Parkoló",
        dock: "Dokk",

        err_location: "Helymeghatározási hiba",
        err_network: "Hálózati hiba",
        err_error: "Hiba",
        help_location: "Kapcsold be a GPS-t és engedélyezd a helyhozzáférést.",

        notify_not_supported: "Értesítések nem támogatottak",
        notify_not_supported_help: "Androidon Chrome/Edge ajánlott. iOS-en add a weboldalt a Főképernyőhöz.",
        notify_denied: "Értesítések letiltva",
        notify_denied_help: "Engedélyezd az értesítéseket a böngésző beállításaiban.",
        notify_failed: "Feliratkozás sikertelen",
        notify_enabled_msg: "Értesítések bekapcsolva",
        notify_enabled_help: "Push értesítést kapsz, ha a státusz változik.",
        subscribe_error: "Feliratkozási hiba",
        route_error: "Útvonal hiba"
      },
      uz: {
  title: "Davlat raqami bo‘yicha harakat holati",
  plate_ph: "Davlat raqamini kiriting (masalan AB-123-CD)",
  btn_check: "Tekshirish",
  btn_notify: "Bildirishnomalarni yoqish",
  btn_enabling: "Yoqilmoqda…",
  btn_enabled: "Bildirishnomalar yoqildi",

  getting_location: "Joylashuv olinmoqda…",
  loading_status: "Holat yuklanmoqda…",
  loading_route: "Marshrut yuklanmoqda…",

  no_movement: "Harakat topilmadi",
  last_refresh: "Oxirgi yangilanish",
  destination: "Manzil",
  departure_time: "Jo‘nash vaqti",
  report_office: "Ofisga murojaat qiling",
  trailer: "Treyler",
  place: "Joy",
  route_map: "Marshrut xaritasi",
  origin: "Boshlanish",
  destination_pin: "Manzil",

  parking: "Parkovka",
  dock: "Dok",

  err_location: "Joylashuv xatosi",
  err_network: "Tarmoq xatosi",
  err_error: "Xato",
  help_location: "GPS-ni yoqing va joylashuv ruxsatini bering.",

  notify_not_supported: "Bildirishnomalar qo‘llab-quvvatlanmaydi",
  notify_not_supported_help: "Androidda Chrome/Edge’dan foydalaning. iOS’da saytni Home Screen’ga qo‘shish kerak.",
  notify_denied: "Bildirishnomalar rad etildi",
  notify_denied_help: "Brauzer sozlamalarida bildirishnomalarga ruxsat bering.",
  notify_failed: "Obuna bo‘lish muvaffaqiyatsiz",
  notify_enabled_msg: "Bildirishnomalar yoqildi",
  notify_enabled_help: "Holat o‘zgarsa push xabar olasiz.",
  subscribe_error: "Obuna xatosi",
  route_error: "Marshrut xatosi"
      },
      tg: {
  title: "Ҳолати ҳаракат аз рӯи рақами мошин",
  plate_ph: "Рақамро ворид кунед (масалан AB-123-CD)",
  btn_check: "Санҷидан",
  btn_notify: "Фаъол кардани огоҳиномаҳо",
  btn_enabling: "Фаъол мешавад…",
  btn_enabled: "Огоҳиномаҳо фаъол шуданд",

  getting_location: "Ҷойгиршавӣ гирифта мешавад…",
  loading_status: "Ҳолат бор мешавад…",
  loading_route: "Масир бор мешавад…",

  no_movement: "Ҳаракат ёфт нашуд",
  last_refresh: "Охирин навсозӣ",
  destination: "Самт",
  departure_time: "Вақти баромад",
  report_office: "Ба офис ҳозир шавед",
  trailer: "Прицеп",
  place: "Ҷой",
  route_map: "Харитаи масир",
  origin: "Оғоз",
  destination_pin: "Самт",

  parking: "Парковка",
  dock: "Док",

  err_location: "Хатои ҷойгиршавӣ",
  err_network: "Хатои шабака",
  err_error: "Хато",
  help_location: "GPS-ро фаъол кунед ва иҷозати ҷойгиршавиро диҳед.",

  notify_not_supported: "Огоҳиномаҳо дастгирӣ намешаванд",
  notify_not_supported_help: "Дар Android Chrome/Edge истифода баред. Дар iOS сайтро ба Home Screen илова кунед.",
  notify_denied: "Огоҳиномаҳо рад шуданд",
  notify_denied_help: "Дар танзимоти браузер огоҳиномаҳоро иҷозат диҳед.",
  notify_failed: "Обуна шудан ноком шуд",
  notify_enabled_msg: "Огоҳиномаҳо фаъол шуданд",
  notify_enabled_help: "Ҳангоми тағйири ҳолат push мегиред.",
  subscribe_error: "Хатои обуна",
  route_error: "Хатои масир"
      },
      ky: {
  title: "Мамлекеттик номер боюнча кыймылдын абалы",
  plate_ph: "Номерди киргизиңиз (мисалы AB-123-CD)",
  btn_check: "Текшерүү",
  btn_notify: "Билдирмелерди күйгүзүү",
  btn_enabling: "Күйгүзүлүүдө…",
  btn_enabled: "Билдирмелер күйгүзүлдү",

  getting_location: "Жайгашкан жер алынууда…",
  loading_status: "Абалы жүктөлүүдө…",
  loading_route: "Маршрут жүктөлүүдө…",

  no_movement: "Кыймыл табылган жок",
  last_refresh: "Акыркы жаңыртуу",
  destination: "Багыт",
  departure_time: "Жөнөө убактысы",
  report_office: "Кеңсеге кайрылыңыз",
  trailer: "Чиркегич",
  place: "Жай",
  route_map: "Маршрут картасы",
  origin: "Башталыш",
  destination_pin: "Багыт",

  parking: "Токтотмо",
  dock: "Док",

  err_location: "Жайгашуу катасы",
  err_network: "Тармак катасы",
  err_error: "Ката",
  help_location: "GPSти күйгүзүп, геолокацияга уруксат бериңиз.",

  notify_not_supported: "Билдирмелер колдоого алынбайт",
  notify_not_supported_help: "Android'де Chrome/Edge колдонуңуз. iOS'то сайтты Home Screen'ге кошуңуз.",
  notify_denied: "Билдирмелерге тыюу салынды",
  notify_denied_help: "Браузердин жөндөөлөрүнөн билдирмелерге уруксат бериңиз.",
  notify_failed: "Жазылуу ийгиликсиз",
  notify_enabled_msg: "Билдирмелер күйгүзүлдү",
  notify_enabled_help: "Абалы өзгөрсө, push билдирүү аласыз.",
  subscribe_error: "Жазылуу катасы",
  route_error: "Маршрут катасы"
      },
      be: {
  title: "Статус руху pa нумары",
  plate_ph: "Увядзіце нумар (напрыклад AB-123-CD)",
  btn_check: "Праверыць",
  btn_notify: "Уключыць апавяшчэнні",
  btn_enabling: "Уключэнне…",
  btn_enabled: "Апавяшчэнні ўключаны",

  getting_location: "Атрымліваем месцазнаходжанне…",
  loading_status: "Загружаем статус…",
  loading_route: "Загружаем маршрут…",

  no_movement: "Рух не знойдзены",
  last_refresh: "Апошняе абнаўленне",
  destination: "Пункт прызначэння",
  departure_time: "Час выезду",
  report_office: "Зайдзіце ў офіс",
  trailer: "Прычэп",
  place: "Месца",
  route_map: "Карта маршруту",
  origin: "Старт",
  destination_pin: "Прызначэнне",

  parking: "Паркоўка",
  dock: "Док",

  err_location: "Памылка месцазнаходжання",
  err_network: "Памылка сеткі",
  err_error: "Памылка",
  help_location: "Уключыце GPS і дазвольце доступ да месцазнаходжання.",

  notify_not_supported: "Апавяшчэнні не падтрымліваюцца",
  notify_not_supported_help: "Выкарыстоўвайце Chrome/Edge на Android. На iOS дадайце сайт на Home Screen.",
  notify_denied: "Апавяшчэнні забароненыя",
  notify_denied_help: "Дазвольце апавяшчэнні ў наладах браўзера.",
  notify_failed: "Падпіска не атрымалася",
  notify_enabled_msg: "Апавяшчэнні ўключаны",
  notify_enabled_help: "Вы атрымаеце push, калі статус зменіцца.",
  subscribe_error: "Памылка падпіскі",
  route_error: "Памылка маршруту"
}

    };


    function tryLang(v) {
      try {
        const s0 = String(v || "").trim().toLowerCase().replaceAll("_", "-");
        const base = s0.split("-", 1)[0];
        if (SUPPORTED_LANGS.includes(base)) return base;
        // common aliases
        if (base === "kz" || base === "kaz") return "kk";
        if (base === "uzb") return "uz";
        if (base === "tgk" || base === "taj" || base === "tj") return "tg";
        if (base === "kir" || base === "kg") return "ky";
        if (base === "bel" || base === "by") return "be";
        return "";
      } catch (e) {
        return "";
      }
    }

    function normLang(v) {
      return tryLang(v) || "en";
    }

    let CURRENT_LANG = "en";

    function t(key) {
      const pack = UI[CURRENT_LANG] || UI.en;
      return (pack && pack[key]) || (UI.en && UI.en[key]) || key;
    }

    function setNotifyMsg(html, kind) {
      const el = document.getElementById("notifyMsg");
      if (!el) return;
      el.style.display = html ? "block" : "none";
      el.innerHTML = html || "";
      el.style.color = (kind === "err") ? "#a00000" : "";
    }

    function normalizePlate(v) {
      return (v || "").toUpperCase().trim().replaceAll(" ", "").replaceAll("-", "");
    }

    function getInitialLang() {
      try {
        const l = new URLSearchParams(window.location.search).get("lang") || "";
        const ln = tryLang(l);
        if (ln) return ln;
      } catch (e) {}
      try {
        const ls = localStorage.getItem("lang") || "";
        const ln2 = tryLang(ls);
        if (ln2) return ln2;
      } catch (e) {}
      try {
        const list = (navigator.languages && navigator.languages.length) ? navigator.languages : [];
        for (const cand of list) {
          const ln3 = tryLang(cand);
          if (ln3) return ln3;
        }
      } catch (e) {}
      try {
        const nav = (navigator.language || navigator.userLanguage || "") || "";
        const ln4 = tryLang(nav);
        if (ln4) return ln4;
      } catch (e) {}
      return "en";
    }

    function setCurrentLang(lang, opts) {
      const ln = normLang(lang);
      const changed = (ln !== CURRENT_LANG);
      CURRENT_LANG = ln;

      try { localStorage.setItem("lang", ln); } catch (e) {}

      try {
        const u = new URL(window.location.href);
        u.searchParams.set("lang", ln);

        // Keep current plate in the URL as well (if user already typed it)
        const pEl = document.getElementById("plate");
        const pNow = pEl ? normalizePlate(pEl.value) : "";
        if (pNow) u.searchParams.set("plate", pNow);

        if (opts && opts.reload && changed) {
          window.location.replace(u.toString()); // full reload
          return;
        }
        history.replaceState(null, "", u.toString());
      } catch (e) {}

      try { document.documentElement.lang = ln; } catch (e) {}
      applyLangUI();
      updateLangButtons();
    }

    function updateLangButtons() {
      const bar = document.getElementById("langbar");
      if (!bar) return;
      const btns = bar.querySelectorAll("button[data-lang]");
      btns.forEach((b) => {
        const l = normLang(b.getAttribute("data-lang") || "");
        if (l === CURRENT_LANG) b.classList.add("active");
        else b.classList.remove("active");
      });
    }

    function applyLangUI() {
      const h2 = document.getElementById("titleH2");
      if (h2) h2.textContent = t("title");

      const plate = document.getElementById("plate");
      if (plate) plate.setAttribute("placeholder", t("plate_ph"));

      const btn = document.getElementById("btn");
      if (btn) btn.textContent = t("btn_check");

      const bn = document.getElementById("btnNotify");
      if (bn && bn.style.display !== "none") {
        if (bn.disabled && bn.textContent === UI.en.btn_enabled) bn.textContent = t("btn_enabled");
        else bn.textContent = t("btn_notify");
      }
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
        u.searchParams.set("lang", CURRENT_LANG);
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

      setMapNote(t("loading_route"), false);

      try {
        const url = `${API_BASE}/api/route?plate=${encodeURIComponent(plate)}&lat=${encodeURIComponent(loc.lat)}&lon=${encodeURIComponent(loc.lon)}&ts=${encodeURIComponent(loc.ts)}&lang=${encodeURIComponent(CURRENT_LANG)}`;
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
          L.marker([data.origin.lat, data.origin.lon]).addTo(_map).bindPopup(t("origin"));
        }
        if (data.dest && data.dest.lat != null && data.dest.lon != null) {
          L.marker([data.dest.lat, data.dest.lon]).addTo(_map).bindPopup(t("destination_pin"));
        }

        setMapNote(data.note || "", false);
        setTimeout(() => { if (_map) _map.invalidateSize(); }, 80);
      } catch (e) {
        setMapNote(t("route_error") + ": " + e, true);
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

      show(`<div class="muted">${t("getting_location")}</div>`);

      let loc;
      try {
        loc = await getLocation();
      } catch (e) {
        destroyMap();
        show(`<b>${t("err_location")}:</b> ${e.message}<div class="muted">${t("help_location")}</div>`, "err");
        return;
      }

      show(`<div class="muted">${t("loading_status")}</div>`);

      try {
        const url = `${API_BASE}/api/status?plate=${encodeURIComponent(plate)}&lat=${encodeURIComponent(loc.lat)}&lon=${encodeURIComponent(loc.lon)}&ts=${encodeURIComponent(loc.ts)}&lang=${encodeURIComponent(CURRENT_LANG)}`;
        const res = await fetch(url);
        const data = await readJsonOrText(res);

        if (!res.ok) {
          destroyMap();
          show(`<b>${t("err_error")}:</b> ${data.detail || res.statusText}`, "err");
          document.getElementById("btnNotify").style.display = "none";
          setNotifyMsg("", "");
          return;
        }

        const last = data.last_refresh || "-";

        if (!data.found) {
          destroyMap();
          show(`
            <div class="status-big">${t("no_movement")}</div>
            <div class="muted">${t("last_refresh")}: ${last}</div>
          `, "warn");
          document.getElementById("btnNotify").style.display = "none";
          setNotifyMsg("", "");
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
            const digits = (rest.match(/\d+/g) || []).join("");
            if (digits) {
              const num = digits.replace(/^0+/, "") || "0";
              placeText = `${t("parking")} ${num}`;
            } else if (rest) {
              placeText = `${t("parking")} ${rest}`;
            } else {
              placeText = t("parking");
            }
          }
          // If location begins with a number → show "Dock <location>"
          else if (/^\d/.test(locVal)) {
            placeText = `${t("dock")} ${locVal}`;
          }

          extraTrailerPlace = `
          <div style="margin-top:6px;"><b>${t("trailer")}:</b> ${trailerVal || "-"}</div>
          <div><b>${t("place")}:</b> ${placeText}</div>`;
        }

        show(`
          <div class="status-big">"${data.status_text}"</div>
          <hr style="border:none;border-top:1px solid #ddd;margin:12px 0;">
          <div><b>${t("destination")}:</b> ${destLink}</div>
          <div><b>${t("departure_time")}:</b> ${data.scheduled_departure || "-"}</div>
          <div><b>${t("report_office")}:</b> ${data.report_in_office_at || "-"}</div>
          ${extraTrailerPlace}
          <div class="muted" style="margin-top:8px;">${t("last_refresh")}: ${last}</div>

          <div style="margin-top:12px;"><b>${t("route_map")}:</b></div>
          <div id="map"></div>
          <div id="mapNote" class="muted" style="margin-top:6px;"></div>
        `, "ok");

        setTimeout(() => renderRouteMap(plate, loc), 0);

        if (data.push_enabled && data.vapid_public_key) {
          const bn = document.getElementById("btnNotify");
          bn.style.display = "block";
          bn.onclick = () => enableNotifications(plate, loc, data.vapid_public_key);
          bn.disabled = false;
          bn.textContent = t("btn_notify");
          bn.style.opacity = "";
          setNotifyMsg("", "");
        } else {
          document.getElementById("btnNotify").style.display = "none";
          setNotifyMsg("", "");
        }

      } catch (e) {
        destroyMap();
        show(`<b>${t("err_network")}:</b> ${e}`, "err");
        document.getElementById("btnNotify").style.display = "none";
        setNotifyMsg("", "");
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
        const bn0 = document.getElementById("btnNotify");
        if (bn0) {
          bn0.disabled = true;
          bn0.textContent = t("btn_enabling");
          bn0.style.opacity = "0.75";
        }
        setNotifyMsg(`<div class="muted">${t("btn_enabling")}</div>`, "");

        if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
          setNotifyMsg(`<b>${t("notify_not_supported")}</b><div class="muted">${t("notify_not_supported_help")}</div>`, "err");
          const bn1 = document.getElementById("btnNotify");
          if (bn1) { bn1.disabled = false; bn1.textContent = t("btn_notify"); bn1.style.opacity = ""; }
          return;
        }

        const perm = await Notification.requestPermission();
        if (perm !== 'granted') {
          setNotifyMsg(`<b>${t("notify_denied")}</b><div class="muted">${t("notify_denied_help")}</div>`, "err");
          const bn2 = document.getElementById("btnNotify");
          if (bn2) { bn2.disabled = false; bn2.textContent = t("btn_notify"); bn2.style.opacity = ""; }
          return;
        }

        const reg = await navigator.serviceWorker.register('/sw.js');

        // Wait until the service worker is activated (otherwise PushManager.subscribe can fail with "no active Service Worker")
        const sw = reg.installing || reg.waiting || reg.active;
        if (!sw) throw new Error("Service Worker registration failed.");
        await new Promise((resolve, reject) => {
          if (sw.state === 'activated') return resolve();
          const tmo = setTimeout(() => reject(new Error("Service Worker activation timeout.")), 8000);
          sw.addEventListener('statechange', () => {
            if (sw.state === 'activated') {
              clearTimeout(tmo);
              resolve();
            }
          });
        });

        const existing = await reg.pushManager.getSubscription();
        const sub = existing || await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
        });

        const resp = await fetch(`${API_BASE}/api/subscribe?plate=${encodeURIComponent(plate)}&lat=${encodeURIComponent(loc.lat)}&lon=${encodeURIComponent(loc.lon)}&ts=${encodeURIComponent(loc.ts)}&lang=${encodeURIComponent(CURRENT_LANG)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(sub),
        });

        const data = await readJsonOrText(resp);
        if (!resp.ok) {
          setNotifyMsg(`<b>${t("notify_failed")}:</b> ${data.detail || resp.statusText}`, "err");
          const bn3 = document.getElementById("btnNotify");
          if (bn3) { bn3.disabled = false; bn3.textContent = t("btn_notify"); bn3.style.opacity = ""; }
          return;
        }

        const bn = document.getElementById("btnNotify");
        if (bn) {
          bn.disabled = true;
          bn.textContent = t("btn_enabled");
          bn.style.opacity = "0.75";
          bn.onclick = null;
        }
        setNotifyMsg(`<b>${t("notify_enabled_msg")}</b><div class="muted">${t("notify_enabled_help")}</div>`, "");
      } catch (e) {
        setNotifyMsg(`<b>${t("subscribe_error")}:</b> ${e}`, "err");
        const bn4 = document.getElementById("btnNotify");
        if (bn4) { bn4.disabled = false; bn4.textContent = t("btn_notify"); bn4.style.opacity = ""; }
      }
    }

    document.getElementById("btn").addEventListener("click", checkStatus);
    document.getElementById("plate").addEventListener("keydown", (e) => {
      if (e.key === "Enter") checkStatus();
    });

    // Language buttons
    (function initLang() {
      setCurrentLang(getInitialLang(), { reload: false });

      const bar = document.getElementById("langbar");
      if (bar) {
        bar.addEventListener("click", (ev) => {
          const btn = ev.target && ev.target.closest ? ev.target.closest("button[data-lang]") : null;
          if (!btn) return;
          const l = btn.getAttribute("data-lang") || "en";
          setCurrentLang(l, { reload: true });
        });
      }
    })();

    // Restore plate from URL or last usage and auto-run once
    (function initPlate() {
      const p = getInitialPlate();
      if (p) {
        document.getElementById("plate").value = p;
        setTimeout(() => { checkStatus(); }, 50);
      } else {
        applyLangUI();
        updateLangButtons();
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
