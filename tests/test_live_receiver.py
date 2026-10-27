"""Real loopback protocol tests for durable effects, controls and stale telemetry."""
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import live_receiver as receiver

TOKEN = "a" * 48


def message(payload="Ordre synthétique 7", message_id="event-7"):
    return {"run_id": "demo-1", "message_id": message_id, "payload": payload,
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()}


def status(**updates):
    value = {"run_id": "demo-1", "phase": "healthy", "elapsed_ms": 500, "generated": 1,
             "acked": 0, "pending": 1, "attempts": 1, "timeouts": 0, "active_path": "a",
             "paths": {name: {"srtt_ms": None, "timeout_ms": 500, "failures": 0,
                               "rttvar_ms": None, "successes": 0, "consecutive_failures": 0,
                               "last_attempt_ms": None, "last_success_ms": None} for name in ("a", "b")},
             "events": [{"at_ms": 100, "type": "route_selected", "path": "a", "reason": "initial_route"}],
             "mode": "scripted", "scope": "two_virtual_paths_shared_ssh_tunnel_no_radio"}
    value.update(updates)
    return value


class LiveReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = receiver.Store(self.root / "state", TOKEN)
        self.server = receiver.Server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.tmp.cleanup()

    def request(self, method, path, value=None, *, auth=True, browser=False, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        request_headers = {"Content-Type": "application/json"}
        if auth:
            request_headers["Authorization"] = "Bearer " + TOKEN
        if browser:
            request_headers.update(Origin=self.server.origin, **{"X-Demo-Token": self.store.nonce})
        request_headers.update(headers or {})
        body = raw if raw is not None else (json.dumps(value).encode() if value is not None else None)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, json.loads(data) if response_headers.get("Content-Type", "").startswith("application/json") else data.decode(), response_headers

    def test_commit_ack_duplicates_conflict_and_restart_persistence(self):
        code, ack, _ = self.request("POST", "/api/message", message())
        self.assertEqual(code, 200)
        self.assertTrue(ack["committed"])
        self.assertFalse(ack["duplicate"])
        # A separate connection sees the committed effect before further requests.
        with sqlite3.connect(self.store.db_path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM receipts").fetchone()[0], 1)
        code, ack, _ = self.request("POST", "/api/message", message())
        self.assertEqual(code, 200)
        self.assertTrue(ack["duplicate"])
        self.assertEqual(self.request("POST", "/api/message", message("different"))[0], 409)
        restored = receiver.Store(self.root / "state", TOKEN)
        self.assertEqual(restored.state()["actual"], {"received_unique": 1, "duplicate_requests": 1,
                                                    "duplicate_effects": 0, "conflicts": 1})

    def test_auth_digest_size_and_strict_json_rejections(self):
        self.assertEqual(self.request("POST", "/api/message", message(), auth=False)[0], 401)
        wrong = {**message(), "sha256": "0" * 64}
        self.assertEqual(self.request("POST", "/api/message", wrong)[0], 400)
        self.assertEqual(self.request("POST", "/api/message", message(), headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/message", raw=b" " * 4097)[0], 413)
        self.assertEqual(self.request("POST", "/api/status", raw=b'{"run_id":"x","run_id":"y"}')[0], 400)
        for invalid in (status(elapsed_ms=float("nan")), status(elapsed_ms=10**400), status(generated=True), status(events=[{}] * 31)):
            self.assertEqual(self.request("POST", "/api/status", invalid)[0], 400)
        self.assertEqual(self.store.state()["actual"]["received_unique"], 0)

    def test_browser_controls_require_origin_nonce_and_json(self):
        self.assertEqual(self.request("POST", "/api/control", {"profile": "cut_a"})[0], 403)
        self.assertEqual(self.request("POST", "/api/control", {"profile": "cut_a"}, browser=True,
                                      headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/control", {"profile": "cut_a"}, browser=True,
                                      headers={"X-Demo-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("POST", "/api/control", {"profile": "cut_a"}, browser=True,
                                      headers={"Content-Type": "text/plain"})[0], 415)
        code, command, _ = self.request("POST", "/api/control", {"profile": "cut_both"}, auth=False, browser=True)
        self.assertEqual(code, 200)
        self.assertEqual(command, {"command_version": 1, "mode": "manual", "profile": "cut_both"})
        self.assertEqual(self.request("POST", "/api/control", {"mode": "scripted"}, browser=True)[0], 400)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)

    def test_status_deduplicates_events_caps_history_and_never_refreshes_on_get(self):
        with patch.object(receiver.time, "monotonic", return_value=100):
            self.assertEqual(self.request("POST", "/api/status", status())[0], 200)
        with patch.object(receiver.time, "monotonic", return_value=102.9):
            self.assertTrue(self.request("GET", "/api/state")[1]["fresh"])
        with patch.object(receiver.time, "monotonic", return_value=103.0):
            state = self.request("GET", "/api/state")[1]
            self.assertFalse(state["fresh"])
        for i in range(365):
            self.store.status(status(elapsed_ms=500 + i))
        state = self.store.state()
        self.assertEqual(len(state["events"]), 1)
        self.assertEqual(len(state["history"]), 360)
        self.assertEqual(state["actual"]["received_unique"], 0)  # sender claims are not receipts
        self.assertEqual(receiver.Store(self.root / "state", TOKEN).state()["fresh"], False)

    def test_completed_run_and_stale_cut_are_not_live(self):
        self.store.status(status(phase="cut_both"))
        state = self.store.state()
        self.assertTrue(state["fresh"])  # Telemetry is independent of delivery availability.
        self.assertEqual(state["status"]["phase"], "cut_both")
        self.store.status(status(phase="finished", elapsed_ms=600, completed=True, success=True))
        state = self.store.state()
        self.assertTrue(state["finished"])
        self.assertFalse(state["fresh"])
        self.assertEqual(self.request("POST", "/api/status", status(elapsed_ms=700))[0], 409)
        self.store.status(status(run_id="demo-2"))
        self.assertEqual(self.store.state()["run_id"], "demo-2")
        self.assertEqual(self.store.state("demo-1")["actual"]["received_unique"], 0)

    def test_untrusted_strings_are_data_not_markup_and_token_is_absent(self):
        evil = '<img src=x onerror="alert(1)">'
        self.request("POST", "/api/message", message(evil))
        self.store.status(status(events=[{"at_ms": 1, "type": "observed", "reason": evil}]))
        code, state, _ = self.request("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(state["receipts"][0]["payload"], evil)
        code, page, headers = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.assertNotIn(evil, page)
        self.assertNotIn(TOKEN, page)
        self.assertNotIn("innerHTML", page)
        self.assertIn("textContent", page)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        (self.store.directory / "report-reference.json").write_text(json.dumps({"title": evil,"summary": evil,"source_label": "Proof"}))
        self.assertIn("&lt;img", self.request("GET", "/report-reference")[1])

    def test_private_file_and_launcher_fixed_argv_active_guard(self):
        token_file = self.root / "token"
        token_file.write_text(TOKEN)
        token_file.chmod(0o644)
        with self.assertRaises(receiver.Rejected):
            receiver.private_file(token_file)
        token_file.chmod(0o600)
        self.assertEqual(receiver.private_file(token_file).decode(), TOKEN)
        symlink = self.root / "symlink"
        symlink.symlink_to(token_file)
        with self.assertRaises(receiver.Rejected):
            receiver.private_file(symlink)
        config = self.root / "launcher.json"
        config.write_text(json.dumps({"argv": [sys.executable, "-c", "import time; print('owned child',flush=True); time.sleep(1)"],
                                      "cwd": str(self.root), "log_dir": str(self.root / "logs")}))
        config.chmod(0o600)
        launcher = receiver.Store(self.root / "launcher-state", TOKEN, config)
        self.addCleanup(launcher.close)
        with self.assertRaises(receiver.Rejected):
            launcher.start({"argv": ["untrusted"]})
        launcher.control({"profile": "cut_both"})
        self.assertEqual(launcher.start({}), {"started": True})
        with self.assertRaises(receiver.Rejected) as caught:
            launcher.start({})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(launcher.state()["command"]["mode"], "scripted")
        self.assertTrue(launcher.state()["launcher"]["active"])
        launcher.process.wait(timeout=3)
        self.assertEqual(launcher.state()["launcher"]["returncode"], 0)
        log = next((self.root / "logs").iterdir())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertIn("owned child", log.read_text())
        self.assertNotIn("argv", json.dumps(launcher.state()))


if __name__ == "__main__":
    unittest.main()
