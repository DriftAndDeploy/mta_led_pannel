"""
MTA Subway Panel — server
=========================
Runs on the Raspberry Pi behind the Cloudflare tunnel (whenisthetraincoming.com).

  * /next-trains        what each ESP32 panel polls (train time, colours, settings)
  * /                   the phone web app used to configure a panel
  * /api/...            JSON endpoints used by the web app
  * /firmware/...       over-the-air firmware updates for the panels

Weather comes from Open-Meteo (free, no API key) for the panel's station.

Run locally:  python proxy_server.py        (http://localhost:5000)
On the Pi it runs as the mta-proxy systemd service — see README.md.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import os
import random
import re
import secrets
import threading
import time
import zlib
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, abort, jsonify, render_template, request, send_file
import nyct_gtfs
from nyct_gtfs import NYCTFeed
from nyct_gtfs.compiled_gtfs import gtfs_realtime_pb2

# ── Paths & settings ───────────────────────────────────────────────────────

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR     = os.environ.get("MTA_DATA_DIR", os.path.join(BASE_DIR, "data"))
FIRMWARE_BIN = os.environ.get("MTA_FIRMWARE_BIN",
                              os.path.join(BASE_DIR, "firmware", "firmware.bin"))
PORT         = int(os.environ.get("PORT", "5000"))

EASTERN          = ZoneInfo("America/New_York")
ALERTS_URL       = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts"
HTTP_TIMEOUT_S   = 8
FEED_TTL_S       = 20     # train feeds change every ~30 s at the MTA
ALERTS_TTL_S     = 60
STATIONS_TTL_S   = 3600
MAX_STALE_S      = 300    # serve cached data this long if the MTA is down
DEMO_INTERVAL_S  = 45     # demo mode: new line every 45 s
ONLINE_WINDOW_S  = 60     # a panel counts as online if seen within this window

WEATHER_URL       = "https://api.open-meteo.com/v1/forecast"
WEATHER_TTL_S     = 900    # Open-Meteo updates current conditions every 15 min
WEATHER_TIMEOUT_S = 4      # keep a slow weather API from delaying the panel
WEATHER_RETRY_S   = 120    # after a failed fetch, wait this long before retrying
UMBRELLA_HOURS    = 12     # look this far ahead for rain
UMBRELLA_CHANCE   = 50     # % chance of rain in any of those hours -> umbrella
NYC_CENTER        = (40.7128, -74.0060)

ARRIVING_WINDOW_S = 60     # a train due within this many seconds is "arriving"

os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("mta")

# ── Subway lines ───────────────────────────────────────────────────────────

LINES = ["1", "2", "3", "4", "5", "6", "7",
         "A", "C", "E", "B", "D", "F", "M",
         "G", "J", "Z", "L", "N", "Q", "R", "W", "SI"]

LINE_COLORS = {
    "1": "#EE352E", "2": "#EE352E", "3": "#EE352E",
    "4": "#00933C", "5": "#00933C", "6": "#00933C",
    "7": "#B933AD",
    "A": "#2850AD", "C": "#2850AD", "E": "#2850AD",
    "B": "#FF6319", "D": "#FF6319", "F": "#FF6319", "M": "#FF6319",
    "G": "#6CBE45", "J": "#996633", "Z": "#996633",
    "L": "#A7A9AC", "N": "#FCCC0A", "Q": "#FCCC0A", "R": "#FCCC0A", "W": "#FCCC0A",
    "SI": "#2850AD",
}
DARK_TEXT_LINES = {"N", "Q", "R", "W"}   # black letter on yellow bullet

# (N platform label, S platform label)
DIRECTION_LABELS = {line: ("Uptown", "Downtown") for line in LINES}
DIRECTION_LABELS.update({
    "7":  ("Manhattan", "Queens"),
    "G":  ("Northbound", "Southbound"),
    "J":  ("Manhattan", "Brooklyn"),
    "Z":  ("Manhattan", "Brooklyn"),
    "L":  ("Manhattan", "Brooklyn"),
    "SI": ("St George", "Tottenville"),
})

DEFAULT_CONFIG = {
    "line":          "L",
    "stop_parent":   "L14",
    "stop_name":     "Morgan Av",
    "platform":      "N",
    "walk_minutes":  10,
    "show_leave_by": True,
    "show_alerts":   True,
    "minimalist":    False,
    "demo":          False,
    "brightness":    80,
    "panel_off":     False,
    "time_r":        255,
    "time_g":        255,
    "time_b":        255,
    "color_all":     False,  # use the time colour for all panel text
    "auto_update":   True,
    "latch_blanking": 4,     # panel anti-ghosting (1-8); raise if faint lines appear
    "show_weather":  False,  # temperature + icon (+ umbrella if rain is likely)
    "animations":    False,  # train pulls across the panel when one is arriving
    "night_mode":    False,  # panel off overnight
    "night_start":   "01:00",
    "night_end":     "06:00",
    # Both directions: same line/station, uptown row + downtown row.
    "both_dirs":     False,
    # Two-train mode: a second line/station shown as a second row.
    "dual":          False,
    "line2":         "L",
    "stop_parent2":  "L14",
    "stop_name2":    "Morgan Av",
    "platform2":     "S",
    "walk_minutes2": 10,
}


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# ── Small TTL cache (single-flight, serves stale data if a refresh fails) ──

class TTLCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict = {}
        self._key_locks: dict = {}

    def get(self, key, ttl: float, loader):
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:  # only one thread refreshes a given key
            with self._lock:
                hit = self._data.get(key)
                if hit and time.monotonic() - hit[0] < ttl:
                    return hit[1]
            try:
                value = loader()
            except Exception:
                if hit and time.monotonic() - hit[0] < MAX_STALE_S:
                    log.warning("Refresh failed for %s — serving cached copy", key, exc_info=True)
                    return hit[1]
                raise
            with self._lock:
                self._data[key] = (time.monotonic(), value)
            return value

    def peek(self, key, max_age: float):
        """Cached value if younger than max_age, without refreshing."""
        with self._lock:
            hit = self._data.get(key)
        return hit[1] if hit and time.monotonic() - hit[0] < max_age else None


cache = TTLCache()


# ── Panel config storage ──────────────────────────────────────────────────

_config_lock = threading.Lock()
PANEL_ID_RE = re.compile(r"^[A-Z0-9]{4,8}$")


def clean_panel_id(raw) -> str | None:
    pid = (raw or "").strip().upper()
    return pid if PANEL_ID_RE.match(pid) else None


def config_path(panel_id: str) -> str:
    return os.path.join(DATA_DIR, f"config_{panel_id}.json")


def load_config(panel_id: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(panel_id)) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        log.exception("Unreadable config for %s — using defaults", panel_id)
    return cfg


def save_config(panel_id: str, cfg: dict) -> None:
    path = config_path(panel_id)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)   # atomic: a crash never leaves a half-written file


def _bool(v):
    if isinstance(v, bool):
        return v
    raise ValueError("must be true/false")


def _int_range(lo, hi):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"must be a number {lo}-{hi}")
        return max(lo, min(hi, int(v)))
    return check


def _choice(options):
    def check(v):
        if v not in options:
            raise ValueError(f"must be one of {sorted(options)}")
        return v
    return check


def _text(max_len, pattern=None):
    def check(v):
        if not isinstance(v, str):
            raise ValueError("must be text")
        v = v.strip()[:max_len]
        if pattern and not re.fullmatch(pattern, v):
            raise ValueError("invalid value")
        return v
    return check


CONFIG_SCHEMA = {
    "line":          _choice(set(LINES)),
    "stop_parent":   _text(8, r"[A-Z0-9]{1,8}"),
    "stop_name":     _text(60),
    "platform":      _choice({"N", "S"}),
    "walk_minutes":  _int_range(0, 90),
    "show_leave_by": _bool,
    "show_alerts":   _bool,
    "minimalist":    _bool,
    "demo":          _bool,
    "brightness":    _int_range(10, 100),
    "panel_off":     _bool,
    "time_r":        _int_range(0, 255),
    "time_g":        _int_range(0, 255),
    "time_b":        _int_range(0, 255),
    "color_all":     _bool,
    "auto_update":   _bool,
    "latch_blanking": _int_range(1, 8),
    "show_weather":  _bool,
    "animations":    _bool,
    "night_mode":    _bool,
    "night_start":   _text(5, r"(?:[01]\d|2[0-3]):[0-5]\d"),
    "night_end":     _text(5, r"(?:[01]\d|2[0-3]):[0-5]\d"),
    "both_dirs":     _bool,
    "dual":          _bool,
    "line2":         _choice(set(LINES)),
    "stop_parent2":  _text(8, r"[A-Z0-9]{1,8}"),
    "stop_name2":    _text(60),
    "platform2":     _choice({"N", "S"}),
    "walk_minutes2": _int_range(0, 90),
}


def validate_update(data) -> tuple[dict, list[str]]:
    if not isinstance(data, dict):
        return {}, ["body must be a JSON object"]
    clean, errors = {}, []
    for key, value in data.items():
        check = CONFIG_SCHEMA.get(key)
        if check is None:
            continue   # ignore unknown keys (e.g. from an older web app)
        try:
            clean[key] = check(value)
        except ValueError as e:
            errors.append(f"{key}: {e}")
    return clean, errors


# ── Panel heartbeat (in memory — shows "online" in the web app) ───────────

_seen_lock = threading.Lock()
_seen: dict[str, dict] = {}


def record_seen(panel_id: str, fw_build: int | None, ssid: str | None = None,
                rssi: int | None = None) -> None:
    with _seen_lock:
        _seen[panel_id] = {"ts": time.time(), "fw_build": fw_build,
                           "ssid": (ssid or "")[:32] or None, "rssi": rssi}


def panel_status(panel_id: str) -> dict:
    with _seen_lock:
        info = _seen.get(panel_id)
    if not info:
        return {"online": False, "last_seen_s": None, "fw_build": None,
                "ssid": None, "rssi": None}
    age = time.time() - info["ts"]
    return {"online": age < ONLINE_WINDOW_S, "last_seen_s": int(age),
            "fw_build": info["fw_build"], "ssid": info["ssid"], "rssi": info["rssi"]}


# ── WiFi hand-off (send an upcoming network to a panel) ───────────────────
#
# The web app stores {id, ssid, password} in the panel's config as
# "wifi_pending". /next-trains hands it to the panel, the panel echoes the id
# back as ?wifi_ok=, and the password is then deleted from the Pi — only the
# network name is kept, as "wifi_delivered".

WIFI_KEYS = ("wifi_pending", "wifi_delivered")


# ── Night schedule ────────────────────────────────────────────────────────
#
# Between night_start and night_end (Eastern; may cross midnight) the panel
# is sent panel_off=1. Tapping power in the web app during the window sets
# night_skip_until = the window's end, so the panel stays on until morning
# and the schedule resumes the next night.

def _hhmm(v: str) -> dtime:
    h, m = v.split(":")
    return dtime(int(h), int(m))


def night_window(cfg: dict, now: datetime) -> datetime | None:
    """End of the night window if `now` is inside it, else None."""
    if not cfg.get("night_mode"):
        return None
    start, end = _hhmm(cfg["night_start"]), _hhmm(cfg["night_end"])
    t = now.time().replace(second=0, microsecond=0)
    if start == end:
        return None
    inside = start <= t < end if start < end else (t >= start or t < end)
    if not inside:
        return None
    end_dt = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    return end_dt if end_dt > now else end_dt + timedelta(days=1)


def night_off(cfg: dict, now: datetime) -> bool:
    end = night_window(cfg, now)
    return end is not None and cfg.get("night_skip_until", 0) < now.timestamp()


def clock_label(t: datetime | dtime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def public_config(cfg: dict) -> dict:
    """Config as shown to the web app: never includes WiFi passwords."""
    return {k: v for k, v in cfg.items() if k not in WIFI_KEYS}


def wifi_handoff(panel_id: str, ack_id: str | None) -> dict | None:
    """Process a panel's ack; return the network still waiting to be sent."""
    with _config_lock:
        cfg = load_config(panel_id)
        pending = cfg.get("wifi_pending")
        if not pending:
            return None
        if ack_id and ack_id == pending.get("id"):
            cfg.pop("wifi_pending", None)
            cfg["wifi_delivered"] = {"ssid": pending["ssid"], "at": int(time.time())}
            save_config(panel_id, cfg)
            log.info("WiFi network \"%s\" delivered to panel %s — password deleted",
                     pending["ssid"], panel_id)
            return None
        return {"id": pending["id"], "ssid": pending["ssid"], "pass": pending["password"]}


# ── MTA data ──────────────────────────────────────────────────────────────

def feed_url(line: str) -> str:
    return NYCTFeed._train_to_url[line]


def get_feed(line: str) -> NYCTFeed:
    """Live feed for a line. Lines sharing an MTA feed share one cache entry."""
    url = feed_url(line)

    def load():
        # Fetch ourselves: NYCTFeed.refresh() has no timeout and can hang.
        r = requests.get(url, timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        feed = NYCTFeed(line, fetch_immediately=False)
        feed.load_gtfs_bytes(r.content)
        return feed

    return cache.get(("feed", url), FEED_TTL_S, load)


def next_departure(feed, stop_id: str, line: str, walk_minutes: int, now: datetime):
    """Soonest train at stop_id that you can still catch if you leave now."""
    soonest = None
    for trip in feed.filter_trips(line_id=line):
        for update in trip.stop_time_updates:
            if update.stop_id != stop_id:
                continue
            t = update.departure or update.arrival
            if t is not None:
                if t.tzinfo is None:          # feed times are naive Eastern
                    t = t.replace(tzinfo=EASTERN)
                if t - timedelta(minutes=walk_minutes) >= now and (soonest is None or t < soonest):
                    soonest = t
            break
    return soonest


def get_stations(line: str) -> list[dict]:
    """Stations with live trips on this line, alphabetical."""
    def load():
        stations = {}
        for trip in get_feed(line).filter_trips(line_id=line):
            for update in trip.stop_time_updates:
                sid = update.stop_id or ""
                parent = sid[:-1] if sid[-1:] in ("N", "S") else sid
                if parent and parent not in stations:
                    name = (getattr(update, "stop_name", None) or parent).strip()
                    stations[parent] = name
        return sorted(({"id": k, "name": v} for k, v in stations.items()),
                      key=lambda s: s["name"])

    return cache.get(("stations", line), STATIONS_TTL_S, load)


_BULLET_RE = re.compile(r"\[([A-Z0-9]{1,3})\]")
_ICON_RE   = re.compile(r"\[[^\]]*\]")


ALERT_LIMIT = 80   # panel ticker space (firmware buffer is 100)

# Condensing, used only when an alert is too long. Each step is tried in
# turn, and the first version that fits wins, so short alerts read exactly as
# the MTA wrote them.
_ABBREVIATIONS = [   # (pattern, replacement), applied case-insensitively
    (r"\bare running with (?:some |severe |extensive )?delays\b", "are delayed"),
    (r"\bis running with (?:some |severe |extensive )?delays\b", "is delayed"),
    (r"\bare not running\b", "not running"),
    (r"\bare running\b", "run"),
    (r"\bis running\b", "runs"),
    (r"\bafter we (?:addressed|responded to|removed|fixed|cleared)\b", "after"),
    (r"\bwhile we (?:address|respond to|remove|clear)\b", "due to"),
    (r"\bwhile (?:we|crews) (?:make|complete|perform) (?:repairs|track work|work)\b", "for repairs"),
    (r"\bwhile we (?:fix|repair)\b", "to fix"),
    (r"\bbecause of\b", "due to"),
    (r"\bmechanical problems?\b", "mech problem"),
    (r"\bsignal problems?\b", "signal issue"),
    (r"\bswitch problems?\b", "switch issue"),
    (r"\b(?:an |a )?earlier incident\b", "earlier incident"),
    (r"\bin both directions\b", "both ways"),
    (r"\bminutes\b", "min"),
    (r"\bminute\b", "min"),
    (r"\bnorthbound\b", "N/B"),
    (r"\bsouthbound\b", "S/B"),
    (r"\bmanhattan-bound\b", "Mhtn-bound"),
    (r"\bbrooklyn-bound\b", "Bklyn-bound"),
    (r"\bstreet\b", "St"),
    (r"\bavenue\b", "Av"),
    (r"\bboulevard\b", "Blvd"),
    (r"\bparkway\b", "Pkwy"),
    (r"\bsquare\b", "Sq"),
    (r"\bjunction\b", "Jct"),
    (r"\bcenter\b", "Ctr"),
    (r"\bheights\b", "Hts"),
    (r"\bstations\b", "stops"),
    (r"\bstation\b", "stop"),
    (r"\band\b", "&"),
]
# Trailing "why" clause; the "what" matters more on a 64 px panel.
_REASON_RE = re.compile(
    r"[,;]?\s+(?:while|after|as|because|due to|for|so|to fix|following)\b.*$", re.I)
_FILLER_RE = re.compile(r"\b(?:the|some|currently|please|on a train|on the train)\s+", re.I)


def normalize_alert_text(text: str) -> str:
    text = re.sub(r"\](?=\[)", "]/", text)  # "[2][3]" -> "[2]/[3]"
    text = _BULLET_RE.sub(r"\1", text)      # "[A] trains" -> "A trains"
    text = _ICON_RE.sub("", text)           # drop "[accessibility icon]" etc.
    return re.sub(r"\s+", " ", text).strip()


def _tidy(text: str) -> str:
    text = re.sub(r"\s+([,.;:])", r"\1", re.sub(r"\s+", " ", text)).strip()
    return text[:1].upper() + text[1:]


def condense_alert_text(text: str, limit: int = ALERT_LIMIT) -> str:
    """Shorten an alert to fit the panel, keeping as much meaning as possible."""
    if len(text) <= limit:
        return text
    for pattern, repl in _ABBREVIATIONS:              # 1. abbreviate
        text = re.sub(pattern, repl, text, flags=re.I)
    text = _tidy(text)
    if len(text) <= limit:
        return text
    text = re.split(r"(?<=[.!?])\s+", text)[0]       # 2. first sentence only
    if len(text) <= limit:
        return text
    text = _tidy(_FILLER_RE.sub("", text))            # 3. drop filler words
    if len(text) <= limit:
        return text
    m = _REASON_RE.search(text)                       # 4. drop the "why" clause...
    what = (text[:m.start()] if m else text).rstrip(" .,;")
    place = (m and not re.search(r"\b(?:at|near|between|from)\b", what)   # no location left?
             and re.search(r"\b(at|near) ([A-Z0-9][^,;]*?)\.?$", m.group(0)))
    if place and 12 < len(f"{what} {place[1]} {place[2]}.") <= limit:
        return f"{what} {place[1]} {place[2]}."       # ...but keep where it is
    what += "."
    if 12 < len(what) <= limit:
        return what
    if len(what) > 12:
        text = what
    return text[:limit - 3].rsplit(" ", 1)[0].rstrip(",;:&-/") + "..."   # 5. last resort


def clean_alert_text(text: str, limit: int = ALERT_LIMIT) -> str:
    return condense_alert_text(normalize_alert_text(text), limit)


_SUSPENDED_RE = re.compile(
    r"suspend|no service|not running|not stopping|\bno (?:[a-z0-9]+[/ ])*(?:shuttle )?trains?\b", re.I)


def classify_alert(text: str) -> int:
    return 2 if _SUSPENDED_RE.search(text) else 1


def get_alerts() -> list[dict]:
    """All subway alerts, parsed once per minute and shared by every panel."""
    def load():
        r = requests.get(ALERTS_URL, timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(r.content)
        alerts = []
        for entity in feed.entity:
            if not entity.HasField("alert"):
                continue
            a = entity.alert
            tr = a.header_text.translation or a.description_text.translation
            if not tr:
                continue
            # Prefer the plain English translation over the HTML one.
            raw = next((t.text for t in tr if t.language in ("en", "")), tr[0].text)
            full = normalize_alert_text(raw)
            if not full:
                continue
            alerts.append({
                "routes":  {ie.route_id for ie in a.informed_entity if ie.route_id},
                "stops":   {ie.stop_id for ie in a.informed_entity if ie.stop_id},
                "periods": [(p.start or 0, p.end or 0) for p in a.active_period],
                "text":    condense_alert_text(full),   # for the panel
                "full":    full,                        # for the web app
                "level":   classify_alert(full),   # judged on the full wording
            })
        return alerts

    return cache.get(("alerts",), ALERTS_TTL_S, load)


def _alert_active(a: dict, now_ts: float) -> bool:
    return not a["periods"] or any(start <= now_ts <= (end or float("inf"))
                                   for start, end in a["periods"])


def alert_directions(a: dict, line: str) -> set:
    """{'N'}, {'S'} or set() (both / unknown) for an alert on this line.
    Uses the platform stop IDs the MTA tags (e.g. L14N) when there are any,
    otherwise direction words in the text ("Northbound", "Manhattan-bound")."""
    dirs = {sid[-1] for sid in a.get("stops", ()) if sid[-1:] in ("N", "S")}
    if dirs:
        return dirs if len(dirs) == 1 else set()
    t = a.get("full", a["text"]).lower()
    n_label, s_label = (x.lower() for x in DIRECTION_LABELS.get(line, ("Uptown", "Downtown")))
    words = {
        "N": ["northbound", "uptown", f"{n_label}-bound", f"{n_label} bound"],
        "S": ["southbound", "downtown", f"{s_label}-bound", f"{s_label} bound"],
    }
    found = {d for d, ws in words.items() if any(re.search(rf"\b{re.escape(w)}\b", t) for w in ws)}
    return found if len(found) == 1 else set()


def alerts_for_platform(line: str, stop_parent: str, platform: str, now_ts: float) -> list[dict]:
    """Active alerts for a line that apply to this direction, most severe and
    station-specific first. Full wording (the web app has room for it)."""
    out = []
    for a in get_alerts():
        if line not in a["routes"] or not _alert_active(a, now_ts):
            continue
        dirs = alert_directions(a, line)
        if dirs and platform not in dirs:
            continue
        here = any(sid.startswith(stop_parent) for sid in a.get("stops", ()))
        out.append((a["level"], here, a.get("full", a["text"])))
    out.sort(key=lambda x: (-x[0], not x[1]))
    seen, alerts = set(), []
    for level, here, text in out:
        if text not in seen:          # the MTA sometimes repeats an alert per route
            seen.add(text)
            alerts.append({"text": text, "level": level})
    return alerts


def alert_for_line(line: str, now_ts: float) -> tuple[str, int]:
    """Most severe alert active right now for this line -> (text, level)."""
    try:
        alerts = get_alerts()
    except Exception:
        log.warning("Alerts unavailable", exc_info=True)
        return "", 0
    best = None
    for a in alerts:
        if line not in a["routes"] or not _alert_active(a, now_ts):
            continue   # other line, expired or not started yet
        if best is None or a["level"] > best["level"]:
            best = a
    return (best["text"], best["level"]) if best else ("", 0)


# ── Firmware (OTA) ────────────────────────────────────────────────────────

_FW_TAG_RE = re.compile(rb"##MTAPANEL_BUILD=(\d+)##")
_fw_lock = threading.Lock()
_fw_cache: dict = {}


def firmware_info() -> dict | None:
    """Build number + MD5 of server/firmware/firmware.bin (re-read when it changes)."""
    try:
        st = os.stat(FIRMWARE_BIN)
    except FileNotFoundError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    with _fw_lock:
        if _fw_cache.get("key") != key:
            with open(FIRMWARE_BIN, "rb") as f:
                blob = f.read()
            m = _FW_TAG_RE.search(blob)
            build = int(m.group(1)) if m else 0
            if not m:
                log.warning("firmware.bin has no build tag — OTA disabled until fixed")
            if blob[:1] != b"\xe9":
                log.warning("firmware.bin does not look like an ESP32 app image")
                build = 0
            _fw_cache.update(key=key, build=build, size=st.st_size,
                             md5=hashlib.md5(blob).hexdigest())
            log.info("Firmware available: build %s (%d bytes)", build, st.st_size)
        return {k: _fw_cache[k] for k in ("build", "size", "md5")}


@app.get("/firmware/info")
def firmware_info_route():
    return jsonify(firmware_info() or {"build": 0})


@app.get("/firmware/download")
def firmware_download():
    info = firmware_info()
    if not info or not info["build"]:
        abort(404)
    pid = clean_panel_id(request.args.get("panel_id"))
    log.info("OTA download: panel %s (running %s) -> build %s",
             pid, request.headers.get("x-ESP32-version"), info["build"])
    resp = send_file(FIRMWARE_BIN, mimetype="application/octet-stream",
                     conditional=False, max_age=0)
    resp.headers["x-MD5"] = info["md5"]
    resp.headers["Cache-Control"] = "no-store, no-transform"
    return resp


# ── Panel endpoint ────────────────────────────────────────────────────────

# ── Weather ───────────────────────────────────────────────────────────────

# Icon numbers the firmware draws (0 = none).
WX_ICONS = {"sun": 1, "moon": 2, "partly": 3, "cloud": 4, "rain": 5, "snow": 6, "storm": 7}

_station_coords: dict | None = None


def station_coords(stop_parent: str) -> tuple[float, float]:
    """Lat/lon of a station from the GTFS stops list bundled with nyct_gtfs."""
    global _station_coords
    if _station_coords is None:
        coords = {}
        path = os.path.join(os.path.dirname(nyct_gtfs.__file__), "gtfs_static", "stops.txt")
        try:
            with open(path, newline="") as f:
                for row in csv.DictReader(f):
                    if not row.get("parent_station"):
                        coords[row["stop_id"]] = (float(row["stop_lat"]), float(row["stop_lon"]))
        except (OSError, ValueError, KeyError):
            log.exception("Could not read station coordinates — weather uses central NYC")
        _station_coords = coords
    return _station_coords.get(stop_parent, NYC_CENTER)


def weather_icon(code: int, is_day: bool) -> str:
    """WMO weather code -> one of WX_ICONS."""
    if code in (0, 1):
        return "sun" if is_day else "moon"
    if code == 2:
        return "partly" if is_day else "cloud"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if code >= 95:
        return "storm"
    return "cloud"   # overcast, fog


def parse_weather(data: dict) -> dict:
    cur = data["current"]
    icon = weather_icon(int(cur["weather_code"]), bool(cur.get("is_day", 1)))
    chances = [p for p in (data.get("hourly", {}).get("precipitation_probability") or [])
               if p is not None][:UMBRELLA_HOURS]
    rain_chance = int(max(chances, default=0))
    umbrella = icon in ("rain", "storm") or rain_chance >= UMBRELLA_CHANCE
    return {"temp": int(round(float(cur["temperature_2m"]))), "icon": icon,
            "rain_chance": rain_chance, "umbrella": umbrella}


_weather_failed: dict = {}


def get_weather(stop_parent: str) -> dict | None:
    """Current weather at a station, or None if Open-Meteo can't be reached."""
    lat, lon = station_coords(stop_parent)
    key = ("weather", round(lat, 2), round(lon, 2))

    def load():
        r = requests.get(WEATHER_URL, timeout=WEATHER_TIMEOUT_S, params={
            "latitude": key[1], "longitude": key[2],
            "current": "temperature_2m,weather_code,is_day",
            "hourly": "precipitation_probability",
            "forecast_hours": UMBRELLA_HOURS,
            "temperature_unit": "fahrenheit",
            "timezone": "America/New_York",
        })
        r.raise_for_status()
        return parse_weather(r.json())

    if time.monotonic() - _weather_failed.get(key, -WEATHER_RETRY_S) < WEATHER_RETRY_S:
        return cache.peek(key, MAX_STALE_S)
    try:
        return cache.get(key, WEATHER_TTL_S, load)
    except Exception:
        log.warning("Weather unavailable for %s", stop_parent, exc_info=True)
        _weather_failed[key] = time.monotonic()
        return None


def demo_selection(panel_id: str, slot: int) -> tuple[str, dict | None, str]:
    """Deterministic random line/station/platform for a 45 s demo slot."""
    rng = random.Random(f"{panel_id}:{slot}")
    lines = LINES[:]
    rng.shuffle(lines)
    for line in lines[:4]:          # skip lines with no live trips right now
        try:
            stations = get_stations(line)
        except Exception:
            continue
        if stations:
            return line, rng.choice(stations), rng.choice("NS")
    return lines[0], None, "N"


# ── Holidays & seasons (full-panel animations) ────────────────────────────
#
# With Animations on, on these days the panel plays a 10 s full-screen
# animation every 10 minutes. Numbers must match OCC_* in the firmware.
OCC_KINDS = {"fireworks": 1, "fireworks_usa": 2, "hearts": 3, "shamrocks": 4, "eggs": 5,
             "pumpkin": 6, "leaves": 7, "tree": 8, "flag": 9, "stars": 10, "flowers": 11,
             "sun": 12, "snow": 13, "turtle": 14}
OCCASION_PREVIEW_S = 20

# Solstices/equinoxes: Meeus, Astronomical Algorithms ch. 27 (accurate to
# about a minute for 1000-3000 AD), so the date is right even when the
# moment falls near midnight.
_EQ_MEAN = [  # March, June, September, December
    (2451623.80984, 365242.37404,  0.05169, -0.00411, -0.00057),
    (2451716.56767, 365241.62603,  0.00325,  0.00888, -0.00030),
    (2451810.21715, 365242.01767, -0.11575,  0.00337,  0.00078),
    (2451900.05952, 365242.74049, -0.06223, -0.00823,  0.00032),
]
_EQ_TERMS = [
    (485, 324.96, 1934.136), (203, 337.23, 32964.467), (199, 342.08, 20.186),
    (182, 27.85, 445267.112), (156, 73.14, 45036.886), (136, 171.52, 22518.443),
    (77, 222.54, 65928.934), (74, 296.72, 3034.906), (70, 243.58, 9037.513),
    (58, 119.81, 33718.147), (52, 297.17, 150.678), (50, 21.02, 2281.226),
    (45, 247.54, 29929.562), (44, 325.15, 31555.956), (29, 60.93, 4443.417),
    (18, 155.12, 67555.328), (17, 288.79, 4562.452), (16, 198.04, 62894.029),
    (14, 199.76, 31436.921), (12, 95.39, 14577.848), (12, 287.11, 31931.756),
    (12, 320.81, 34777.259), (9, 227.73, 1222.114), (8, 15.45, 16859.074),
]


_J2000 = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)


def season_start(year: int, i: int) -> datetime:
    """Moment of the March equinox (0), June solstice (1), September
    equinox (2) or December solstice (3), in Eastern time."""
    y = (year - 2000) / 1000
    a, b, c, d, e = _EQ_MEAN[i]
    jde0 = a + b * y + c * y ** 2 + d * y ** 3 + e * y ** 4
    t = (jde0 - 2451545.0) / 36525
    w = math.radians(35999.373 * t - 2.47)
    dl = 1 + 0.0334 * math.cos(w) + 0.0007 * math.cos(2 * w)
    s = sum(A * math.cos(math.radians(B + C * t)) for A, B, C in _EQ_TERMS)
    jde = jde0 + 0.00001 * s / dl
    utc = _J2000 + timedelta(days=jde - 2451545.0, seconds=-69)
    return utc.astimezone(EASTERN)


def easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th weekday (Mon=0) of a month; n=-1 for the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def occasions(year: int) -> dict:
    """{date: (animation, label)} for a year. Holidays win over seasons."""
    seasons = [("flowers", "SPRING EQUINOX"), ("sun", "SUMMER SOLSTICE"),
               ("leaves", "FALL EQUINOX"), ("snow", "WINTER SOLSTICE")]
    days = {season_start(year, i).date(): seasons[i] for i in range(4)}
    days.update({
        date(year, 1, 1):          ("fireworks", f"HAPPY {year}"),
        nth_weekday(year, 1, 0, 3): ("stars", "MLK DAY"),
        date(year, 2, 14):         ("hearts", "VALENTINE'S DAY"),
        nth_weekday(year, 2, 0, 3): ("flag", "PRESIDENTS DAY"),
        date(year, 3, 17):         ("shamrocks", "ST. PATRICK'S DAY"),
        easter(year):              ("eggs", "HAPPY EASTER"),
        nth_weekday(year, 5, 0, -1): ("flag", "MEMORIAL DAY"),
        date(year, 6, 19):         ("stars", "JUNETEENTH"),
        date(year, 7, 4):          ("fireworks_usa", "HAPPY 4TH OF JULY"),
        nth_weekday(year, 9, 0, 1): ("flag", "LABOR DAY"),
        date(year, 10, 31):        ("pumpkin", "HAPPY HALLOWEEN"),
        date(year, 11, 11):        ("flag", "VETERANS DAY"),
        nth_weekday(year, 11, 3, 4): ("leaves", "HAPPY THANKSGIVING"),
        date(year, 12, 24):        ("tree", "CHRISTMAS EVE"),
        date(year, 12, 25):        ("tree", "MERRY CHRISTMAS"),
        date(year, 12, 31):        ("fireworks", "NEW YEAR'S EVE"),
    })
    return days


def occasion_payload(panel_id: str, cfg: dict, now: datetime) -> dict:
    """occasion / occasion_label / occasion_seq for /next-trains (0 = none).
    A new seq makes the panel play right away; otherwise every 10 min."""
    preview = cfg.get("preview_occasion") or {}
    if preview.get("until", 0) > time.time():
        return {"occasion": OCC_KINDS[preview["kind"]], "occasion_label": preview["label"],
                "occasion_seq": preview["seq"]}
    if cfg["animations"]:
        today = now.date()
        hit = occasions(today.year).get(today)
        if hit:
            kind, label = hit
            return {"occasion": OCC_KINDS[kind], "occasion_label": label,
                    "occasion_seq": int(today.strftime("%Y%m%d")) * 100 + OCC_KINDS[kind]}
    return {"occasion": 0, "occasion_label": "", "occasion_seq": 0}


def preview_list(year: int) -> list[tuple[str, str]]:
    """One of each animation, in calendar order, for 'Preview a holiday'."""
    seen, out = set(), []
    for d, (kind, label) in sorted(occasions(year).items()):
        if kind not in seen:
            seen.add(kind)
            out.append((kind, label))
    return out


def arriving_train(feed, stop_id: str, line: str, now: datetime) -> int:
    """Id of a train pulling into stop_id right now (0 = none).

    The id is stable for a given trip, so the panel animates each train once
    even though it polls several times while the train is in the window.
    """
    for trip in feed.filter_trips(line_id=line):
        for update in trip.stop_time_updates:
            if update.stop_id != stop_id:
                continue
            t = update.arrival or update.departure
            if t is not None:
                if t.tzinfo is None:
                    t = t.replace(tzinfo=EASTERN)
                if -15 <= (t - now).total_seconds() <= ARRIVING_WINDOW_S:
                    key = getattr(trip, "trip_id", None) or t.isoformat()
                    return (zlib.crc32(key.encode()) & 0x7FFFFFFF) or 1
            break
    return 0


def arrival_id(panel_id: str, line: str, stop_id: str, now: datetime) -> int:
    try:
        return arriving_train(get_feed(line), stop_id, line, now)
    except Exception:
        return 0   # feed problems are already logged by train_times()


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M").lstrip("0")


def train_times(panel_id: str, line: str, stop_parent: str, platform: str,
                walk: int, now: datetime) -> tuple[str, str, str | None]:
    """(train time, leave-by time, error) for one line/station/platform."""
    try:
        dt = next_departure(get_feed(line), stop_parent + platform, line, walk, now)
    except Exception:
        log.exception("Train feed error (panel %s, line %s)", panel_id, line)
        return "--:--", "", "feed"
    if not dt:
        return "--:--", "", None
    return fmt_time(dt), fmt_time(dt - timedelta(minutes=walk)), None


@app.get("/next-trains")
def next_trains():
    panel_id = clean_panel_id(request.args.get("panel_id")) or "DEFAULT"
    fw_build = request.args.get("fw", type=int)
    record_seen(panel_id, fw_build, request.args.get("ssid"),
                request.args.get("rssi", type=int))
    wifi_new = wifi_handoff(panel_id, request.args.get("wifi_ok"))

    cfg = load_config(panel_id)
    line, stop_parent, platform = cfg["line"], cfg["stop_parent"], cfg["platform"]
    both_dirs = cfg["both_dirs"] and not cfg["demo"]
    if cfg["demo"]:
        line, station, platform = demo_selection(panel_id, int(time.time() // DEMO_INTERVAL_S))
        stop_parent = station["id"] if station else stop_parent
    if both_dirs:
        platform = "N"          # row 1 = N (uptown etc.), row 2 = S

    now = datetime.now(tz=EASTERN)
    train_time, leave_by, error = train_times(panel_id, line, stop_parent, platform,
                                              cfg["walk_minutes"], now)

    alert_text, level = alert_for_line(line, now.timestamp())
    r, g, b = hex_to_rgb(LINE_COLORS.get(line, "#888888"))
    fw = firmware_info() if cfg["auto_update"] else None
    wx = get_weather(stop_parent) if cfg["show_weather"] else None

    # Always 200: even when the MTA is down the panel still needs its
    # brightness / power / colour settings.
    payload = {
        "train_time":     train_time,
        "leave_by":       leave_by if cfg["show_leave_by"] else "",
        "alert_msg":      alert_text if cfg["show_alerts"] else "",
        "service_status": level,
        "line":           line,
        "logo_r": r, "logo_g": g, "logo_b": b,
        "logo_dark":      1 if line in DARK_TEXT_LINES else 0,
        "show_leave_by":  cfg["show_leave_by"],
        "show_alerts":    cfg["show_alerts"],
        "minimalist":     1 if cfg["minimalist"] else 0,
        "brightness":     cfg["brightness"],
        "latch_blanking": cfg["latch_blanking"],
        "panel_off":      1 if cfg["panel_off"] or night_off(cfg, now) else 0,
        "time_r": cfg["time_r"], "time_g": cfg["time_g"], "time_b": cfg["time_b"],
        "color_all":      1 if cfg["color_all"] else 0,
        "fw_build":       fw["build"] if fw else 0,
        "error":          error,
        # weather strip: wx=0 hides it (turned off, or no weather data)
        "wx":             1 if wx else 0,
        "wx_temp":        wx["temp"] if wx else 0,
        "wx_icon":        WX_ICONS.get(wx["icon"], 0) if wx else 0,
        "wx_umbrella":    1 if wx and wx["umbrella"] else 0,
        # seconds of diagnostic test pattern left (0 = normal display)
        "test_pattern":   max(0, int(cfg.get("test_until", 0) - time.time())),
        # nonzero = play the arriving-train animation once for this id
        "arriving":       0,
    }
    if cfg["animations"]:
        payload["arriving"] = arrival_id(panel_id, line, stop_parent + platform, now)
    preview = cfg.get("preview_arrival") or {}
    if preview.get("until", 0) > time.time():
        payload["arriving"] = preview["id"]
    # Two rows: both directions of one line, or two trains (never in demo).
    payload["dual"] = 0
    payload["dirs"] = 0          # 1 = rows are N/S of the same line: firmware adds arrows
    if both_dirs:
        time2, _, error2 = train_times(panel_id, line, stop_parent, "S", cfg["walk_minutes"], now)
        payload.update({
            "dual":            1,
            "dirs":            1,
            "train_time2":     time2,
            "line2":           line,
            "logo2_r": r, "logo2_g": g, "logo2_b": b,
            "service_status2": level,
            "arriving2":       arrival_id(panel_id, line, stop_parent + "S", now)
                               if cfg["animations"] else 0,
        })
        payload["error"] = payload["error"] or error2
    elif cfg["dual"] and not cfg["demo"]:
        line2 = cfg["line2"]
        time2, _, error2 = train_times(panel_id, line2, cfg["stop_parent2"], cfg["platform2"],
                                       cfg["walk_minutes2"], now)
        r2, g2, b2 = hex_to_rgb(LINE_COLORS.get(line2, "#888888"))
        payload.update({
            "dual":            1,
            "train_time2":     time2,
            "line2":           line2,
            "logo2_r": r2, "logo2_g": g2, "logo2_b": b2,
            "service_status2": alert_for_line(line2, now.timestamp())[1],
            "arriving2":       arrival_id(panel_id, line2, cfg["stop_parent2"] + cfg["platform2"], now)
                               if cfg["animations"] else 0,
        })
        payload["error"] = payload["error"] or error2
    payload.update(occasion_payload(panel_id, cfg, now))
    if wifi_new:
        payload["wifi_new"] = wifi_new
    return jsonify(payload)


# ── Web app API ───────────────────────────────────────────────────────────

def require_panel_id() -> str:
    pid = clean_panel_id(request.args.get("panel_id"))
    if not pid:
        abort(400, description="panel_id is required (4-8 letters/numbers)")
    return pid


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    pid = require_panel_id()
    if request.method == "GET":
        return jsonify(public_config(load_config(pid)))
    update, errors = validate_update(request.get_json(silent=True))
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    with _config_lock:
        cfg = load_config(pid)
        cfg.update(update)
        save_config(pid, cfg)
    log.info("Config updated [%s]: %s", pid, update)
    return jsonify({"ok": True, "config": public_config(cfg)})


@app.post("/api/test-pattern")
def api_test_pattern():
    """Show the panel test pattern for 60 s (dead-LED / signal check)."""
    pid = require_panel_id()
    with _config_lock:
        cfg = load_config(pid)
        cfg["test_until"] = int(time.time()) + 60
        save_config(pid, cfg)
    log.info("Test pattern requested for panel %s", pid)
    return jsonify({"ok": True, "seconds": 60})


@app.get("/api/weather")
def api_weather():
    """Weather the panel would show right now (for the web app's toggle hint)."""
    cfg = load_config(require_panel_id())
    wx = get_weather(cfg["stop_parent"])
    if not wx:
        return jsonify({"ok": False})
    return jsonify({"ok": True, "station": cfg["stop_name"], **wx})


@app.post("/api/preview-arrival")
def api_preview_arrival():
    """Play the arriving-train animation on the panel once (next poll)."""
    pid = require_panel_id()
    with _config_lock:
        cfg = load_config(pid)
        cfg["preview_arrival"] = {"id": secrets.randbelow(2**31 - 1) + 1, "until": int(time.time()) + 30}
        save_config(pid, cfg)
    return jsonify({"ok": True})


def _set_occasion_preview(pid: str, kind: str, label: str) -> None:
    with _config_lock:
        cfg = load_config(pid)
        cfg["preview_occasion"] = {"kind": kind, "label": label,
                                   "seq": secrets.randbelow(2**31 - 1) + 1,
                                   "until": int(time.time()) + OCCASION_PREVIEW_S}
        save_config(pid, cfg)


@app.post("/api/preview-occasion")
def api_preview_occasion():
    """Play the next holiday/season animation on the panel (cycles each call)."""
    pid = require_panel_id()
    options = preview_list(datetime.now(tz=EASTERN).year)
    cfg = load_config(pid)
    idx = (cfg.get("preview_occasion_idx", -1) + 1) % len(options)
    kind, label = options[idx]
    _set_occasion_preview(pid, kind, label)
    with _config_lock:
        cfg = load_config(pid)
        cfg["preview_occasion_idx"] = idx
        save_config(pid, cfg)
    return jsonify({"ok": True, "label": label, "kind": kind})


@app.post("/api/turtle")
def api_turtle():
    """Easter egg: a dancing turtle on the panel."""
    _set_occasion_preview(require_panel_id(), "turtle", "")
    return jsonify({"ok": True})


@app.post("/api/night-skip")
def api_night_skip():
    """{"skip": true}: keep the panel on until tonight's window ends.
    {"skip": false}: go back to the schedule (panel off now if in the window)."""
    pid = require_panel_id()
    skip = (request.get_json(silent=True) or {}).get("skip", True) is True
    with _config_lock:
        cfg = load_config(pid)
        end = night_window(cfg, datetime.now(tz=EASTERN))
        cfg["night_skip_until"] = int(end.timestamp()) if skip and end else 0
        save_config(pid, cfg)
    return jsonify({"ok": True, "until": clock_label(end) if skip and end else None})


@app.get("/api/alerts")
def api_alerts():
    """Service alerts for a line/station/direction, for the web app ticker."""
    line = request.args.get("line", "")
    if line not in LINES:
        abort(400, description="unknown line")
    stop = (request.args.get("stop") or "").upper()[:8]
    platform = request.args.get("platform", "N")
    if platform not in ("N", "S"):
        abort(400, description="platform must be N or S")
    try:
        alerts = alerts_for_platform(line, stop, platform, time.time())
    except Exception:
        log.warning("Alerts unavailable", exc_info=True)
        return jsonify({"ok": False, "alerts": []})
    return jsonify({"ok": True, "alerts": alerts})


@app.get("/api/status")
def api_status():
    pid = require_panel_id()
    fw = firmware_info()
    cfg = load_config(pid)
    now = datetime.now(tz=EASTERN)
    end = night_window(cfg, now)
    return jsonify({**panel_status(pid), "latest_fw_build": fw["build"] if fw else None,
                    "night_active": end is not None,          # inside tonight's window
                    "night_off": night_off(cfg, now),         # ...and not skipped
                    "night_until": clock_label(end) if end else None})


@app.route("/api/wifi", methods=["GET", "POST", "DELETE"])
def api_wifi():
    """Send an upcoming WiFi network to the panel (before swapping routers)."""
    pid = require_panel_id()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        ssid = data.get("ssid")
        password = data.get("password", "")
        errors = []
        if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32:
            errors.append("Network name must be 1-32 characters")
        if not isinstance(password, str) or (password and not 8 <= len(password) <= 63):
            errors.append("Password must be 8-63 characters (or empty for an open network)")
        if errors:
            return jsonify({"ok": False, "errors": errors}), 400
        with _config_lock:
            cfg = load_config(pid)
            cfg["wifi_pending"] = {"id": secrets.token_hex(4), "ssid": ssid,
                                   "password": password, "created": int(time.time())}
            save_config(pid, cfg)
        log.info("WiFi network \"%s\" queued for panel %s", ssid, pid)
    elif request.method == "DELETE":
        with _config_lock:
            cfg = load_config(pid)
            if cfg.pop("wifi_pending", None) is not None:
                save_config(pid, cfg)
    cfg = load_config(pid)
    pending = cfg.get("wifi_pending")
    status = panel_status(pid)
    return jsonify({
        "ok": True,
        "current_ssid": status["ssid"],
        "rssi": status["rssi"],
        "pending_ssid": pending["ssid"] if pending else None,
        "delivered": cfg.get("wifi_delivered"),
    })


@app.get("/api/lines")
def api_lines():
    return jsonify([{
        "id": line,
        "color": LINE_COLORS[line],
        "text": "#000" if line in DARK_TEXT_LINES else "#fff",
        "directions": DIRECTION_LABELS[line],
    } for line in LINES])


@app.get("/api/stations")
def api_stations():
    line = (request.args.get("line") or "").upper()
    if line not in LINES:
        abort(400, description="unknown line")
    try:
        return jsonify(get_stations(line))
    except Exception:
        log.exception("Station list error for %s", line)
        return jsonify([]), 503


@app.get("/healthz")
def healthz():
    fw = firmware_info()
    return jsonify({"ok": True, "firmware_build": fw["build"] if fw else None})


@app.get("/")
def index():
    resp = app.make_response(render_template(
        "index.html",
        line_colors=LINE_COLORS,
        direction_labels=DIRECTION_LABELS,
    ))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.errorhandler(400)
def bad_request(e):
    return jsonify({"ok": False, "errors": [getattr(e, "description", "bad request")]}), 400


if __name__ == "__main__":
    log.info("MTA panel server starting on :%d (data: %s)", PORT, DATA_DIR)
    firmware_info()
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=8)
    except ImportError:
        log.warning("waitress not installed — using Flask's development server")
        app.run(host="0.0.0.0", port=PORT, threaded=True)
