import contextlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adaptive_lab
import delivery
import witness


class AdaptiveLabTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_workload_pairing_and_order_are_reproducible(self):
        plan = adaptive_lab.schedule(3)
        self.assertEqual(len(plan), 12)
        self.assertEqual(sum(row["order"][0] == "adaptive" for row in plan), 6)
        for row in plan:
            messages = adaptive_lab.payloads(row["profile"], row["repetition"])
            self.assertEqual(messages, adaptive_lab.payloads(row["profile"], row["repetition"]))
            self.assertEqual(len(messages), 20)
            self.assertEqual(len({item["message_id"] for item in messages}), 20)
            for item in messages:
                delivery.validate_message(item)
        for repetitions in (0, 4, True):
            with self.assertRaises(delivery.Refused):
                adaptive_lab.schedule(repetitions)

    def test_faults_have_fixed_transitions_and_message_keyed_ack_loss(self):
        f = adaptive_lab.fault_decision
        self.assertEqual(f("blackout_return", 299.9, 1, 1)["action"], "ack")
        self.assertEqual(f("blackout_return", 300, 1, 1)["action"], "hold_without_commit")
        self.assertEqual(f("blackout_return", 1000, 1, 1)["action"], "ack")
        self.assertGreaterEqual(f("latency_jump_recovery", 300, 1, 1)["response_delay_ms"], 180)
        self.assertLess(f("latency_jump_recovery", 1000, 1, 1)["response_delay_ms"], 12)
        self.assertEqual(f("ack_loss_after_commit", 400, 7, 1)["action"], "drop_ack_after_commit")
        self.assertEqual(f("ack_loss_after_commit", 1500, 7, 1)["action"], "drop_ack_after_commit")
        self.assertEqual(f("ack_loss_after_commit", 400, 7, 2)["action"], "ack")

    def test_real_http_ack_loss_has_committed_effect_and_verified_duplicate(self):
        ledger = self.root / "witness.sqlite"
        witness.initialize(ledger)
        fixture = adaptive_lab.FaultFixture(ledger, "ack_loss_after_commit").start()
        try:
            message = adaptive_lab.payloads("ack_loss_after_commit", 1)[6]
            first = delivery.send_once(fixture.port, "durable", message, 500)
            self.assertFalse(first["ack_verified"])
            with contextlib.closing(delivery.connect_db(ledger)) as db:
                row = db.execute("SELECT * FROM deliveries").fetchone()
                self.assertEqual(row["digest"], delivery.digest(message))
                self.assertEqual(row["requests"], 1)
            second = delivery.send_once(fixture.port, "durable", message, 500)
            self.assertTrue(second["ack_verified"])
            self.assertTrue(second["duplicate_ack"])
            with contextlib.closing(delivery.connect_db(ledger)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT requests FROM deliveries").fetchone()[0], 2)
        finally:
            fixture.stop()
        self.assertEqual(len(fixture.events), 2)
        self.assertTrue(fixture.events[0]["committed"])
        self.assertFalse(fixture.events[0]["ack_write_completed"])

    def test_real_http_blackout_times_out_without_committing(self):
        ledger = self.root / "witness.sqlite"
        witness.initialize(ledger)
        fixture = adaptive_lab.FaultFixture(ledger, "blackout_return", timeout_ms=50).start()
        fixture.origin_ns = time.monotonic_ns() - 400_000_000
        try:
            message = adaptive_lab.payloads("blackout_return", 1)[0]
            result = delivery.send_once(fixture.port, "durable", message, 50)
            self.assertEqual(result["error"], "timeout")
            with contextlib.closing(delivery.connect_db(ledger)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
        finally:
            fixture.stop()
        self.assertFalse(fixture.events[0]["committed"])

    def test_real_restart_trials_preserve_policy_counts_and_calibration(self):
        for mode in ("static", "adaptive"):
            with self.subTest(mode=mode):
                output = self.root / mode
                report = adaptive_lab.run_trial(output, profile="blackout_return", mode=mode,
                    repetition=1, policy=delivery.DEFAULT_POLICY,
                    messages=adaptive_lab.payloads("blackout_return", 1), campaign_deadline=time.monotonic() + 15)
                self.assertTrue(report["success"], report)
                self.assertTrue(all(report["checks"].values()))
                self.assertEqual(report["metrics"]["delivered_unique"], 20)
                self.assertEqual(report["metrics"]["acked"], 20)
                self.assertEqual(report["metrics"]["duplicate_effects"], 0)
                self.assertEqual(len(report["queue_summary"]["worker_runs"]), 2)
                self.assertGreater(report["blackout_requests_without_commit"], 0)
                before = json.loads((output / "restart-snapshot.json").read_text())
                final = json.loads((output / "queue-final.json").read_text())
                self.assertEqual(before["metadata"], final["metadata"])
                if mode == "adaptive":
                    self.assertTrue(report["checks"]["restart_resumed_persisted_controller"])
                    self.assertGreater(report["metrics"]["policy_changes"], 0)
                for name, expected in report["artifact_sha256"].items():
                    self.assertEqual(adaptive_lab.sha256(output / name), expected)

    def test_failed_trial_and_campaign_keep_evidence_and_manifest(self):
        output = self.root / "failed-trial"
        with patch.object(adaptive_lab.subprocess, "Popen", side_effect=OSError("not exposed private diagnostic")):
            report = adaptive_lab.run_trial(output, profile="normal", mode="adaptive", repetition=1,
                policy=delivery.DEFAULT_POLICY, messages=adaptive_lab.payloads("normal", 1),
                campaign_deadline=time.monotonic() + 3)
        self.assertFalse(report["success"])
        self.assertTrue((output / "report.json").is_file())
        self.assertNotIn("private diagnostic", (output / "report.json").read_text())
        campaign = self.root / "bounded-campaign"
        result = adaptive_lab.run(campaign, repetitions=1, budget_seconds=1)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "campaign_time_cap_reached")
        manifest = json.loads((campaign / "manifest.json").read_text())["sha256"]
        self.assertIn("report.json", manifest)
        for name, expected in manifest.items():
            self.assertEqual(adaptive_lab.sha256(campaign / name), expected)
        with self.assertRaises(FileExistsError):
            adaptive_lab.run(campaign, repetitions=1)

    def test_real_exit_two_with_refusal_output_is_not_valid_execution(self):
        # Even a previously completed durable queue cannot make a later refusal
        # count as a valid worker result merely because both use exit code 2.
        actual_popen = adaptive_lab.subprocess.Popen
        script = ("import json,sys; sys.path.insert(0,sys.argv[1]); import delivery; "
                  "delivery.work(sys.argv[2]); print(json.dumps({'ok':False,'error':'refused'})); sys.exit(2)")

        def rejecting_worker(command, **kwargs):
            return actual_popen([sys.executable, "-c", script, str(ROOT), command[-1]], **kwargs)

        output = self.root / "refused-worker"
        with patch.object(adaptive_lab.subprocess, "Popen", side_effect=rejecting_worker):
            report = adaptive_lab.run_trial(output, profile="normal", mode="static", repetition=1,
                policy=delivery.DEFAULT_POLICY, messages=adaptive_lab.payloads("normal", 1),
                campaign_deadline=time.monotonic() + 5)
        self.assertEqual(report["worker_exit_code"], 2)
        self.assertEqual(report["worker_outcome"], "all_acknowledged")
        self.assertEqual(report["metrics"]["acked"], 20)
        self.assertFalse(report["worker_completed"])
        self.assertFalse(report["checks"]["worker_final_result_valid"])
        self.assertFalse(report["checks"]["worker_result_matches_durable_state"])
        self.assertFalse(report["success"])
        self.assertTrue((output / "worker-1.log").is_file())
        self.assertTrue((output / "recipient.json").is_file())
        self.assertEqual(json.loads((output / "report.json").read_text())["success"], False)

    def test_worker_result_requires_outcome_exit_coherence(self):
        log = self.root / "worker.log"
        for outcome, returncode in (("all_acknowledged", 2), ("budget_exhausted", 0),
                                    ("attempts_exhausted", 0), ("worker_error", 2)):
            log.write_text(json.dumps({"schema": "resilience-worker-result/v1", "outcome": outcome}) + "\n")
            self.assertIsNone(adaptive_lab.worker_result_from_log(log, returncode))

    def test_aggregate_keeps_losing_and_failed_runs(self):
        reports = [{"profile": "normal", "mode": "static", "repetition": 1, "success": True,
                    "metrics": {"ack_p95_ms": 10, "attempts": 20}},
                   {"profile": "normal", "mode": "adaptive", "repetition": 1, "success": False,
                    "metrics": {"ack_p95_ms": 30, "attempts": 25}}]
        aggregate = adaptive_lab.aggregate_reports(reports)
        self.assertEqual(aggregate["paired_deltas"][0]["adaptive_minus_static"]["ack_p95_ms"], 20)
        self.assertFalse(aggregate["paired_deltas"][0]["both_runs_valid"])
        self.assertEqual(aggregate["groups"][1]["metrics"]["ack_p95_ms"]["observed"], [30])
        self.assertIn("30.0 ms", adaptive_lab.chart_svg(aggregate))
        self.assertEqual(adaptive_lab.percentile([1, 2, 3, 4, 5], .95), 5)


if __name__ == "__main__":
    unittest.main()
