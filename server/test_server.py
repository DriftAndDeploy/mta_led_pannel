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
                         "Uptown AC trains are delayed")

    def test_truncates_on_word_boundary(self):
        out = ps.clean_alert_text("word " * 40)
        self.assertLessEqual(len(out), 80)
        self.assertTrue(out.endswith("..."))

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
