import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

import live_vm_lab as lab


class LiveLabTests(unittest.TestCase):
    def body(self):
        return {"run_id": "live-test", "message_id": "live-test-001", "payload": "test",
                "sha256": hashlib.sha256(b"test").hexdigest()}

    def test_ack_is_correlated_and_committed(self):
        body = self.body()
        valid = {k: body[k] for k in ("run_id", "message_id", "sha256")}
        valid.update(committed=True, duplicate=False)
        self.assertTrue(lab.verified_receipt(body, valid))
        self.assertTrue(lab.verified_receipt(body, dict(valid, duplicate=True)))
        for field, value in [("run_id", "other"), ("message_id", "other"), ("sha256", "0" * 64),
                             ("committed", 1), ("duplicate", 0)]:
            self.assertFalse(lab.verified_receipt(body, dict(valid, **{field: value})))
        self.assertFalse(lab.verified_receipt(body, []))

    def test_failover_and_measured_recovery_without_profile_input(self):
        selector = lab.Selector()
        self.assertEqual(selector.choose(0)[0], "a")
        selector.observe("a", 10, True, False, 10)
        self.assertEqual(selector.choose(400)[0], "b")  # first observation of alternate path
        selector.observe("b", 40, True, False, 440)
        self.assertEqual(selector.choose(800)[0], "a")
        before = selector.paths["a"]["srtt_ms"]
        selector.observe("a", 500, False, True, 1300)
        self.assertEqual(selector.active, "b")
        self.assertEqual(selector.paths["a"]["srtt_ms"], before)  # timeout is censored
        self.assertEqual(selector.choose(1700)[0], "b")
        path, reason = selector.choose(4000)
        self.assertEqual((path, reason), ("a", "periodic_recovery_probe"))
        selector.observe("a", 10, True, False, 4010)
        self.assertEqual(selector.active, "a")
        self.assertEqual(selector.paths["a"]["consecutive_failures"], 0)
        self.assertEqual(selector.paths["a"]["failures"], 1)

    def test_timeout_bounds_and_hysteresis(self):
        selector = lab.Selector()
        for i in range(20):
            selector.observe("a", 1, True, False, i)
        self.assertEqual(selector.paths["a"]["timeout_ms"], 80)
        for i in range(20):
            selector.observe("a", 500, False, True, 100 + i)
        self.assertEqual(selector.paths["a"]["timeout_ms"], 500)
        self.assertEqual(selector.paths["a"]["srtt_ms"], 1)
        selector.observe("a", 25, True, False, 200)
        selector.observe("b", 20, True, False, 201)
        # Hysteresis avoids switching for an insignificant cost difference.
        selector.active = "a"
        selector.paths["a"]["srtt_ms"] = 25
        self.assertEqual(selector.choose(250)[0], "a")

    def test_profiles_are_closed_and_qdiscs_only_owned_interfaces(self):
        links = lab.Links()
        for profile in lab.PROFILES:
            for key in lab.ADDRESSES:
                host = links.qdisc_args(profile, key, "host")
                sender = links.qdisc_args(profile, key, "sender")
                self.assertEqual(host[host.index("dev") + 1], links.hosts[key])
                self.assertEqual(sender[:4], ["ip", "netns", "exec", links.name])
                self.assertEqual(sender[sender.index("dev") + 1], "link" + key)
                self.assertNotIn("eth0", host + sender)
        self.assertEqual(lab.profile_parameters("degraded_a", "a"), (80, 20, 10))
        self.assertEqual(lab.profile_parameters("cut_a", "b"), (15, 0, 0))
        self.assertEqual(lab.profile_parameters("cut_both", "b"), (0, 0, 100))
        for args in [("unbounded", "a", "host"), ("healthy", "eth0", "host"), ("healthy", "a", "other")]:
            with self.assertRaises(lab.Refused):
                links.qdisc_args(*args)
        with self.assertRaises(lab.Refused):
            lab.in_namespace("production", "ip", "link")

    def test_isolation_rejects_default_route_or_extra_interfaces(self):
        snapshot = {"links": [{"ifname": "lo"}] + [
            {"ifname": "link" + key, "addr_info": [{"family": "inet", "local": addresses[0]}]}
            for key, addresses in lab.ADDRESSES.items()], "routes": [
            {"dst": addresses[2], "dev": "link" + key} for key, addresses in lab.ADDRESSES.items()]}
        self.assertTrue(lab.topology(snapshot))
        self.assertFalse(lab.topology(dict(snapshot, routes=snapshot["routes"] + [{"dst": "default"}])))
        self.assertFalse(lab.topology(dict(snapshot, links=snapshot["links"] + [{"ifname": "eth0"}])))

    def test_blackout_boundary_and_recovery_are_measured_separately(self):
        phases = [{"profile": "cut_a", "started_ms": 990, "applied_ms": 1000},
                  {"profile": "cut_both", "started_ms": 2000, "applied_ms": 2020},
                  {"profile": "recovery", "started_ms": 3000, "applied_ms": 3020}]
        attempts = [{"outcome": "acked", "path": "b", "started_ms": 1100, "ended_ms": 1140},
                    {"outcome": "acked", "path": "b", "started_ms": 1990, "ended_ms": 2050},
                    {"outcome": "acked", "path": "a", "started_ms": 3030, "ended_ms": 3060}]
        result = lab.analyze_phases(attempts, phases, 4000)
        self.assertEqual(result[0]["path_b_acks_attempted_during_phase"], 1)
        self.assertEqual(result[1]["boundary_acks"], 1)
        self.assertEqual(result[1]["stable_ack_count"], 0)
        self.assertEqual(result[2]["first_ack_after_recovery_ms"], 40)
        self.assertIsNone(lab.analyze_phases(attempts[:-1], phases, 4000)[2]["first_ack_after_recovery_ms"])
        unexpected = dict(attempts[1], ended_ms=2500)
        self.assertEqual(lab.analyze_phases([unexpected], phases, 4000)[1]["stable_ack_count"], 1)

    def test_sqlite_queue_and_status_reflect_recorded_data(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / "queue.sqlite"
            start = lab.time.monotonic_ns()
            lab.initialize(queue, "live-test", start)
            body = lab.enqueue(queue, "live-test", 1, start)
            self.assertEqual(body["sha256"], hashlib.sha256(body["payload"].encode()).hexdigest())
            status = lab.current_status(queue, "live-test", "healthy", start)
            self.assertEqual((status["generated"], status["pending"], status["acked"]), (1, 1, 0))
            with lab.connect(queue) as db:
                db.execute("UPDATE messages SET ack_ms=35,attempts=1")
                lab.event_put(db, {"at_ms": 35, "type": "acked"})
            status = lab.current_status(queue, "live-test", "healthy", start)
            self.assertEqual((status["pending"], status["acked"], status["attempts"]), (0, 1, 1))
            self.assertEqual(status["events"][0]["type"], "acked")

    def test_token_file_requires_private_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("a" * 32)
            token.chmod(0o600)
            self.assertEqual(lab.token_from(token), "a" * 32)
            token.chmod(0o644)
            with self.assertRaises(lab.Refused):
                lab.token_from(token)
            token.chmod(0o600)
            link = Path(directory) / "link"; link.symlink_to(token)
            with self.assertRaises(lab.Refused):
                lab.token_from(link)

    def test_cleanup_attempts_every_resource_after_failure(self):
        links = Mock()
        links.close.return_value = True
        server, thread = Mock(), Mock()
        thread.is_alive.return_value = False
        with patch.object(lab, "stop_process", side_effect=RuntimeError("failed")):
            result = lab.cleanup(Mock(), [(server, thread)], links)
        self.assertFalse(result["worker_stopped"])
        self.assertTrue(result["proxy_0_stopped"])
        server.shutdown.assert_called_once()
        links.close.assert_called_once()

    def test_network_cleanup_continues_after_namespace_error(self):
        links = lab.Links(); links.created_namespace = True
        links.created_links = list(links.hosts.values())
        attempted = []
        def call(args, check=True):
            attempted.append(args)
            if "netns" in args:
                raise TimeoutError()
            return types.SimpleNamespace(returncode=0)
        links.call = call
        self.assertFalse(links.close())
        for host in links.hosts.values():
            self.assertIn(["ip", "link", "delete", host], attempted)

    def test_proxy_close_attempted_even_when_shutdown_fails(self):
        links, server, thread = Mock(), Mock(), Mock()
        server.shutdown.side_effect = RuntimeError("shutdown error")
        thread.is_alive.return_value = False
        with patch.object(lab, "stop_process", return_value=True):
            result = lab.cleanup(None, [(server, thread)], links)
        self.assertFalse(result["proxy_0_stopped"])
        server.server_close.assert_called_once()
        thread.join.assert_called_once()
        links.close.assert_called_once()

    def test_worker_records_invalid_receipt_and_retries_instead_of_acknowledging(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / "queue.sqlite"
            start = lab.time.monotonic_ns()
            lab.initialize(queue, "live-test", start)
            lab.enqueue(queue, "live-test", 1, start)
            lab.enqueue(queue, "live-test", 2, start)
            with lab.connect(queue) as db:
                lab.state_put(db, "generated_done", True)
            counter = iter(range(0, 120000, 20))
            calls = []
            def reply(host, port, path, body, token, timeout):
                calls.append(host)
                receipt = {k: body[k] for k in ("run_id", "message_id", "sha256")}
                receipt.update(committed=True, duplicate=False)
                if len(calls) == 1:
                    receipt["message_id"] = "forged"
                return receipt
            with patch.object(lab.os, "stat", return_value=types.SimpleNamespace(st_ino=9)), \
                 patch.object(lab, "snapshot", return_value={}), \
                 patch.object(lab, "topology", return_value=True), \
                 patch.object(lab, "token_from", return_value="t" * 32), \
                 patch.object(lab, "elapsed", side_effect=lambda _: next(counter)), \
                 patch.object(lab, "http_json", side_effect=reply):
                self.assertEqual(lab.worker(queue, "sawm-live-0123456789ab-tx", Path(directory) / "token"), 0)
            with lab.connect(queue) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE ack_ms IS NOT NULL").fetchone()[0], 2)
                outcomes = [r[0] for r in db.execute("SELECT outcome FROM attempts ORDER BY id")]
                self.assertEqual(outcomes, ["invalid_ack", "acked", "acked"])
            self.assertIn(lab.ADDRESSES["b"][1], calls)

    def test_commands_are_closed_and_version_integer_not_boolean(self):
        valid = {"command_version": 1, "profile": "cut_a", "mode": "manual"}
        self.assertTrue(lab.valid_command(valid))
        for invalid in [dict(valid, profile="shell"), dict(valid, mode="exec"), dict(valid, command_version=True),
                        dict(valid, command_version=-1)]:
            self.assertFalse(lab.valid_command(invalid))


if __name__ == "__main__":
    unittest.main()


class ProxyStartupTests(unittest.TestCase):
    def test_numeric_proxy_does_not_use_dns(self):
        from unittest.mock import patch
        import live_vm_lab as live
        with patch("socket.getfqdn", side_effect=AssertionError("DNS must not consume scenario time")):
            server = live.Proxy(("127.0.0.1", 0), live.ProxyHandler)
            server.server_close()


class CleanupRaceTests(unittest.TestCase):
    def test_namespace_can_remove_peer_between_show_and_delete(self):
        for remains in (False, True):
            links = lab.Links()
            host = links.hosts["a"]
            links.created_namespace = True
            links.created_links = [host]
            def call(args, check=True):
                if args == ["ip", "link", "delete", host]:
                    return types.SimpleNamespace(returncode=1, stdout="")
                if args == ["ip", "-j", "link", "show"]:
                    return types.SimpleNamespace(returncode=0, stdout=json.dumps([{"ifname":host}] if remains else []))
                return types.SimpleNamespace(returncode=0, stdout="")
            links.call = call
            self.assertEqual(links.close(), not remains)
