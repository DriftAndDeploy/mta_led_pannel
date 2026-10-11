"""
MTA Subway Panel — server
=========================
Runs on the Raspberry Pi behind the Cloudflare tunnel (whenisthetraincoming.com).

  * /next-trains        what each ESP32 panel polls (train time, colours, settings)
  * /                   the phone web app used to configure a panel
  * /api/...            JSON endpoints used by the web app
  * /firmware/...       over-the-air firmware updates for the panels

Run locally:  python proxy_server.py        (http://localhost:5000)
On the Pi it runs as the mta-proxy systemd service — see README.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, abort, jsonify, render_template, request, send_file
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
    "auto_update":   True,
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
    "auto_update":   _bool,
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


def record_seen(panel_id: str, fw_build: int | None) -> None:
    with _seen_lock:
        _seen[panel_id] = {"ts": time.time(), "fw_build": fw_build}


def panel_status(panel_id: str) -> dict:
    with _seen_lock:
        info = _seen.get(panel_id)
    if not info:
        return {"online": False, "last_seen_s": None, "fw_build": None}
    age = time.time() - info["ts"]
    return {"online": age < ONLINE_WINDOW_S, "last_seen_s": int(age),
            "fw_build": info["fw_build"]}


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


def clean_alert_text(text: str, limit: int = 80) -> str:
    text = _BULLET_RE.sub(r"\1", text)      # "[A] trains" -> "A trains"
    text = _ICON_RE.sub("", text)           # drop "[accessibility icon]" etc.
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return text


def classify_alert(text: str) -> int:
    t = text.lower()
    if any(w in t for w in ("suspend", "no service", "not running", "not stopping")):
        return 2
    return 1


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
            text = clean_alert_text(raw)
            if not text:
                continue
            alerts.append({
                "routes":  {ie.route_id for ie in a.informed_entity if ie.route_id},
                "periods": [(p.start or 0, p.end or 0) for p in a.active_period],
                "text":    text,
                "level":   classify_alert(text),
            })
        return alerts

    return cache.get(("alerts",), ALERTS_TTL_S, load)


def alert_for_line(line: str, now_ts: float) -> tuple[str, int]:
    """Most severe alert active right now for this line -> (text, level)."""
    try:
        alerts = get_alerts()
    except Exception:
        log.warning("Alerts unavailable", exc_info=True)
        return "", 0
    best = None
    for a in alerts:
        if line not in a["routes"]:
            continue
        if a["periods"] and not any(start <= now_ts <= (end or float("inf"))
                                    for start, end in a["periods"]):
            continue   # expired or not started yet
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


@app.get("/next-trains")
def next_trains():
    panel_id = clean_panel_id(request.args.get("panel_id")) or "DEFAULT"
    fw_build = request.args.get("fw", type=int)
    record_seen(panel_id, fw_build)

    cfg = load_config(panel_id)
    line, stop_parent, platform = cfg["line"], cfg["stop_parent"], cfg["platform"]
    if cfg["demo"]:
        line, station, platform = demo_selection(panel_id, int(time.time() // DEMO_INTERVAL_S))
        stop_parent = station["id"] if station else stop_parent

    now = datetime.now(tz=EASTERN)
    walk = cfg["walk_minutes"]
    train_time, leave_by, error = "--:--", "", None
    try:
        dt = next_departure(get_feed(line), stop_parent + platform, line, walk, now)
        if dt:
            train_time = dt.strftime("%I:%M").lstrip("0")
            leave_by = (dt - timedelta(minutes=walk)).strftime("%I:%M").lstrip("0")
    except Exception:
        log.exception("Train feed error (panel %s, line %s)", panel_id, line)
        error = "feed"

    alert_text, level = alert_for_line(line, now.timestamp())
    r, g, b = hex_to_rgb(LINE_COLORS.get(line, "#888888"))
    fw = firmware_info() if cfg["auto_update"] else None

    # Always 200: even when the MTA is down the panel still needs its
    # brightness / power / colour settings.
    return jsonify({
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
        "panel_off":      1 if cfg["panel_off"] else 0,
        "time_r": cfg["time_r"], "time_g": cfg["time_g"], "time_b": cfg["time_b"],
        "fw_build":       fw["build"] if fw else 0,
        "error":          error,
    })


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
        return jsonify(load_config(pid))
    update, errors = validate_update(request.get_json(silent=True))
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    with _config_lock:
        cfg = load_config(pid)
        cfg.update(update)
        save_config(pid, cfg)
    log.info("Config updated [%s]: %s", pid, update)
    return jsonify({"ok": True, "config": cfg})


@app.get("/api/status")
def api_status():
    pid = require_panel_id()
    fw = firmware_info()
    return jsonify({**panel_status(pid), "latest_fw_build": fw["build"] if fw else None})


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
