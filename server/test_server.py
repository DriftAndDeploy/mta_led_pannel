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
