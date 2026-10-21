"""
Offline tests for proxy_server.py (no MTA access needed).

    cd server && venv/bin/python -m unittest test_server -v
"""
import hashlib
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ["MTA_DATA_DIR"] = os.path.join(_tmp, "data")
os.environ["MTA_FIRMWARE_BIN"] = os.path.join(_tmp, "firmware.bin")

import proxy_server as ps  # noqa: E402

ET = ps.EASTERN


def at(h, m):
    return datetime(2026, 10, 11, h, m, tzinfo=ET)


def fake_feed(stop_id, times, line="L"):
    """Feed whose trips each stop at stop_id at the given naive Eastern times."""
    trips = [SimpleNamespace(stop_time_updates=[
        SimpleNamespace(stop_id=stop_id, departure=t.replace(tzinfo=None), arrival=None,
                        stop_name="Morgan Av")]) for t in times]
    return SimpleNamespace(filter_trips=lambda line_id: trips)


class DepartureLogic(unittest.TestCase):
    def test_skips_trains_you_cannot_catch(self):
        feed = fake_feed("L14N", [at(10, 20), at(10, 25), at(10, 30)])
        self.assertEqual(ps.next_departure(feed, "L14N", "L", 5, at(10, 18)), at(10, 25))

    def test_one_minute_walk_catches_first_train(self):
        feed = fake_feed("L14N", [at(10, 20), at(10, 25)])
        self.assertEqual(ps.next_departure(feed, "L14N", "L", 1, at(10, 18)), at(10, 20))

    def test_none_when_nothing_catchable(self):
        feed = fake_feed("L14N", [at(10, 20)])
        self.assertIsNone(ps.next_departure(feed, "L14N", "L", 10, at(10, 18)))

    def test_other_platform_ignored(self):
        feed = fake_feed("L14S", [at(10, 25)])
        self.assertIsNone(ps.next_departure(feed, "L14N", "L", 1, at(10, 18)))


class Alerts(unittest.TestCase):
    def test_clean_text_keeps_line_letters(self):
        self.assertEqual(ps.clean_alert_text("Uptown [A][C] trains are delayed [accessibility icon]"),
                         "Uptown A/C trains are delayed")

    def test_truncates_on_word_boundary(self):
        out = ps.clean_alert_text("word " * 40)
        self.assertLessEqual(len(out), 80)
        self.assertTrue(out.endswith("..."))

    def test_short_alerts_are_untouched(self):
        t = "7 trains are running with delays."
        self.assertEqual(ps.clean_alert_text(t), t)

    def test_long_alerts_are_condensed_not_cut(self):
        cases = {
            "Northbound [A] trains are running with delays after we addressed a mechanical problem "
            "on a train at 59 St-Columbus Circle.":
                "N/B A trains are delayed after a mech problem at 59 St-Columbus Circle.",
            "[L] trains are running every 12 minutes between Broadway Junction and 8 Av while we make "
            "repairs to the signal system.":
                "L trains run every 12 min between Broadway Jct & 8 Av.",
            "Southbound [N][Q][R][W] trains are delayed while we respond to a person being struck by a "
            "train at Times Sq-42 St.":
                "S/B N/Q/R/W trains are delayed at Times Sq-42 St.",
            "No [G] trains running between Court Sq and Bedford-Nostrand Avs. Take shuttle buses "
            "instead. Allow additional travel time.":
                "No G trains running between Court Sq & Bedford-Nostrand Avs.",
            "Some northbound [2][3] trains are running on the [4][5] line from Chambers Street to 96 "
            "Street because of a switch problem at 72 St.":
                "N/B 2/3 trains run on 4/5 line from Chambers St to 96 St.",
        }
        for raw, want in cases.items():
            out = ps.clean_alert_text(raw)
            self.assertEqual(out, want)
            self.assertLessEqual(len(out), ps.ALERT_LIMIT)
            self.assertFalse(out.endswith("..."))

    def test_suspension_detected_on_full_text(self):
        self.assertEqual(ps.classify_alert("No G trains running between Court Sq and Bedford"), 2)
        self.assertEqual(ps.classify_alert("No 2/3 trains between 96 St and 242 St"), 2)
        self.assertEqual(ps.classify_alert("S trains are not running between X and Y"), 2)
        self.assertEqual(ps.classify_alert("A trains are delayed"), 1)
        self.assertEqual(ps.classify_alert("Trains are running with delays, no other changes"), 1)

    def test_expired_ignored_and_most_severe_wins(self):
        now = 1_000_000
        alerts = [
            {"routes": {"L"}, "periods": [(0, now - 10)], "text": "old", "level": 2},
            {"routes": {"L"}, "periods": [(now - 10, 0)], "text": "delays", "level": 1},
            {"routes": {"L"}, "periods": [], "text": "suspended", "level": 2},
            {"routes": {"G"}, "periods": [], "text": "other line", "level": 2},
        ]
        with mock.patch.object(ps, "get_alerts", return_value=alerts):
            self.assertEqual(ps.alert_for_line("L", now), ("suspended", 2))
            self.assertEqual(ps.alert_for_line("A", now), ("", 0))


class Api(unittest.TestCase):
    def setUp(self):
        self.client = ps.app.test_client()

    def test_next_trains_payload(self):
        feed = fake_feed("L14N", [at(10, 25)])
        with mock.patch.object(ps, "get_feed", return_value=feed), \
             mock.patch.object(ps, "alert_for_line", return_value=("L trains delayed", 1)), \
             mock.patch.object(ps, "datetime") as dt:
            dt.now.return_value = at(10, 10)
            r = self.client.get("/next-trains?panel_id=abc123&fw=600")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["train_time"], "10:25")
        self.assertEqual(j["leave_by"], "10:15")
        self.assertEqual(j["alert_msg"], "L trains delayed")
        self.assertEqual(j["service_status"], 1)
        self.assertEqual((j["logo_r"], j["logo_g"], j["logo_b"]), (167, 169, 172))
        self.assertEqual(j["latch_blanking"], 4)
        self.assertEqual(j["color_all"], 0)
        status = self.client.get("/api/status?panel_id=ABC123").get_json()
        self.assertTrue(status["online"])
        self.assertEqual(status["fw_build"], 600)

    def test_feed_failure_still_returns_settings(self):
        with mock.patch.object(ps, "get_feed", side_effect=RuntimeError("MTA down")), \
             mock.patch.object(ps, "alert_for_line", return_value=("", 0)):
            r = self.client.get("/next-trains?panel_id=DEAD01")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["train_time"], "--:--")
        self.assertEqual(r.get_json()["error"], "feed")

    def test_config_validation(self):
        r = self.client.post("/api/config?panel_id=CF0001", json={"brightness": "loud"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/config?panel_id=CF0001",
                             json={"brightness": 250, "platform": "S", "bogus": 1, "walk_minutes": 3})
        self.assertEqual(r.status_code, 200)
        cfg = self.client.get("/api/config?panel_id=CF0001").get_json()
        self.assertEqual((cfg["brightness"], cfg["platform"], cfg["walk_minutes"]), (100, "S", 3))
        r = self.client.post("/api/config?panel_id=CF0001", json={"latch_blanking": 20})
        self.assertEqual(r.get_json()["config"]["latch_blanking"], 8)
        self.assertNotIn("bogus", cfg)
        self.assertEqual(self.client.get("/api/config").status_code, 400)
        self.assertEqual(self.client.get("/api/config?panel_id=../../x").status_code, 400)

    def test_toggles_blank_scroll_text(self):
        self.client.post("/api/config?panel_id=TG0001",
                         json={"show_leave_by": False, "show_alerts": False})
        feed = fake_feed("L14N", [at(23, 59)])
        with mock.patch.object(ps, "get_feed", return_value=feed), \
             mock.patch.object(ps, "alert_for_line", return_value=("delays", 1)):
            j = self.client.get("/next-trains?panel_id=TG0001").get_json()
        self.assertEqual((j["leave_by"], j["alert_msg"]), ("", ""))
        self.assertEqual(j["service_status"], 1)   # minimalist dot still knows

    def test_demo_mode_is_stable_within_a_slot(self):
        stations = [{"id": "L14", "name": "Morgan Av"}, {"id": "L15", "name": "Jefferson St"}]
        with mock.patch.object(ps, "get_stations", return_value=stations):
            a = ps.demo_selection("ABC123", 42)
            b = ps.demo_selection("ABC123", 42)
        self.assertEqual(a, b)
        self.assertIn(a[0], ps.LINES)

    def test_index_renders(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'"L": "#A7A9AC"', r.data)
        self.assertNotIn(b"{{", r.data)


class WifiHandoff(unittest.TestCase):
    def setUp(self):
        self.client = ps.app.test_client()
        self.patches = [mock.patch.object(ps, "get_feed", side_effect=RuntimeError("offline")),
                        mock.patch.object(ps, "alert_for_line", return_value=("", 0))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def poll(self, pid, ack=None):
        url = f"/next-trains?panel_id={pid}&fw=610&ssid=Old%20Router&rssi=-60"
        if ack:
            url += f"&wifi_ok={ack}"
        return self.client.get(url).get_json()

    def test_full_handoff_deletes_password(self):
        r = self.client.post("/api/wifi?panel_id=WF0001",
                             json={"ssid": "New Router", "password": "hunter2hunter2"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["pending_ssid"], "New Router")

        first = self.poll("WF0001")["wifi_new"]
        self.assertEqual((first["ssid"], first["pass"]), ("New Router", "hunter2hunter2"))
        self.assertEqual(self.poll("WF0001")["wifi_new"]["id"], first["id"])   # resent until acked

        self.assertNotIn("wifi_new", self.poll("WF0001", ack=first["id"]))
        status = self.client.get("/api/wifi?panel_id=WF0001").get_json()
        self.assertIsNone(status["pending_ssid"])
        self.assertEqual(status["delivered"]["ssid"], "New Router")
        self.assertEqual(status["current_ssid"], "Old Router")
        with open(ps.config_path("WF0001")) as f:
            self.assertNotIn("hunter2", f.read())

    def test_wrong_ack_keeps_pending(self):
        self.client.post("/api/wifi?panel_id=WF0002", json={"ssid": "Net", "password": ""})
        self.assertIn("wifi_new", self.poll("WF0002", ack="deadbeef"))

    def test_password_never_reaches_web_app(self):
        self.client.post("/api/wifi?panel_id=WF0003", json={"ssid": "Net", "password": "secretsecret"})
        self.assertNotIn(b"secretsecret", self.client.get("/api/config?panel_id=WF0003").data)
        self.assertNotIn(b"secretsecret", self.client.get("/api/wifi?panel_id=WF0003").data)
        r = self.client.post("/api/config?panel_id=WF0003", json={"brightness": 50})
        self.assertNotIn(b"secretsecret", r.data)

    def test_cancel_and_validation(self):
        self.client.post("/api/wifi?panel_id=WF0004", json={"ssid": "Net", "password": "12345678"})
        self.client.delete("/api/wifi?panel_id=WF0004")
        self.assertNotIn("wifi_new", self.poll("WF0004"))
        self.assertEqual(self.client.post("/api/wifi?panel_id=WF0004",
                                          json={"ssid": "", "password": "12345678"}).status_code, 400)
        self.assertEqual(self.client.post("/api/wifi?panel_id=WF0004",
                                          json={"ssid": "Net", "password": "short"}).status_code, 400)
        self.assertEqual(self.client.post("/api/config?panel_id=WF0004",
                                          json={"wifi_pending": {"ssid": "x"}}).status_code, 200)
        self.assertNotIn("wifi_new", self.poll("WF0004"))   # can't inject via /api/config


class TestPattern(unittest.TestCase):
    def test_request_sets_countdown(self):
        c = ps.app.test_client()
        with mock.patch.object(ps, "get_feed", side_effect=RuntimeError("x")), \
             mock.patch.object(ps, "alert_for_line", return_value=("", 0)):
            self.assertEqual(c.get("/next-trains?panel_id=TP0001").get_json()["test_pattern"], 0)
            self.assertEqual(c.post("/api/test-pattern?panel_id=TP0001").status_code, 200)
            left = c.get("/next-trains?panel_id=TP0001").get_json()["test_pattern"]
        self.assertTrue(55 <= left <= 60)
        self.assertNotIn("test_until", ps.CONFIG_SCHEMA)   # not settable via /api/config


class Weather(unittest.TestCase):
    SAMPLE = {"current": {"temperature_2m": 51.6, "weather_code": 3, "is_day": 1},
              "hourly": {"precipitation_probability": [10, 20, 65, 30]}}

    def setUp(self):
        ps.cache._data.clear()
        ps._weather_failed.clear()

    def test_station_coordinates_from_gtfs(self):
        lat, lon = ps.station_coords("L14")
        self.assertAlmostEqual(lat, 40.706, places=2)
        self.assertAlmostEqual(lon, -73.933, places=2)
        self.assertEqual(ps.station_coords("NOPE"), ps.NYC_CENTER)

    def test_icons(self):
        self.assertEqual(ps.weather_icon(0, True), "sun")
        self.assertEqual(ps.weather_icon(0, False), "moon")
        self.assertEqual(ps.weather_icon(2, True), "partly")
        self.assertEqual(ps.weather_icon(45, True), "cloud")
        self.assertEqual(ps.weather_icon(63, True), "rain")
        self.assertEqual(ps.weather_icon(81, False), "rain")
        self.assertEqual(ps.weather_icon(73, True), "snow")
        self.assertEqual(ps.weather_icon(95, True), "storm")

    def test_umbrella_rule(self):
        w = ps.parse_weather(self.SAMPLE)
        self.assertEqual((w["temp"], w["icon"], w["rain_chance"], w["umbrella"]), (52, "cloud", 65, True))
        dry = {"current": {"temperature_2m": 70, "weather_code": 0, "is_day": 1},
               "hourly": {"precipitation_probability": [0, 10, None, 49]}}
        self.assertFalse(ps.parse_weather(dry)["umbrella"])
        raining = {"current": {"temperature_2m": 60, "weather_code": 61, "is_day": 1}, "hourly": {}}
        self.assertTrue(ps.parse_weather(raining)["umbrella"])   # raining now

    def _resp(self):
        return mock.Mock(json=lambda: self.SAMPLE, raise_for_status=lambda: None)

    def test_payload_off_by_default_and_on_when_toggled(self):
        c = ps.app.test_client()
        with mock.patch.object(ps, "get_feed", side_effect=RuntimeError("x")), \
             mock.patch.object(ps, "alert_for_line", return_value=("", 0)), \
             mock.patch.object(ps.requests, "get", return_value=self._resp()) as get:
            self.assertEqual(c.get("/next-trains?panel_id=WX0001").get_json()["wx"], 0)
            get.assert_not_called()
            c.post("/api/config?panel_id=WX0001", json={"show_weather": True})
            j = c.get("/next-trains?panel_id=WX0001").get_json()
            c.get("/next-trains?panel_id=WX0001")          # cached: no second fetch
            self.assertEqual(get.call_count, 1)
            self.assertEqual(get.call_args.kwargs["params"]["temperature_unit"], "fahrenheit")
            info = c.get("/api/weather?panel_id=WX0001").get_json()
        self.assertEqual((j["wx"], j["wx_temp"], j["wx_icon"], j["wx_umbrella"]),
                         (1, 52, ps.WX_ICONS["cloud"], 1))
        self.assertEqual((info["ok"], info["rain_chance"]), (True, 65))

    def test_weather_outage_hides_strip_and_backs_off(self):
        c = ps.app.test_client()
        c.post("/api/config?panel_id=WX0002", json={"show_weather": True})
        with mock.patch.object(ps, "get_feed", side_effect=RuntimeError("x")), \
             mock.patch.object(ps, "alert_for_line", return_value=("", 0)), \
             mock.patch.object(ps.requests, "get", side_effect=ps.requests.Timeout()) as get:
            j = c.get("/next-trains?panel_id=WX0002").get_json()
            c.get("/next-trains?panel_id=WX0002")
        self.assertEqual((j["wx"], j["train_time"]), (0, "--:--"))
        self.assertEqual(get.call_count, 1)   # no retry for WEATHER_RETRY_S


class TwoTrains(unittest.TestCase):
    def test_second_row_in_payload(self):
        c = ps.app.test_client()
        feeds = {"L": fake_feed("L14S", [at(10, 31)]), "G": fake_feed("G29N", [at(10, 44)], "G")}
        levels = {"L": ("", 0), "G": ("G trains delayed", 1)}
        with mock.patch.object(ps, "get_feed", side_effect=lambda line: feeds[line]), \
             mock.patch.object(ps, "alert_for_line", side_effect=lambda line, ts: levels.get(line, ("", 0))), \
             mock.patch.object(ps, "datetime") as dt:
            dt.now.return_value = at(10, 10)
            self.assertEqual(c.get("/next-trains?panel_id=TT0001").get_json()["dual"], 0)
            r = c.post("/api/config?panel_id=TT0001", json={
                "platform": "S", "dual": True, "line2": "G", "stop_parent2": "G29",
                "stop_name2": "Metropolitan Av", "platform2": "N", "walk_minutes2": 4})
            self.assertEqual(r.status_code, 200)
            j = c.get("/next-trains?panel_id=TT0001").get_json()
            c.post("/api/config?panel_id=TT0001", json={"demo": True})
            demo = c.get("/next-trains?panel_id=TT0001").get_json()
        self.assertEqual((j["dual"], j["train_time"], j["train_time2"], j["line2"]),
                         (1, "10:31", "10:44", "G"))
        self.assertEqual((j["logo2_r"], j["logo2_g"], j["logo2_b"]), (108, 190, 69))
        self.assertEqual((j["service_status"], j["service_status2"]), (0, 1))
        self.assertEqual(demo["dual"], 0)   # demo shows one train

    def test_second_train_validation(self):
        c = ps.app.test_client()
        r = c.post("/api/config?panel_id=TT0002", json={"line2": "X", "platform2": "E"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(r.get_json()["errors"]), 2)


class Animations(unittest.TestCase):
    def _feed(self, t, trip_id="T1"):
        trips = [SimpleNamespace(trip_id=trip_id, stop_time_updates=[
            SimpleNamespace(stop_id="L14N", arrival=t.replace(tzinfo=None), departure=None)])]
        return SimpleNamespace(filter_trips=lambda line_id: trips)

    def test_arriving_window_and_stable_id(self):
        now = at(10, 10)
        a = ps.arriving_train(self._feed(at(10, 10).replace(second=40)), "L14N", "L", now)
        b = ps.arriving_train(self._feed(at(10, 11)), "L14N", "L", now)         # prediction moved
        self.assertTrue(a)
        self.assertEqual(a, b)                                                  # same trip, same id
        self.assertEqual(ps.arriving_train(self._feed(at(10, 12)), "L14N", "L", now), 0)   # 2 min out
        self.assertEqual(ps.arriving_train(self._feed(at(10, 9)), "L14N", "L", now), 0)    # long gone
        self.assertNotEqual(a, ps.arriving_train(self._feed(at(10, 11), "T2"), "L14N", "L", now))

    def test_payload_respects_toggle_and_preview(self):
        c = ps.app.test_client()
        feed = self._feed(at(10, 10).replace(second=30))
        with mock.patch.object(ps, "get_feed", return_value=feed), \
             mock.patch.object(ps, "alert_for_line", return_value=("", 0)), \
             mock.patch.object(ps, "datetime") as dt:
            dt.now.return_value = at(10, 10)
            off = c.get("/next-trains?panel_id=AN0001").get_json()["arriving"]
            self.assertEqual(c.post("/api/preview-arrival?panel_id=AN0001").status_code, 200)
            preview = c.get("/next-trains?panel_id=AN0001").get_json()["arriving"]
            c.post("/api/config?panel_id=AN0001", json={"animations": True, "preview_arrival": None})
            ps.save_config("AN0001", {**ps.load_config("AN0001"), "preview_arrival": None})
            on = c.get("/next-trains?panel_id=AN0001").get_json()["arriving"]
        self.assertEqual(off, 0)
        self.assertTrue(preview)
        self.assertTrue(on)
        self.assertNotIn("preview_arrival", ps.CONFIG_SCHEMA)


class NightSchedule(unittest.TestCase):
    def cfg(self, start="01:00", end="06:00", **kw):
        return {**ps.DEFAULT_CONFIG, "night_mode": True, "night_start": start, "night_end": end, **kw}

    def test_window(self):
        c = self.cfg()
        self.assertFalse(ps.night_off(c, at(0, 59)))
        self.assertTrue(ps.night_off(c, at(1, 0)))
        self.assertTrue(ps.night_off(c, at(5, 59)))
        self.assertFalse(ps.night_off(c, at(6, 0)))
        self.assertFalse(ps.night_off({**c, "night_mode": False}, at(3, 0)))
        self.assertEqual(ps.night_window(c, at(3, 0)), at(6, 0))

    def test_window_across_midnight(self):
        c = self.cfg("22:30", "06:00")
        self.assertTrue(ps.night_off(c, at(23, 0)))
        self.assertTrue(ps.night_off(c, at(2, 0)))
        self.assertFalse(ps.night_off(c, at(12, 0)))
        self.assertEqual(ps.night_window(c, at(23, 0)).day, at(23, 0).day + 1)   # ends tomorrow 6:00

    def test_payload_skip_and_validation(self):
        c = ps.app.test_client()
        self.assertEqual(c.post("/api/config?panel_id=NT0001", json={"night_start": "25:00"}).status_code, 400)
        c.post("/api/config?panel_id=NT0001", json={"night_mode": True})
        with mock.patch.object(ps, "get_feed", side_effect=RuntimeError("x")), \
             mock.patch.object(ps, "alert_for_line", return_value=("", 0)), \
             mock.patch.object(ps, "datetime") as dt:
            dt.now.return_value = at(2, 30)
            dark = c.get("/next-trains?panel_id=NT0001").get_json()["panel_off"]
            status = c.get("/api/status?panel_id=NT0001").get_json()
            skip = c.post("/api/night-skip?panel_id=NT0001", json={"skip": True}).get_json()
            lit = c.get("/next-trains?panel_id=NT0001").get_json()["panel_off"]
            c.post("/api/night-skip?panel_id=NT0001", json={"skip": False})
            dark_again = c.get("/next-trains?panel_id=NT0001").get_json()["panel_off"]
            dt.now.return_value = at(9, 0)
            day = c.get("/next-trains?panel_id=NT0001").get_json()["panel_off"]
        self.assertEqual((dark, lit, dark_again, day), (1, 0, 1, 0))
        self.assertEqual((status["night_off"], status["night_until"]), (True, "6:00 AM"))
        self.assertEqual(skip["until"], "6:00 AM")
        self.assertNotIn("night_skip_until", ps.CONFIG_SCHEMA)


class Occasions(unittest.TestCase):
    def test_season_dates_match_published_times(self):
        # 2025: Mar 20 05:01 EDT, Jun 20 22:42 EDT, Sep 22 14:19 EDT, Dec 21 10:03 EST
        got = [ps.season_start(2025, i).strftime("%m-%d %H:%M") for i in range(4)]
        self.assertEqual(got, ["03-20 05:01", "06-20 22:42", "09-22 14:19", "12-21 10:03"])
        # Sep 2026 equinox is 00:05 UTC on the 23rd, i.e. the 22nd in New York
        self.assertEqual(ps.season_start(2026, 2).date(), ps.date(2026, 9, 22))

    def test_floating_holidays(self):
        self.assertEqual(ps.easter(2026), ps.date(2026, 4, 5))
        self.assertEqual(ps.easter(2027), ps.date(2027, 3, 28))
        occ = ps.occasions(2026)
        self.assertEqual(occ[ps.date(2026, 11, 26)], ("leaves", "HAPPY THANKSGIVING"))
        self.assertEqual(occ[ps.date(2026, 5, 25)], ("flag", "MEMORIAL DAY"))
        self.assertEqual(occ[ps.date(2026, 9, 7)][1], "LABOR DAY")
        self.assertEqual(occ[ps.date(2026, 1, 19)][1], "MLK DAY")
        self.assertEqual(occ[ps.date(2026, 6, 21)][1], "SUMMER SOLSTICE")
        self.assertEqual(occ[ps.date(2026, 1, 1)][1], "HAPPY 2026")
        self.assertNotIn(ps.date(2026, 10, 20), occ)
        for kind, label in occ.values():
            self.assertIn(kind, ps.OCC_KINDS)
            self.assertLessEqual(len(label), 23)   # firmware buffer

    def test_payload_and_preview(self):
        c = ps.app.test_client()
        with mock.patch.object(ps, "get_feed", side_effect=RuntimeError("x")), \
             mock.patch.object(ps, "alert_for_line", return_value=("", 0)), \
             mock.patch.object(ps, "datetime") as dt:
            dt.now.return_value = datetime(2026, 10, 31, 12, 0, tzinfo=ET)
            off = c.get("/next-trains?panel_id=OC0001").get_json()
            c.post("/api/config?panel_id=OC0001", json={"animations": True})
            on = c.get("/next-trains?panel_id=OC0001").get_json()
            dt.now.return_value = datetime(2026, 10, 20, 12, 0, tzinfo=ET)
            plain = c.get("/next-trains?panel_id=OC0001").get_json()
            first = c.post("/api/preview-occasion?panel_id=OC0001").get_json()
            prev = c.get("/next-trains?panel_id=OC0001").get_json()
            second = c.post("/api/preview-occasion?panel_id=OC0001").get_json()
            c.post("/api/turtle?panel_id=OC0001")
            turtle = c.get("/next-trains?panel_id=OC0001").get_json()
        self.assertEqual(off["occasion"], 0)
        self.assertEqual((on["occasion"], on["occasion_label"]), (ps.OCC_KINDS["pumpkin"], "HAPPY HALLOWEEN"))
        self.assertEqual(on["occasion_seq"], 2026103100 + ps.OCC_KINDS["pumpkin"])
        self.assertEqual(plain["occasion"], 0)
        self.assertEqual(first["label"], "HAPPY 2026")
        self.assertEqual(prev["occasion"], ps.OCC_KINDS["fireworks"])
        self.assertNotEqual(first["kind"], second["kind"])
        self.assertEqual(turtle["occasion"], ps.OCC_KINDS["turtle"])
        self.assertEqual(len(ps.preview_list(2026)), 13)   # every animation except the turtle


class BothDirections(unittest.TestCase):
    def test_rows_are_north_then_south(self):
        c = ps.app.test_client()
        trips = [SimpleNamespace(trip_id="n1", stop_time_updates=[SimpleNamespace(
                     stop_id="L14N", departure=at(10, 31).replace(tzinfo=None), arrival=None)]),
                 SimpleNamespace(trip_id="s1", stop_time_updates=[SimpleNamespace(
                     stop_id="L14S", departure=at(10, 37).replace(tzinfo=None), arrival=None)])]
        feed = SimpleNamespace(filter_trips=lambda line_id: trips)
        with mock.patch.object(ps, "get_feed", return_value=feed), \
             mock.patch.object(ps, "alert_for_line", return_value=("L delays", 1)), \
             mock.patch.object(ps, "datetime") as dt:
            dt.now.return_value = at(10, 10)
            c.post("/api/config?panel_id=BD0001", json={"both_dirs": True, "platform": "S", "dual": True})
            j = c.get("/next-trains?panel_id=BD0001").get_json()
            c.post("/api/config?panel_id=BD0001", json={"both_dirs": False})
            two = c.get("/next-trains?panel_id=BD0001").get_json()
        self.assertEqual((j["dual"], j["dirs"]), (1, 1))
        self.assertEqual((j["train_time"], j["train_time2"]), ("10:31", "10:37"))   # N first, even if platform=S
        self.assertEqual((j["line"], j["line2"], j["service_status2"]), ("L", "L", 1))
        self.assertEqual(two["dirs"], 0)                                             # back to two-train mode


class PlatformAlerts(unittest.TestCase):
    ALERTS = [
        {"routes": {"L"}, "stops": set(), "periods": [], "level": 1, "text": "x",
         "full": "Manhattan-bound L trains are running with delays."},
        {"routes": {"L"}, "stops": set(), "periods": [], "level": 1, "text": "x",
         "full": "Brooklyn-bound L trains are skipping Graham Av."},
        {"routes": {"L"}, "stops": {"L14S"}, "periods": [], "level": 2, "text": "x",
         "full": "No L trains at Morgan Av toward Canarsie."},
        {"routes": {"L", "G"}, "stops": set(), "periods": [], "level": 1, "text": "x",
         "full": "L trains run every 12 minutes."},
        {"routes": {"L"}, "stops": set(), "periods": [(0, 5)], "level": 2, "text": "x",
         "full": "Old alert."},
        {"routes": {"G"}, "stops": set(), "periods": [], "level": 1, "text": "x",
         "full": "Northbound G trains are delayed."},
    ]

    def test_direction_filtering_and_order(self):
        with mock.patch.object(ps, "get_alerts", return_value=self.ALERTS):
            north = [a["text"] for a in ps.alerts_for_platform("L", "L14", "N", 1000)]
            south = ps.alerts_for_platform("L", "L14", "S", 1000)
        self.assertEqual(north, ["Manhattan-bound L trains are running with delays.",
                                 "L trains run every 12 minutes."])
        self.assertEqual([a["text"] for a in south],
                         ["No L trains at Morgan Av toward Canarsie.",          # severe + this station first
                          "Brooklyn-bound L trains are skipping Graham Av.",
                          "L trains run every 12 minutes."])
        self.assertEqual(south[0]["level"], 2)

    def test_endpoint(self):
        c = ps.app.test_client()
        with mock.patch.object(ps, "get_alerts", return_value=self.ALERTS):
            j = c.get("/api/alerts?line=G&stop=G29&platform=S").get_json()
        self.assertEqual([a["text"] for a in j["alerts"]], ["L trains run every 12 minutes."])   # tagged L+G; northbound G alert left out
        self.assertEqual(c.get("/api/alerts?line=X&platform=N").status_code, 400)
        with mock.patch.object(ps, "get_alerts", side_effect=RuntimeError("MTA down")):
            self.assertEqual(c.get("/api/alerts?line=L&stop=L14&platform=N").get_json()["ok"], False)


class StationOrder(unittest.TestCase):
    def test_merges_locals_branches_and_partial_trips(self):
        order = ps.order_stops([
            ["A1", "A3", "A5", "A6", "L1", "L2"],         # express trip to branch L
            ["A1", "A2", "A3", "A4", "A5"],               # local trip (short)
            ["A5", "A6", "R1", "R2", "R3"],               # trip on branch R, already under way
        ])
        self.assertEqual(order, ["A1", "A2", "A3", "A4", "A5", "A6", "L1", "L2", "R1", "R2", "R3"])

    def test_conflicting_trips_still_return_every_stop(self):
        order = ps.order_stops([["A", "B", "C"], ["C", "B"]])
        self.assertEqual(sorted(order), ["A", "B", "C"])

    def test_get_stations_in_route_order(self):
        def trip(stops):
            return SimpleNamespace(stop_time_updates=[SimpleNamespace(stop_id=s, stop_name=n)
                                                      for s, n in stops])
        trips = [
            trip([("L06S", "1 Av"), ("L08S", "Bedford Av"), ("L10S", "Lorimer St"), ("L14S", "Morgan Av")]),
            trip([("L29N", "Canarsie"), ("L14N", "Morgan Av"), ("L10N", "Lorimer St"), ("L01N", "8 Av")]),
        ]
        ps.cache._data.clear()
        with mock.patch.object(ps, "get_feed", return_value=SimpleNamespace(filter_trips=lambda line_id: trips)):
            names = [s["name"] for s in ps.get_stations("L")]
        self.assertEqual(names, ["8 Av", "1 Av", "Bedford Av", "Lorimer St", "Morgan Av", "Canarsie"])


class AlertFeedParsing(unittest.TestCase):
    def test_headline_for_panel_and_description_for_web_app(self):
        from nyct_gtfs.compiled_gtfs import gtfs_realtime_pb2 as rt
        feed = rt.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        e = feed.entity.add(id="a1")
        e.alert.informed_entity.add(route_id="L")
        e.alert.header_text.translation.add(
            text="Northbound [L] trains are running with delays after we addressed a mechanical "
                 "problem on a train at Lorimer St.", language="en")
        e.alert.header_text.translation.add(text="<p>html</p>", language="en-html")
        e.alert.description_text.translation.add(
            text="What's happening?\nWe're fixing a train.\n\nTravel alternatives: [accessibility icon] "
                 "Take the [G] to Court Sq.", language="en")
        resp = mock.Mock(content=feed.SerializeToString(), raise_for_status=lambda: None)
        ps.cache._data.clear()
        with mock.patch.object(ps.requests, "get", return_value=resp):
            alerts = ps.get_alerts()
            web = ps.alerts_for_platform("L", "L10", "N", 1000)
        self.assertEqual(alerts[0]["text"], "N/B L trains are delayed after a mech problem on a train at Lorimer St.")
        self.assertEqual(web[0]["text"], "Northbound L trains are running with delays after we addressed "
                                         "a mechanical problem on a train at Lorimer St.")
        self.assertEqual(web[0]["detail"], "What's happening? We're fixing a train. Travel alternatives: "
                                           "Take the G to Court Sq.")

    def test_long_description_is_trimmed(self):
        out = ps.shorten_words("word " * 300, ps.ALERT_DETAIL_LIMIT)
        self.assertLessEqual(len(out), ps.ALERT_DETAIL_LIMIT)
        self.assertTrue(out.endswith("…"))


def espn_event(eid, state, detail, away, home):
    def comp(side, t):
        abbr, loc, score, color, alt = t
        return {"homeAway": side, "score": str(score),
                "team": {"abbreviation": abbr, "location": loc, "color": color, "alternateColor": alt}}
    return {"id": eid, "status": {"type": {"state": state, "shortDetail": detail}},
            "competitions": [{"competitors": [comp("home", home), comp("away", away)]}]}


class LiveScores(unittest.TestCase):
    NBA = {"events": [
        espn_event("1", "in", "4:21 - 3rd", ("BOS", "Boston", 70, "008348", "ffffff"), ("DET", "Detroit", 71, "1d428a", "c8102e")),
        espn_event("2", "in", "Halftime", ("MIA", "Miami", 50, "98002e", "f9a01b"), ("NY", "New York", 61, "1d428a", "f58426")),
        espn_event("3", "pre", "7:30 PM", ("BKN", "Brooklyn", 0, "000000", "ffffff"), ("LAL", "Los Angeles", 0, "552583", "fdb927")),
        espn_event("4", "in", "Q4", ("CHI", "Chicago", 90, "ce1141", "000000"), ("PHX", "Phoenix", 120, "1d1160", "e56020")),
    ]}
    MLB = {"events": [
        espn_event("9", "in", "Top 5th", ("NYY", "New York", 3, "003087", "e4002c"), ("TOR", "Toronto", 2, "134a8e", "1d2d5c")),
        espn_event("8", "post", "Final", ("TB", "Tampa Bay", 2, "092c5c", "8fbce6"), ("CLE", "Cleveland", 3, "002b5c", "e31937")),
    ]}

    def setUp(self):
        ps.cache._data.clear()
        ps._scores_failed.clear()

    def _get(self, url, timeout):
        return mock.Mock(json=lambda: self.MLB if "baseball" in url else self.NBA, raise_for_status=lambda: None)

    def test_short_status(self):
        self.assertEqual(ps.short_status("4:21 - 3rd"), "Q3 4:21")
        self.assertEqual(ps.short_status("0:45.2 - 4th"), "Q4 0:45.2")
        self.assertEqual(ps.short_status("2:00 - OT"), "OT 2:00")
        self.assertEqual(ps.short_status("End of 1st"), "END Q1")
        self.assertEqual(ps.short_status("Top 5th"), "TOP 5TH")
        self.assertEqual(ps.short_status("Halftime"), "HALFTIME")

    def test_led_color_brightens_dark_navy(self):
        self.assertEqual(ps.led_color("003087", "e4002c"), (0, 74, 210))     # Yankees navy -> bright blue
        self.assertEqual(ps.led_color("000000", "ffffff"), (255, 255, 255))  # Nets black -> white alternate
        self.assertEqual(max(ps.led_color("1d1160", "")), 210)              # no usable alternate: brightened
        self.assertEqual(ps.led_color("ffffff", ""), (255, 255, 255))

    def test_live_only_ny_first_then_closest(self):
        with mock.patch.object(ps.requests, "get", side_effect=self._get):
            games = ps.live_games(["mlb", "nba"])
        self.assertEqual([g["id"] for g in games], ["9", "2", "1", "4"])    # NYY, Knicks, then 1-pt game, then blowout
        self.assertEqual(games[0]["detail"], "TOP 5TH")

    def test_payload_rotates_among_ny_games_and_respects_toggles(self):
        cfg = {**ps.DEFAULT_CONFIG, "show_scores": True}
        with mock.patch.object(ps.requests, "get", side_effect=self._get), \
             mock.patch.object(ps.time, "time", return_value=0):
            a = ps.score_payload(cfg)
        with mock.patch.object(ps.requests, "get", side_effect=self._get), \
             mock.patch.object(ps.time, "time", return_value=ps.SCORES_ROTATE_S):
            b = ps.score_payload(cfg)
            nba_only = ps.score_payload({**cfg, "scores_mlb": False})
            off = ps.score_payload({**cfg, "show_scores": False})
        self.assertEqual((a["away_abbr"], a["away_score"], a["home_abbr"], a["score_status"]), ("NYY", 3, "TOR", "TOP 5TH"))
        self.assertEqual((b["away_abbr"], b["home_abbr"], b["home_score"]), ("MIA", "NY", 61))
        self.assertEqual(nba_only["home_abbr"], "NY")
        self.assertEqual(off, {"score_on": 0})

    def test_no_ny_game_shows_closest_and_outage_is_quiet(self):
        cfg = {**ps.DEFAULT_CONFIG, "show_scores": True, "scores_mlb": False}
        self.NBA = {"events": [e for e in LiveScores.NBA["events"] if e["id"] != "2"]}
        with mock.patch.object(ps.requests, "get", side_effect=self._get), \
             mock.patch.object(ps.time, "time", return_value=0):
            self.assertEqual(ps.score_payload(cfg)["away_abbr"], "BOS")
        ps.cache._data.clear()
        with mock.patch.object(ps.requests, "get", side_effect=ps.requests.Timeout()):
            self.assertEqual(ps.score_payload(cfg), {"score_on": 0})


class Firmware(unittest.TestCase):
    def setUp(self):
        self.client = ps.app.test_client()

    def tearDown(self):
        if os.path.exists(ps.FIRMWARE_BIN):
            os.remove(ps.FIRMWARE_BIN)

    def test_no_firmware(self):
        self.assertEqual(self.client.get("/firmware/info").get_json()["build"], 0)
        self.assertEqual(self.client.get("/firmware/download").status_code, 404)

    def test_build_tag_and_download(self):
        blob = b"\xe9" + b"\x00" * 100 + b"##MTAPANEL_BUILD=601##" + b"\xff" * 1000
        with open(ps.FIRMWARE_BIN, "wb") as f:
            f.write(blob)
        info = self.client.get("/firmware/info").get_json()
        self.assertEqual(info["build"], 601)
        r = self.client.get("/firmware/download?build=601&panel_id=ABC123")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-MD5"], hashlib.md5(blob).hexdigest())
        self.assertEqual(int(r.headers["Content-Length"]), len(blob))
        self.assertIn("no-store", r.headers["Cache-Control"])
        r.close()

    def test_rejects_non_esp_image(self):
        with open(ps.FIRMWARE_BIN, "wb") as f:
            f.write(b"PK\x03\x04 ##MTAPANEL_BUILD=700## zip file")
        self.assertEqual(self.client.get("/firmware/info").get_json()["build"], 0)


if __name__ == "__main__":
    unittest.main()
