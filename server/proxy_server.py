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

SCORES_URL       = "https://site.api.espn.com/apis/site/v2/sports/{}/scoreboard"
SCORES_LEAGUES   = {"mlb": "baseball/mlb", "nba": "basketball/nba"}
SCORES_TTL_S     = 30      # live scores refresh
SCORES_ROTATE_S  = 20      # with several games, the panel moves to the next one this often
WIN_SHOW_S       = 300     # after a game ends, show the final (and celebrate a win) this long
NY_LOCATIONS     = {"New York", "Brooklyn"}   # Yankees, Mets, Knicks, Nets

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

# ── Custom layouts (web app → 07 Customize Layout) ─────────────────────────
# Each element: [type, x, y, a, b, color]. Must match EL_* in the firmware and
# EL in server/static/panel-editor.js (which documents a / b). color 0 = the
# element's usual colour. Types 8 and 12 (countdowns) were removed in v7.9.
EL = {"bullet": 1, "time": 2, "leave": 3, "ticker": 4, "weather": 5, "dot": 6, "clock": 7,
      "label": 9, "bullet2": 10, "time2": 11}
LAYOUT_V = 2       # v1 (7.8): leave a = format; v2: leave a = font size, b bit 2 = format
LAYOUT_PRESETS = {
    "standard":   [[EL["time"], 32, 0, 2, 1, 0], [EL["bullet"], 8, 24, 7, 0, 0],
                   [EL["ticker"], 17, 20, 47, 3, 0]],
    "minimalist": [[EL["dot"], 2, 1, 0, 0, 0], [EL["bullet"], 21, 16, 7, 0, 0],
                   [EL["time"], 33, 12, 1, 0, 0]],
    "two_rows":   [[EL["bullet"], 6, 8, 6, 0, 0], [EL["time"], 59, 1, 2, 2, 0],
                   [EL["bullet2"], 6, 24, 6, 0, 0], [EL["time2"], 59, 17, 2, 2, 0]],
}
MAX_LAYOUT_ELEMENTS = 16

DEFAULT_CONFIG = {
    "line":          "L",
    "stop_parent":   "L14",
    "stop_name":     "Morgan Av",
    "platform":      "N",
    "walk_minutes":  10,
    "show_leave_by": True,
    "show_alerts":   True,
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
    "custom_layout": False,  # draw the user's own layout instead of the built-in ones
    "layout_single": LAYOUT_PRESETS["standard"],
    "layout_two":    LAYOUT_PRESETS["two_rows"],   # used by Two Trains and Both Directions
    "layout_label":  "HOME",
    "layout_v":      LAYOUT_V,
    "alt_routes":    False,  # suggest a faster route to alt_dest
    "alt_dest":      "",
    "alt_dest_name": "",
    "alt_origins":   [],     # up to 2 more stations you can walk to: {stop, name, walk}
    "show_scores":   False,  # live game score card every 2 min while a game is on
    "scores_mlb":    True,
    "scores_nba":    True,
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


def migrate_config(raw: dict) -> dict:
    """Bring a saved config up to date (in memory; saved on the next change)."""
    if raw.get("layout_v", 1) < 2:
        for key in ("layout_single", "layout_two"):
            if isinstance(raw.get(key), list):
                new = []
                for e in raw[key]:
                    if not isinstance(e, list) or len(e) != 6 or e[0] in (8, 12):
                        continue                              # countdowns are gone
                    e = list(e)
                    if e[0] == EL["leave"]:                   # a was the format
                        e[3], e[4] = 1, (e[4] & 3) | (4 if e[3] == 1 else 0)
                    new.append(e)
                raw[key] = new
        raw["layout_v"] = LAYOUT_V
    # Minimalist mode (removed in v7.9) becomes the Minimalist custom layout.
    if raw.pop("minimalist", False) and not raw.get("custom_layout"):
        lay = [list(e) for e in LAYOUT_PRESETS["minimalist"]]
        leave = raw.get("show_leave_by", DEFAULT_CONFIG["show_leave_by"])
        alerts = raw.get("show_alerts", DEFAULT_CONFIG["show_alerts"])
        wx = raw.get("show_weather", DEFAULT_CONFIG["show_weather"])
        if leave or alerts or wx:                             # second row, like before
            lay[1][2], lay[2][2] = 13, 9
            if leave or alerts:
                lay.append([EL["ticker"], 0, 24, 64, (1 if leave else 0) | (2 if wx else 0), 0])
            else:
                lay.append([EL["weather"], 32, 24, 1, 1, 0])
        raw["custom_layout"], raw["layout_single"] = True, lay
    return raw


def load_config(panel_id: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(panel_id)) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("config is not an object")
        cfg.update(migrate_config(raw))
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
    "show_scores":   _bool,
    "scores_mlb":    _bool,
    "scores_nba":    _bool,
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


def _alt_origins(v):
    if not isinstance(v, list) or len(v) > MAX_ALT_ORIGINS:
        raise ValueError(f"must be a list of up to {MAX_ALT_ORIGINS} stations")
    out = []
    for o in v:
        if not isinstance(o, dict):
            raise ValueError("each station must be an object")
        stop = _text(8, r"[A-Z0-9]{1,8}")(o.get("stop", ""))
        out.append({"stop": stop, "name": _text(60)(o.get("name", "") or stop),
                    "walk": _int_range(0, 60)(o.get("walk", 5))})
    return out


def _layout(v):
    if not isinstance(v, list) or len(v) > MAX_LAYOUT_ELEMENTS:
        raise ValueError(f"must be a list of up to {MAX_LAYOUT_ELEMENTS} elements")
    ranges = [(1, 11), (-10, 74), (-10, 42), (0, 64), (0, 15), (0, 65535)]
    out = []
    for e in v:
        if (not isinstance(e, list) or len(e) != 6
                or any(isinstance(n, bool) or not isinstance(n, int) for n in e)):
            raise ValueError("each element must be 6 whole numbers")
        if any(not lo <= n <= hi for n, (lo, hi) in zip(e, ranges)) or e[0] not in EL.values():
            raise ValueError("element value out of range")
        out.append(list(e))
    return out


CONFIG_SCHEMA.update({
    "custom_layout": _bool,
    "layout_single": _layout,
    "layout_two":    _layout,
    "layout_label":  _text(15),
    "alt_routes":    _bool,
    "alt_dest":      _text(8, r"[A-Z0-9]{0,8}"),
    "alt_dest_name": _text(60),
    "alt_origins":   _alt_origins,
})


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


def order_stops(sequences: list[list[str]]) -> list[str]:
    """Merge stop sequences from many trips into one route order.

    Each trip says "this stop comes before that one"; a topological sort
    of all of those gives an order every trip agrees with, so local stops
    land between express ones and partial trips fit in. Where the trips
    don't decide (e.g. two branches), it keeps following the branch it is
    on, then falls back to the MTA stop ID, which is numbered in route
    order within each trunk (L01 8 Av ... L29 Canarsie).
    """
    seqs = [s for s in sequences if s]
    nodes = list(dict.fromkeys(sid for s in seqs for sid in s))
    succ = {n: set() for n in nodes}
    indeg = dict.fromkeys(nodes, 0)
    for seq in seqs:
        for a, b in zip(seq, seq[1:]):
            if a != b and b not in succ[a]:
                succ[a].add(b)
                indeg[b] += 1
    order, remaining = [], set(nodes)
    ready = [n for n in nodes if indeg[n] == 0]
    while remaining:
        if ready:
            follow = [n for n in ready if order and n in succ[order[-1]]]
            n = min(follow or ready)
        else:                                   # conflicting trips: break the loop
            n = min(remaining, key=lambda x: (indeg[x], x))
        ready = [r for r in ready if r != n]
        remaining.discard(n)
        order.append(n)
        for m in succ[n]:
            if m in remaining:
                indeg[m] -= 1
                if indeg[m] == 0:
                    ready.append(m)
    return order


def get_stations(line: str) -> list[dict]:
    """Stations with live trips on this line, in the order the train runs
    (the S direction: top of the map to the bottom, e.g. 8 Av -> Canarsie)."""
    def load():
        names, south, north = {}, [], []
        for trip in get_feed(line).filter_trips(line_id=line):
            seq, direction = [], None
            for update in trip.stop_time_updates:
                sid = update.stop_id or ""
                parent = sid[:-1] if sid[-1:] in ("N", "S") else sid
                if not parent:
                    continue
                direction = direction or sid[-1:]
                if parent not in names:
                    names[parent] = (getattr(update, "stop_name", None) or parent).strip()
                if not seq or seq[-1] != parent:
                    seq.append(parent)
            (north if direction == "N" else south).append(seq)
        # S-bound trips give the order; N-bound ones (reversed) fill any gaps.
        order = order_stops(south + [list(reversed(s)) for s in north])
        return [{"id": sid, "name": names[sid]} for sid in order]

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


ALERT_DETAIL_LIMIT = 600   # web app ticker: keep one scroll pass reasonable


def _english(translations) -> str:
    """Plain English text from a GTFS-RT TranslatedString ("" if none)."""
    if not translations:
        return ""
    return next((t.text for t in translations if t.language in ("en", "")), translations[0].text)


def shorten_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:-") + "…"


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
            full = normalize_alert_text(_english(tr))
            if not full:
                continue
            # The longer description ("What's happening", alternatives, ...)
            # is only shown in the web app.
            detail = normalize_alert_text(_english(a.description_text.translation))
            if detail.lower().startswith(full.lower().rstrip(".")):
                detail = detail[len(full):].lstrip(" .")
            detail = shorten_words(detail, ALERT_DETAIL_LIMIT)
            alerts.append({
                "routes":  {ie.route_id for ie in a.informed_entity if ie.route_id},
                "stops":   {ie.stop_id for ie in a.informed_entity if ie.stop_id},
                "periods": [(p.start or 0, p.end or 0) for p in a.active_period],
                "text":    condense_alert_text(full),   # for the panel
                "full":    full,                        # for the web app
                "detail":  detail,                      # web app: MTA's longer description
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
        out.append((a["level"], here, a.get("full", a["text"]), a.get("detail", "")))
    out.sort(key=lambda x: (-x[0], not x[1]))
    seen, alerts = set(), []
    for level, here, text, detail in out:
        if text not in seen:          # the MTA sometimes repeats an alert per route
            seen.add(text)
            alerts.append({"text": text, "detail": detail, "level": level})
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

_static_stations: dict | None = None


def static_stations() -> dict:
    """{parent stop id: {"name", "lat", "lon"}} from the GTFS stops list
    bundled with nyct_gtfs (every subway station, whatever is running)."""
    global _static_stations
    if _static_stations is None:
        out = {}
        path = os.path.join(os.path.dirname(nyct_gtfs.__file__), "gtfs_static", "stops.txt")
        try:
            with open(path, newline="") as f:
                for row in csv.DictReader(f):
                    if not row.get("parent_station"):
                        out[row["stop_id"]] = {"name": row["stop_name"].strip(),
                                               "lat": float(row["stop_lat"]), "lon": float(row["stop_lon"])}
        except (OSError, ValueError, KeyError):
            log.exception("Could not read the station list")
        _static_stations = out
    return _static_stations


def station_coords(stop_parent: str) -> tuple[float, float]:
    """Lat/lon of a station (central NYC if unknown)."""
    st = static_stations().get(stop_parent)
    return (st["lat"], st["lon"]) if st else NYC_CENTER


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


# ── Live scores (ESPN's public scoreboard JSON, no key) ──────────────────

def led_color(primary: str, alternate: str = "") -> tuple[int, int, int]:
    """A team colour that shows up on the LED panel: dark navies (Yankees,
    Mets) fall back to the alternate colour or get brightened."""
    def parse(h):
        try:
            return hex_to_rgb(h) if h and len(h.lstrip("#")) == 6 else None
        except ValueError:
            return None
    c = parse(primary) or parse(alternate) or (200, 200, 200)
    alt = parse(alternate)
    if max(c) < 110 and alt and max(alt) > max(c):
        c = alt
    peak = max(c) or 1
    k = 210 / peak if peak < 210 else 1.0
    return tuple(min(255, int(v * k)) for v in c)


def short_status(detail: str) -> str:
    """ESPN's status, short enough for the panel (10 chars):
    '4:21 - 3rd' -> 'Q3 4:21', 'End of 1st' -> 'END Q1', '2:00 - OT' -> 'OT 2:00'."""
    d = (detail or "").strip()
    m = re.fullmatch(r"(\d+:\d+(?:\.\d)?) - (\d)(?:st|nd|rd|th)", d, re.I)
    if m:
        return f"Q{m[2]} {m[1]}"
    m = re.fullmatch(r"(\d+:\d+(?:\.\d)?) - (\d*OT)", d, re.I)
    if m:
        return f"{m[2].upper()} {m[1]}"
    m = re.fullmatch(r"End of (\d)(?:st|nd|rd|th)(?: Quarter)?", d, re.I)
    if m:
        return f"END Q{m[1]}"
    return d.upper()[:10]


def parse_scoreboard(data: dict, league: str) -> list[dict]:
    games = []
    for ev in data.get("events", []):
        st = ev.get("status", {}).get("type", {})
        comp = (ev.get("competitions") or [{}])[0]
        teams = {}
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            teams[c.get("homeAway")] = {
                "abbr": (t.get("abbreviation") or "?")[:4],
                "score": int(c.get("score") or 0) if str(c.get("score") or "0").isdigit() else 0,
                "color": led_color(t.get("color", ""), t.get("alternateColor", "")),
                "ny": t.get("location") in NY_LOCATIONS,
            }
        if "home" not in teams or "away" not in teams:
            continue
        games.append({"id": str(ev.get("id")), "league": league, "state": st.get("state"),
                      "when": st.get("shortDetail") or "",
                      "detail": short_status(st.get("shortDetail")),
                      "away": teams["away"], "home": teams["home"]})
    return games


_scores_failed: dict = {}
_seen_live: set = set()          # game ids we've seen in progress
_ended: dict = {}                # game id -> (time it ended, final game)


def scoreboards(leagues: list[str]) -> list[dict]:
    """Today's games for these leagues (all states). Also notes when a game
    we saw in progress goes final, for the 5-minute win celebration."""
    games = []
    for lg in leagues:
        key = ("scores", lg)
        if time.monotonic() - _scores_failed.get(key, -WEATHER_RETRY_S) < WEATHER_RETRY_S:
            board = cache.peek(key, MAX_STALE_S) or []
        else:
            def load(lg=lg):
                r = requests.get(SCORES_URL.format(SCORES_LEAGUES[lg]), timeout=WEATHER_TIMEOUT_S)
                r.raise_for_status()
                return parse_scoreboard(r.json(), lg)
            try:
                board = cache.get(key, SCORES_TTL_S, load)
            except Exception:
                log.warning("Scores unavailable (%s)", lg, exc_info=True)
                _scores_failed[key] = time.monotonic()
                board = []
        games += board
    now = time.time()
    for g in games:
        if g["state"] == "in":
            _seen_live.add(g["id"])
        elif g["state"] == "post" and g["id"] in _seen_live and g["id"] not in _ended:
            _ended[g["id"]] = (now, g)
    for gid, (t, _) in list(_ended.items()):
        if now - t > 6 * 3600:
            del _ended[gid]
    return games


def _ny(g: dict) -> bool:
    return g["away"]["ny"] or g["home"]["ny"]


def live_games(leagues: list[str]) -> list[dict]:
    """Games in progress, New York teams first, then the closest games."""
    games = [g for g in scoreboards(leagues) if g["state"] == "in"]
    ny = [g for g in games if _ny(g)]
    rest = sorted((g for g in games if not _ny(g)),
                  key=lambda g: abs(g["away"]["score"] - g["home"]["score"]))
    return ny + rest


def just_finished(leagues: list[str]) -> list[dict]:
    """Games that ended in the last WIN_SHOW_S seconds, NY first."""
    now = time.time()
    done = [g for t, g in _ended.values() if now - t <= WIN_SHOW_S and g["league"] in leagues]
    return sorted(done, key=lambda g: not _ny(g))


def next_game(games: list[dict]) -> str:
    """'NYK 7:30PM' for the next scheduled game (NY teams first), or ''."""
    pre = [g for g in games if g["state"] == "pre"]
    for g in sorted(pre, key=lambda g: not _ny(g)):
        m = re.search(r"(\d{1,2}:\d{2}) ?([AP])M", g.get("when", ""), re.I)
        if m:
            team = g["away"] if g["away"]["ny"] else g["home"] if g["home"]["ny"] else g["away"]
            return f"{team['abbr']} {m[1]}{m[2].upper()}"
    return ""


def winner_side(g: dict) -> int:
    """1 = away won, 2 = home won, 0 = no celebration (tie, or a NY team lost)."""
    a, h = g["away"], g["home"]
    if a["score"] == h["score"]:
        return 0
    side, team, other = (1, a, h) if a["score"] > h["score"] else (2, h, a)
    return 0 if (other["ny"] and not team["ny"]) else side


def _game_fields(g: dict) -> dict:
    out = {"score_on": 1, "score_status": g["detail"],
           "score_seq": zlib.crc32(g["id"].encode()) & 0x7FFFFFFF or 1,
           "score_final": 1 if g["state"] == "post" else 0,
           "score_win": winner_side(g) if g["state"] == "post" else 0}
    for side in ("away", "home"):
        t = g[side]
        out[f"{side}_abbr"] = t["abbr"]
        out[f"{side}_score"] = t["score"]
        out[f"{side}_r"], out[f"{side}_g"], out[f"{side}_b"] = t["color"]
    return out


SAMPLE_GAMES = {   # for "Preview game" / "Preview win"
    "live": {"id": "preview-live", "league": "mlb", "state": "in", "detail": "TOP 9TH",
             "away": {"abbr": "NYY", "score": 5, "color": led_color("003087"), "ny": True},
             "home": {"abbr": "BOS", "score": 3, "color": led_color("bd3039"), "ny": False}},
    "win":  {"id": "preview-win", "league": "nba", "state": "post", "detail": "FINAL",
             "away": {"abbr": "BOS", "score": 104, "color": led_color("008348"), "ny": False},
             "home": {"abbr": "NY", "score": 112, "color": led_color("1d428a", "f58426"), "ny": True}},
}


def score_payload(cfg: dict) -> dict:
    """Scores-only mode. scores_only=1 makes the panel show games instead of
    trains: a just-finished game for 5 min (celebrating a win), else a live
    game (NY first; several take turns every 20 s), else score_msg."""
    preview = cfg.get("preview_score") or {}
    if preview.get("until", 0) > time.time():
        g = dict(SAMPLE_GAMES[preview["kind"]], id=f"preview-{preview['seq']}")
        return {"scores_only": 1, **_game_fields(g)}
    if not cfg["show_scores"]:
        return {"scores_only": 0, "score_on": 0}
    leagues = [lg for lg in ("mlb", "nba") if cfg[f"scores_{lg}"]]
    games = scoreboards(leagues)
    live = live_games(leagues)
    done = just_finished(leagues)
    if done and (_ny(done[0]) or not any(_ny(g) for g in live)):
        return {"scores_only": 1, **_game_fields(done[0])}
    if live:
        ny = [g for g in live if _ny(g)]
        pool = ny or live[:3]
        return {"scores_only": 1, **_game_fields(pool[int(time.time() // SCORES_ROTATE_S) % len(pool)])}
    nxt = next_game(games)
    return {"scores_only": 1, "score_on": 0,
            "score_msg": f"NEXT {nxt}" if nxt else ("NO GAMES TODAY" if not games else "NO LIVE GAMES")}


# ── Alternate routes ──────────────────────────────────────────────────────
#
# With a destination station set, the server works out, from the live
# feeds of every line, when you'd actually arrive by each option: your
# usual train, or any train from up to 3 starting stations, direct or with
# one transfer. Each live trip lists its predicted time at every stop still
# ahead of it, so "arrive at destination" comes straight from the MTA.
# Transfers are any two stations within ALT_WALK_RADIUS_M of each other
# (real transfer complexes like Union Sq, plus short street walks), costing
# 2 min + walking time. The panel only switches to an alternative when it
# arrives ALT_MIN_SAVE_MIN or more minutes sooner than your usual train.

ALT_WALK_RADIUS_M = 300
ALT_SAME_STOP_MIN = 1.0     # change trains at the same platform/station
ALT_MIN_SAVE_MIN  = 5
ALT_MAX_WAIT_MIN  = 45      # ignore trains leaving more than this far ahead
ALT_TTL_S         = 30
MAX_ALT_ORIGINS   = 2       # extra starting stations (plus the panel's own)

_nearby: dict | None = None


def _dist_m(a: dict, b: dict) -> float:
    x = (b["lon"] - a["lon"]) * math.cos(math.radians((a["lat"] + b["lat"]) / 2)) * 111320
    y = (b["lat"] - a["lat"]) * 110540
    return math.hypot(x, y)


def nearby_stations(parent: str) -> list[tuple[str, float]]:
    """[(other station, transfer minutes)] within walking distance."""
    global _nearby
    if _nearby is None:
        st = static_stations()
        ids = list(st)
        near = {i: [] for i in ids}
        for n, a in enumerate(ids):
            for b in ids[n + 1:]:
                d = _dist_m(st[a], st[b])
                if d <= ALT_WALK_RADIUS_M:
                    mins = 2 + d / 80              # ~80 m/min including stairs
                    near[a].append((b, mins))
                    near[b].append((a, mins))
        _nearby = near
    return _nearby.get(parent, [])


def base_line(route_id: str) -> str:
    """'6X' -> '6', 'FX' -> 'F' (express variants share the line's bullet)."""
    r = (route_id or "").upper()
    return r[:-1] if len(r) > 1 and r.endswith("X") and r[:-1] in LINES else r


def live_trips() -> list[dict]:
    """Every live trip on every line: route + [(station, N/S, arrive, depart)]."""
    urls = {}
    for line in LINES:
        urls.setdefault(feed_url(line), line)
    trips = []
    for line in urls.values():
        try:
            feed = get_feed(line)
        except Exception:
            continue                                  # one feed down: route with the rest
        for t in feed.trips:
            seq = []
            for u in t.stop_time_updates:
                sid = u.stop_id or ""
                arr, dep = u.arrival or u.departure, u.departure or u.arrival
                if not sid or arr is None:
                    continue
                if arr.tzinfo is None:
                    arr, dep = arr.replace(tzinfo=EASTERN), dep.replace(tzinfo=EASTERN)
                seq.append((sid[:-1] if sid[-1:] in ("N", "S") else sid, sid[-1:], arr, dep))
            if len(seq) >= 2:
                trips.append({"route": base_line(t.route_id), "stops": seq})
    return trips


def plan_routes(origins: list[tuple[str, int]], dest: str, now: datetime,
                trips: list[dict]) -> list[dict]:
    """Every direct and one-transfer option from the origins to dest.
    origins: [(station, walk minutes)]. Each option: origin, walk, line,
    dir, board, via, line2, arrive (datetime, includes the final walk)."""
    import bisect
    dest_walk = {dest: 0.0, **dict(nearby_stations(dest))}

    best_after = []                    # per trip: earliest (arrive, station) at dest after stop i
    for t in trips:
        acc, best = [None] * len(t["stops"]), None
        for i in range(len(t["stops"]) - 1, -1, -1):
            acc[i] = best
            p, _, arr, _ = t["stops"][i]
            if p in dest_walk:
                cand = arr + timedelta(minutes=dest_walk[p])
                if best is None or cand < best:
                    best = cand
        best_after.append(acc)

    at_stop: dict = {}                 # station -> sorted [(depart, trip, index)]
    for ti, t in enumerate(trips):
        for i, (p, _, _, dep) in enumerate(t["stops"]):
            at_stop.setdefault(p, []).append((dep, ti, i))
    for lst in at_stop.values():
        lst.sort(key=lambda x: x[0])
    times = {p: [x[0] for x in lst] for p, lst in at_stop.items()}

    options = []
    for origin, walk in origins:
        ready = now + timedelta(minutes=walk)
        latest = ready + timedelta(minutes=ALT_MAX_WAIT_MIN)
        lst = at_stop.get(origin, [])
        for dep, ti, i in lst[bisect.bisect_left(times.get(origin, []), ready):]:
            if dep > latest:
                break
            t = trips[ti]
            leg = {"origin": origin, "walk": walk, "line": t["route"], "dir": t["stops"][i][1],
                   "board": dep, "via": None, "line2": None}
            if best_after[ti][i]:
                options.append({**leg, "arrive": best_after[ti][i]})
            for j in range(i + 1, len(t["stops"])):          # one transfer
                x, _, arr_x, _ = t["stops"][j]
                if x in dest_walk:
                    break                                     # already there
                for y, tt in [(x, ALT_SAME_STOP_MIN)] + nearby_stations(x):
                    ready2 = arr_x + timedelta(minutes=tt)
                    lst2 = at_stop.get(y, [])
                    seen = set()
                    for dep2, ti2, i2 in lst2[bisect.bisect_left(times.get(y, []), ready2):]:
                        if dep2 > ready2 + timedelta(minutes=30):
                            break
                        t2 = trips[ti2]
                        key = (t2["route"], t2["stops"][i2][1])
                        if ti2 == ti or t2["route"] == t["route"] or key in seen:
                            continue
                        seen.add(key)                         # first train per line/direction
                        if best_after[ti2][i2]:
                            options.append({**leg, "alight": x, "via": y, "line2": t2["route"],
                                            "arrive": best_after[ti2][i2]})
    options.sort(key=lambda o: (o["arrive"], o["via"] is not None, o["board"]))
    return options


def _station_name(p: str, names: dict | None = None) -> str:
    """The name the user saved for a station, else the GTFS name."""
    return (names or {}).get(p) or static_stations().get(p, {}).get("name", p)


def _saved_names(cfg: dict) -> dict:
    return {cfg["stop_parent"]: cfg["stop_name"],
            **{o["stop"]: o["name"] for o in cfg.get("alt_origins", []) if o.get("name")}}


def describe_route(o: dict, names: dict | None = None) -> str:
    """'From Flushing Av (leave 8:41): J to Myrtle Av, then M. Arrive 9:22'."""
    leave = fmt_time(o["board"] - timedelta(minutes=o["walk"]))
    text = f"From {_station_name(o['origin'], names)} (leave {leave}): {o['line']}{_transfer_text(o, names)}"
    return f"{text}. Arrive {fmt_time(o['arrive'])}"


def _transfer_text(o: dict, names: dict | None = None) -> str:
    """' to 14 St-Union Sq, then 6' or, when you walk between differently
    named stations, ' to Broadway-Lafayette St, walk to Bleecker St, then 6'."""
    if not o["via"]:
        return ""
    off, on = _station_name(o.get("alight") or o["via"], names), _station_name(o["via"], names)
    return f" to {off}, then {o['line2']}" if off == on else f" to {off}, walk to {on}, then {o['line2']}"


_alt_cache: dict = {}


def alt_route(cfg: dict, now: datetime) -> dict:
    """{'usual': option|None, 'best': option|None, 'active': bool, 'saves': min}."""
    dest = cfg.get("alt_dest") or ""
    home = cfg["stop_parent"]
    origins = [(home, cfg["walk_minutes"])] + [(o["stop"], o["walk"]) for o in cfg.get("alt_origins", [])
                                               if o.get("stop") and o["stop"] != home]
    key = (dest, tuple(origins), cfg["line"], cfg["platform"], int(now.timestamp() // ALT_TTL_S))
    if key in _alt_cache:
        return _alt_cache[key]
    result = {"usual": None, "best": None, "active": False, "saves": 0}
    if dest:
        try:
            options = plan_routes(origins, dest, now, live_trips())
        except Exception:
            log.exception("Alternate route planning failed")
            options = []
        usual = next((o for o in options if o["origin"] == home and o["line"] == cfg["line"]
                      and o["dir"] == cfg["platform"]), None)
        best = options[0] if options else None
        saves = int((usual["arrive"] - best["arrive"]).total_seconds() // 60) if usual and best else 0
        active = bool(best) and (usual is None or saves >= ALT_MIN_SAVE_MIN)
        if active and usual and (best["origin"], best["line"], best["via"]) == (usual["origin"], usual["line"], usual["via"]):
            active = False
        result = {"usual": usual, "best": best, "active": active, "saves": saves}
    _alt_cache.clear()
    _alt_cache[key] = result
    return result


def alt_payload(cfg: dict, now: datetime) -> dict:
    """Panel fields: alt=1 swaps the display to the faster route."""
    if not cfg.get("alt_routes") or not cfg.get("alt_dest") or cfg["demo"]:
        return {"alt": 0}
    r = alt_route(cfg, now)
    if not r["active"]:
        return {"alt": 0}
    b = r["best"]
    # Short for the panel: "ALT: Flushing Av, leave 8:41. J to Myrtle Av, then M. Arr 9:22 (6 min sooner)"
    names = _saved_names(cfg)
    leave = fmt_time(b["board"] - timedelta(minutes=b["walk"]))
    text = f"ALT: {_station_name(b['origin'], names)}, leave {leave}. {b['line']}{_transfer_text(b, names)}"
    text += f". Arr {fmt_time(b['arrive'])}"
    text += f" ({r['saves']} min sooner)" if r["usual"] else " (usual train doesn't get there)"
    cr, cg, cb = hex_to_rgb(LINE_COLORS.get(b["line"], "#888888"))
    return {"alt": 1, "alt_line": b["line"][:2], "alt_time": fmt_time(b["board"]),
            "alt_r": cr, "alt_g": cg, "alt_b": cb, "alt_text": text[:140]}


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
    """(train time, leave-by time, error)."""
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
    # A custom layout shows whatever elements it contains, whatever the
    # Display Options toggles say, so fetch what its elements need.
    layout = None
    if cfg["custom_layout"]:
        two_rows = (cfg["dual"] or cfg["both_dirs"]) and not cfg["demo"]
        layout = cfg["layout_two"] if two_rows else cfg["layout_single"]
    kinds = {e[0] for e in layout or []}
    tickers = [e for e in layout or [] if e[0] == EL["ticker"]]
    show_leave = cfg["show_leave_by"] if layout is None else (
        EL["leave"] in kinds or any(e[4] & 1 for e in tickers))
    show_alerts = cfg["show_alerts"] if layout is None else bool(tickers)
    show_wx = cfg["show_weather"] if layout is None else (
        EL["weather"] in kinds or any(e[4] & 2 for e in tickers))
    wx = get_weather(stop_parent) if show_wx else None

    # Always 200: even when the MTA is down the panel still needs its
    # brightness / power / colour settings.
    payload = {
        "train_time":     train_time,
        "leave_by":       leave_by if show_leave else "",
        "alert_msg":      alert_text if show_alerts else "",
        "service_status": level,
        "line":           line,
        "logo_r": r, "logo_g": g, "logo_b": b,
        "logo_dark":      1 if line in DARK_TEXT_LINES else 0,
        "show_leave_by":  show_leave,
        "show_alerts":    show_alerts,
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
    if layout is not None:
        payload.update({"layout": layout, "label": cfg["layout_label"],
                        "clock_s": now.hour * 3600 + now.minute * 60 + now.second})
    payload.update(score_payload(cfg))
    payload.update(alt_payload(cfg, now) if not payload.get("scores_only") else {"alt": 0})
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


@app.post("/api/preview-scores")
def api_preview_scores():
    """{"kind": "live"|"win"}: show a sample game (or a win) on the panel."""
    pid = require_panel_id()
    kind = (request.get_json(silent=True) or {}).get("kind", "live")
    if kind not in SAMPLE_GAMES:
        abort(400, description="kind must be live or win")
    with _config_lock:
        cfg = load_config(pid)
        cfg["preview_score"] = {"kind": kind, "seq": secrets.randbelow(10**6),
                                "until": int(time.time()) + (30 if kind == "win" else 20)}
        save_config(pid, cfg)
    return jsonify({"ok": True})


@app.get("/api/all-stations")
def api_all_stations():
    """Every subway station with the lines running there now (for pickers)."""
    def load():
        lines: dict = {}
        try:
            for t in live_trips():
                for p, _, _, _ in t["stops"]:
                    lines.setdefault(p, set()).add(t["route"])
        except Exception:
            log.warning("Could not read live lines for the station list", exc_info=True)
        order = {l: i for i, l in enumerate(LINES)}
        return sorted(({"id": sid, "name": st["name"],
                        "lines": sorted(lines.get(sid, ()), key=lambda l: order.get(l, 99))}
                       for sid, st in static_stations().items()), key=lambda s: (s["name"], s["id"]))
    return jsonify(cache.get(("all-stations",), STATIONS_TTL_S, load))


@app.get("/api/alt-route")
def api_alt_route():
    """What the panel would suggest right now (for the web app's hint)."""
    cfg = load_config(require_panel_id())
    if not cfg.get("alt_dest"):
        return jsonify({"ok": False, "reason": "no destination"})
    r = alt_route(cfg, datetime.now(tz=EASTERN))
    def brief(o):
        return describe_route(o, _saved_names(cfg)) if o else None
    return jsonify({"ok": True, "active": r["active"], "saves": r["saves"],
                    "usual": brief(r["usual"]), "best": brief(r["best"])})


@app.get("/api/scores")
def api_scores():
    """What the score card would show now, for the web app's toggle hint."""
    cfg = load_config(require_panel_id())
    leagues = [lg for lg in ("mlb", "nba") if cfg[f"scores_{lg}"]]
    games = live_games(leagues)
    return jsonify({"live": len(games), "games": [
        f"{g['away']['abbr']} {g['away']['score']} – {g['home']['abbr']} {g['home']['score']} · {g['detail']}"
        for g in games[:3]]})


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


def _asset_version() -> int:
    """Changes whenever a file in server/static changes (cache-busting)."""
    try:
        folder = os.path.join(BASE_DIR, "static")
        return int(max(os.path.getmtime(os.path.join(folder, f)) for f in os.listdir(folder)))
    except (OSError, ValueError):
        return 0


@app.get("/")
def index():
    resp = app.make_response(render_template(
        "index.html",
        line_colors=LINE_COLORS,
        direction_labels=DIRECTION_LABELS,
        layout_presets=LAYOUT_PRESETS,
        asset_v=_asset_version(),
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
