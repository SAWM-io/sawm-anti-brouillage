import contextlib
import copy
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import delivery
import lab
import witness

RUN = "1" * 32


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = self.root / "witness.sqlite"
        witness.initialize(self.ledger)
        self.server = witness.Witness(self.ledger)

    def tearDown(self):
        self.server.stop()
        self.temporary.cleanup()

    def queue(self, *, policy=None):
        self.server.start()
        path = self.root / "queue.sqlite"
        delivery.create_queue(path, self.server.port, 3, policy)
        messages = [delivery.make_message(RUN, i) for i in range(1, 4)]
        for message in messages:
            delivery.enqueue(path, message)
        return path, messages

    def test_policy_closed_typed_and_bounded(self):
        self.assertEqual(delivery.validate_policy(delivery.DEFAULT_POLICY), delivery.DEFAULT_POLICY)
        mutations = [{"command": "do not execute"}, {"max_attempts": True}, {"max_attempts": 21},
                     {"max_attempts": 0}, {"request_timeout_ms": 2001}, {"total_budget_ms": 25001},
                     {"base_delay_ms": 1000, "max_delay_ms": 999}, {"schema": "different"}]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(delivery.Refused):
                delivery.validate_policy({**delivery.DEFAULT_POLICY, **mutation})
        for text in ('{"schema":"a","schema":"b"}', '{"number":NaN}', '{"number":Infinity}'):
            with self.assertRaises(delivery.Refused):
                delivery.strict_json(text)

    def test_ack_identity_digest_and_confirmation_are_required(self):
        message = delivery.make_message(RUN, 1)
        content_digest = delivery.digest(message)
        ack = {"schema": delivery.ACK_SCHEMA, "message_id": message["message_id"], "digest": content_digest,
               "committed": True, "duplicate": False}
        delivery.validate_ack(delivery.canonical(ack), message, content_digest)
        for mutation in ({"digest": "0" * 64}, {"message_id": "wrong"}, {"committed": 1},
                         {"duplicate": 0}, {"secret": "ignored?"}):
            with self.subTest(mutation=mutation), self.assertRaises(delivery.Refused):
                delivery.validate_ack(delivery.canonical({**ack, **mutation}), message, content_digest)

    def test_duplicate_idempotent_and_divergent_content_rejected(self):
        message = delivery.make_message(RUN, 1)
        first, _, status = witness.commit_message(self.ledger, "durable", message, delivery.digest(message), False)
        second, _, _ = witness.commit_message(self.ledger, "durable", message, delivery.digest(message), False)
        self.assertEqual(status, 200)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        changed = {**message, "created_at_ms": message["created_at_ms"] + 1}
        self.assertEqual(witness.commit_message(self.ledger, "durable", changed, delivery.digest(changed), False)[2], 409)
        summary = witness.ledger_summary(self.ledger, 1)
        self.assertEqual(summary["channels"]["durable"]["delivered_unique"], 1)
        self.assertEqual(summary["channels"]["durable"]["duplicate_requests"], 1)
        self.assertEqual(summary["channels"]["durable"]["duplicate_effects"], 0)
        self.assertEqual(summary["content_conflicts"], 1)

    def test_real_lost_ack_after_commit_then_verified_duplicate_ack(self):
        self.server.start()
        message = delivery.make_message(RUN, 1)
        first = delivery.send_once(self.server.port, "durable", message, 500)
        self.assertFalse(first["ack_verified"])
        self.assertEqual(first["error"], "connection_closed_without_ack")
        self.assertEqual(witness.ledger_summary(self.ledger, 1)["channels"]["durable"]["delivered_unique"], 1)
        second = delivery.send_once(self.server.port, "durable", message, 500)
        self.assertTrue(second["ack_verified"])
        self.assertTrue(second["duplicate_ack"])
        summary = witness.ledger_summary(self.ledger, 1)["channels"]["durable"]
        self.assertEqual((summary["delivered_unique"], summary["duplicate_requests"], summary["duplicate_effects"]), (1, 1, 0))

    def test_queue_deduplicates_and_refuses_changed_message(self):
        queue, messages = self.queue()
        self.assertFalse(delivery.enqueue(queue, messages[0]))
        changed = {**messages[0], "created_at_ms": messages[0]["created_at_ms"] + 1}
        with self.assertRaises(delivery.Refused):
            delivery.enqueue(queue, changed)
        with self.assertRaises(delivery.Refused):
            delivery.enqueue(queue, delivery.make_message(RUN, 4))
        self.assertEqual(delivery.queue_summary(queue)["states"], {"pending": 3})

    def test_actual_process_kill_after_recipient_commit_before_ack(self):
        queue, messages = self.queue()
        committed, release = threading.Event(), threading.Event()
        real_commit = witness.commit_message
        first = True

        def held_commit(*args, **kwargs):
            nonlocal first
            result = real_commit(*args, **kwargs)
            if first and args[1] == "durable":
                first = False
                committed.set()
                release.wait(2)
            return result

        command = [sys.executable, str(ROOT / "delivery.py"), "--queue", str(queue)]
        process = None
        try:
            with patch.object(witness, "commit_message", side_effect=held_commit):
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertTrue(committed.wait(3), "recipient commit was not observed")
                process.kill()
                process.communicate(timeout=2)
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(delivery.queue_summary(queue)["states"].get("inflight"), 1)
                self.assertEqual(witness.ledger_summary(self.ledger, 3)["channels"]["durable"]["delivered_unique"], 1)
                release.set()
                resumed = subprocess.run(command, capture_output=True, text=True, timeout=5)
                self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
                summary = delivery.queue_summary(queue)
                self.assertEqual(summary["states"], {"acked": 3})
                self.assertEqual(len(summary["worker_runs"]), 2)
                with contextlib.closing(delivery.connect_db(queue)) as db:
                    self.assertEqual(db.execute("SELECT attempts FROM messages WHERE message_id=?", (messages[0]["message_id"],)).fetchone()[0], 2)
                    self.assertEqual(db.execute("SELECT result FROM attempts ORDER BY id LIMIT 1").fetchone()[0], "interrupted_unknown")
                recipient = witness.ledger_summary(self.ledger, 3)["channels"]["durable"]
                self.assertEqual((recipient["delivered_unique"], recipient["duplicate_requests"], recipient["duplicate_effects"]), (3, 1, 0))
        finally:
            release.set()
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=2)

    def test_attempt_limit_survives_second_worker_run(self):
        policy = {**delivery.DEFAULT_POLICY, "max_attempts": 2, "base_delay_ms": 25,
                  "request_timeout_ms": 50, "total_budget_ms": 1000}
        queue, _ = self.queue(policy=policy)
        self.server.stop()
        result = delivery.work(queue)
        self.assertEqual(result["outcome"], "attempts_exhausted")
        self.assertEqual(result["states"], {"exhausted": 3})
        self.assertEqual(result["attempts"], 6)
        again = delivery.work(queue)
        self.assertEqual(again["attempts"], 6)
        self.assertEqual(again["states"], {"exhausted": 3})

    def test_insufficient_budget_is_not_extended_on_restart(self):
        queue, _ = self.queue(policy={**delivery.DEFAULT_POLICY, "total_budget_ms": 1000})
        # The request needs a bounded connection/write/read window of 1500 ms,
        # larger than this policy's entire budget: neither run may start it.
        before = delivery.queue_summary(queue)
        for _ in range(2):
            result = delivery.work(queue)
            self.assertEqual(result["outcome"], "budget_exhausted")
            self.assertEqual(result["attempts"], 0)
            self.assertEqual(result["states"], {"pending": 3})
        self.assertEqual(before["attempts"], 0)

    def test_tampered_queue_is_reported_as_worker_error(self):
        queue, _ = self.queue()
        with contextlib.closing(delivery.connect_db(queue)) as db, db:
            db.execute("UPDATE messages SET digest=?", ("0" * 64,))
        with self.assertRaises(delivery.Refused):
            delivery.work(queue)
        summary = delivery.queue_summary(queue)
        self.assertEqual(summary["worker_runs"][-1]["outcome"], "worker_error")
        self.assertEqual(summary["attempts"], 0)

    def test_errors_and_http_redirect_are_not_secret_or_ack(self):
        message = delivery.make_message(RUN, 1)
        with patch.object(delivery.http.client, "HTTPConnection") as connection:
            connection.return_value.request.side_effect = TimeoutError("api-key=secret")
            result = delivery.send_once(12345, "durable", message, 500)
            self.assertEqual(result["error"], "timeout")
            self.assertNotIn("secret", json.dumps(result))
            connection.assert_called_once_with("127.0.0.1", 12345, timeout=0.5)
        with patch.object(delivery.http.client, "HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 302
            response.read.return_value = b"private response"
            result = delivery.send_once(12345, "durable", message, 500)
            self.assertFalse(result["ack_verified"])
            self.assertEqual(result["error"], "http_status_not_success")
            self.assertNotIn("private", json.dumps(result))

    def test_bounded_comparison_real_outage_and_restart(self):
        result = lab.run(self.root / "comparison", messages=3)
        self.assertTrue(result["success"], result.get("error"))
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["comparison"]["durable"]["delivered_unique"], 3)
        self.assertEqual(result["comparison"]["durable"]["lost_at_end"], 0)
        self.assertEqual(result["comparison"]["volatile"]["delivered_unique"], 2)
        self.assertEqual(result["comparison"]["volatile"]["lost_at_end"], 1)
        self.assertEqual(result["comparison"]["durable"]["duplicate_effects"], 0)
        self.assertTrue(result["owned_processes_and_server_stopped"])
        self.assertLess(result["elapsed_seconds"], 30)


if __name__ == "__main__":
    unittest.main()
