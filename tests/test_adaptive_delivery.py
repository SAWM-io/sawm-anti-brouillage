"""Causal calibration, SQLite atomicity, and owned loopback crash/restart tests."""
import contextlib
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
import adaptive_policy
import delivery
import witness

RUN = "a" * 32


class Clock:
    def __init__(self):
        self.ns = 100_000_000_000

    def monotonic_ns(self):
        return self.ns

    def sleep(self, seconds):
        self.ns += round(seconds * 1_000_000_000)

    @contextlib.contextmanager
    def patched(self):
        with patch.object(delivery.time, "monotonic_ns", self.monotonic_ns), patch.object(delivery.time, "sleep", self.sleep):
            yield self


class AdaptiveDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def queue(self, name="queue", *, policy=None, adaptive=True, port=12345, count=3):
        path = self.root / (name + ".sqlite")
        delivery.create_queue(path, port, count, policy, adaptive=adaptive)
        for sequence in range(1, count + 1):
            delivery.enqueue(path, delivery.make_message(RUN, sequence))
        return path

    def fake_sender(self, clock, observations, sent):
        def send(port, channel, message, timeout_ms):
            started = clock.monotonic_ns()
            sent.append(timeout_ms)
            observation = observations.pop(0)
            if isinstance(observation, BaseException):
                raise observation
            error, duration = observation
            if error == "timeout":
                duration = timeout_ms
            clock.sleep(duration / 1000)
            return {"ack_verified": error is None, "duration_ms": float(duration), "error": error,
                    "http_status": 200 if error in (None, "invalid_ack") else None,
                    "duplicate_ack": False, "started_monotonic_ns": started,
                    "finished_monotonic_ns": clock.monotonic_ns()}
        return send

    def test_default_remains_static_and_adaptive_mode_is_explicit(self):
        queue = self.queue(adaptive=False)
        self.assertEqual(set(delivery.queue_summary(queue)), {"planned_messages", "states", "attempts", "worker_runs"})
        adaptive = self.queue("adaptive")
        with contextlib.closing(delivery.connect_db(adaptive)) as db:
            self.assertEqual(delivery.metadata_for(db)["delivery_mode"], "adaptive")
        for flag in (1, "true", None):
            with self.subTest(flag=flag), self.assertRaises(delivery.Refused):
                delivery.create_queue(self.root / "invalid.sqlite", 12345, 3, adaptive=flag)

    def test_real_work_loop_recalibrates_causally_and_treats_timeout_as_censored(self):
        clock, sent = Clock(), []
        with clock.patched():
            queue = self.queue()
            observations = [(None, 20), ("timeout", 0), ("invalid_ack", 2), (None, 40), (None, 15)]
            with patch.object(delivery, "send_once", self.fake_sender(clock, observations, sent)):
                result = delivery.work(queue)
        self.assertEqual(result["states"], {"acked": 3})
        trace, state = result["adaptive"]["calibration_trace"], result["adaptive"]["state"]
        self.assertEqual(sent, [delivery.DEFAULT_POLICY["request_timeout_ms"]] + [e["new_request_timeout_ms"] for e in trace[:-1]])
        self.assertLess(sent[1], sent[0])
        self.assertGreater(sent[2], sent[1])
        self.assertEqual(trace[1]["reason"], "timeout_censored_backoff")
        self.assertEqual(trace[1]["timeout_censored_ms"], sent[1])
        self.assertIsNone(trace[1]["verified_rtt_ms"])
        self.assertEqual(trace[2]["reason"], "invalid_ack_no_rtt_sample")
        self.assertIsNone(trace[2]["verified_rtt_ms"])
        self.assertEqual(trace[2]["new_request_timeout_ms"], trace[2]["old_request_timeout_ms"])
        for event in trace[1:3]:
            self.assertEqual(event["metrics"]["srtt_ms"], trace[0]["metrics"]["srtt_ms"])
            self.assertGreaterEqual(event["new_retry_delay_ms"], event["old_retry_delay_ms"])
        self.assertLess(trace[3]["new_retry_delay_ms"], trace[3]["old_retry_delay_ms"])
        self.assertEqual((state["observed_attempts"], state["verified_acks"], state["timeout_observations"]), (5, 3, 1))
        for event in trace:
            self.assertTrue(50 <= event["new_request_timeout_ms"] <= 500)
            self.assertTrue(25 <= event["new_retry_delay_ms"] <= 1000)

    def test_independent_missing_acks_do_not_compound_global_retry_delay(self):
        clock, sent, requests = Clock(), [], {}
        with clock.patched():
            queue = self.queue(count=20)

            def send(port, channel, message, timeout_ms):
                sequence = message["sequence"]
                requests[sequence] = requests.get(sequence, 0) + 1
                error = "connection_closed_without_ack" if sequence > 1 and requests[sequence] == 1 else None
                return self.fake_sender(clock, [(error, 1 if error else 10)], sent)(port, channel, message, timeout_ms)

            with patch.object(delivery, "send_once", send):
                result = delivery.work(queue)
        self.assertEqual(result["states"], {"acked": 20})
        self.assertEqual(result["attempts"], 39)
        trace = result["adaptive"]["calibration_trace"]
        missing = [event for event in trace if event["observed_error"] == "connection_closed_without_ack"]
        self.assertEqual(len(missing), 19)
        for event in missing:
            self.assertEqual(event["reason"], "ambiguous_ack_no_rtt_sample")
            self.assertEqual(event["algorithm"], adaptive_policy.ALGORITHM)
            self.assertEqual(event["new_retry_delay_ms"], delivery.DEFAULT_POLICY["base_delay_ms"])
            self.assertEqual(event["new_request_timeout_ms"], event["old_request_timeout_ms"])
            self.assertIsNone(event["verified_rtt_ms"])
            self.assertIsNone(event["timeout_censored_ms"])
            self.assertEqual(event["metrics"]["srtt_ms"], trace[0]["metrics"]["srtt_ms"])
            self.assertEqual(event["metrics"]["verified_acks"], 1)
        # Each message fails once: its retained due date proves the trace's
        # retry delay is actually applied by the worker, not merely reported.
        with contextlib.closing(delivery.connect_db(queue)) as db:
            for row in db.execute("SELECT m.next_attempt_ns,a.finished_monotonic_ns FROM messages m JOIN attempts a USING(message_id) WHERE a.error='connection_closed_without_ack'"):
                self.assertEqual(row[0] - row[1], delivery.DEFAULT_POLICY["base_delay_ms"] * 1_000_000)

    def test_ambiguous_ack_retains_backoff_justified_by_other_failures(self):
        queue = self.queue()
        with contextlib.closing(delivery.connect_db(queue)) as db:
            metadata = delivery.metadata_for(db)
        state = adaptive_policy.initial_state(metadata)
        events = []
        for attempt, error in enumerate(("timeout", "connection_closed_without_ack", "connection_refused"), 1):
            state, event = adaptive_policy.observe(metadata, state,
                {"ack_verified": False, "duration_ms": 500 if error == "timeout" else 1, "error": error}, attempt)
            events.append(event)
        self.assertGreater(events[0]["new_retry_delay_ms"], events[0]["old_retry_delay_ms"])
        self.assertEqual(events[1]["new_retry_delay_ms"], events[0]["new_retry_delay_ms"])
        self.assertGreater(events[2]["new_retry_delay_ms"], events[1]["new_retry_delay_ms"])
        self.assertEqual(state["verified_acks"], 0)
        self.assertIsNone(state["srtt_ms"])
        self.assertEqual(state["timeout_observations"], 1)

    def test_pre_revision_queue_replays_and_resumes_its_original_algorithm(self):
        clock, sent = Clock(), []
        with clock.patched():
            queue = self.queue()
            # Reproduce the original queue format, before an algorithm field
            # existed. Its completed events must retain their original meaning.
            with contextlib.closing(delivery.connect_db(queue)) as db, db:
                metadata = delivery.metadata_for(db)
                metadata.pop("adaptive_algorithm")
                db.execute("UPDATE metadata SET json=?", (delivery.canonical(metadata),))
                db.execute("UPDATE adaptive_state SET json=?", (delivery.canonical(adaptive_policy.initial_state(metadata)),))
            observations = [("connection_closed_without_ack", 1)] * 2 + [KeyboardInterrupt()] + [(None, 10)] * 3
            with patch.object(delivery, "send_once", self.fake_sender(clock, observations, sent)):
                with self.assertRaises(KeyboardInterrupt):
                    delivery.work(queue)
                before = delivery.queue_summary(queue)["adaptive"]
                after = delivery.work(queue)["adaptive"]
            trace = before["calibration_trace"]
            self.assertEqual([event["new_retry_delay_ms"] for event in trace], [200, 400])
            self.assertTrue(all(event["reason"] == "failure_backoff" and "algorithm" not in event for event in trace))
            self.assertEqual(after["calibration_trace"][:2], trace)
            self.assertEqual(after["calibration_trace"][2]["old_retry_delay_ms"], before["state"]["retry_delay_ms"])

    def test_restart_reproduces_same_state_for_same_completed_observations(self):
        snapshots = []
        for interrupted in (False, True):
            clock, sent = Clock(), []
            observations = [(None, 20), ("timeout", 0), (None, 30), (None, 10)]
            if interrupted:
                observations.insert(2, KeyboardInterrupt())
            with clock.patched():
                queue = self.queue("restarted" if interrupted else "continuous")
                sender = self.fake_sender(clock, observations, sent)
                with patch.object(delivery, "send_once", sender):
                    if interrupted:
                        with self.assertRaises(KeyboardInterrupt):
                            delivery.work(queue)
                        before = delivery.queue_summary(queue)["adaptive"]
                        self.assertEqual(before["state"]["observed_attempts"], 2)
                    result = delivery.work(queue)
                    again = delivery.work(queue)
                    self.assertEqual(result["adaptive"], again["adaptive"])
            snapshots.append(result)
        self.assertEqual(snapshots[0]["adaptive"]["state"], snapshots[1]["adaptive"]["state"])
        self.assertEqual(snapshots[1]["attempts"], snapshots[0]["attempts"] + 1)
        self.assertEqual(len(snapshots[1]["adaptive"]["calibration_trace"]), 4)

    def test_attempt_budget_and_deadline_never_extend_on_failures_or_restart(self):
        policy = {**delivery.DEFAULT_POLICY, "max_attempts": 2, "request_timeout_ms": 100,
                  "base_delay_ms": 25, "max_delay_ms": 50, "total_budget_ms": 1000}
        clock, sent = Clock(), []
        with clock.patched():
            queue = self.queue(policy=policy)
            with contextlib.closing(delivery.connect_db(queue)) as db:
                metadata = delivery.metadata_for(db)
            with patch.object(delivery, "send_once", self.fake_sender(clock, [("connection_refused", 1)] * 6, sent)):
                result = delivery.work(queue)
                again = delivery.work(queue)
            self.assertEqual((result["attempts"], again["attempts"]), (6, 6))
            self.assertEqual(result["states"], {"exhausted": 3})
            self.assertEqual(result["adaptive"]["state"]["retry_delay_ms"], 50)
            with contextlib.closing(delivery.connect_db(queue)) as db:
                self.assertEqual(metadata, delivery.metadata_for(db))
        clock = Clock()
        with clock.patched():
            queue = self.queue("expired", policy=policy)
            clock.sleep(1.001)
            with patch.object(delivery, "send_once") as send:
                self.assertEqual(delivery.work(queue)["outcome"], "budget_exhausted")
                self.assertEqual(delivery.work(queue)["attempts"], 0)
                send.assert_not_called()

    def test_attempt_completion_state_and_event_roll_back_together(self):
        clock, sent = Clock(), []
        with clock.patched():
            queue = self.queue()
            with contextlib.closing(delivery.connect_db(queue)) as db, db:
                db.execute("CREATE TRIGGER fail_calibration BEFORE INSERT ON calibration_events BEGIN SELECT RAISE(ABORT,'injected'); END")
            with patch.object(delivery, "send_once", self.fake_sender(clock, [(None, 20)], sent)):
                with self.assertRaises(sqlite3.IntegrityError):
                    delivery.work(queue)
            summary = delivery.queue_summary(queue)
            self.assertEqual(summary["adaptive"]["state"]["observed_attempts"], 0)
            self.assertEqual(summary["adaptive"]["calibration_trace"], [])
            self.assertEqual(summary["states"], {"inflight": 1, "pending": 2})
            with contextlib.closing(delivery.connect_db(queue)) as db, db:
                self.assertEqual(db.execute("SELECT result,finished_monotonic_ns FROM attempts").fetchone()[:], ("started", None))
                db.execute("DROP TRIGGER fail_calibration")
            with patch.object(delivery, "send_once", self.fake_sender(clock, [(None, 20)] * 3, sent)):
                result = delivery.work(queue)
            self.assertEqual(result["states"], {"acked": 3})
            self.assertEqual(result["adaptive"]["state"]["observed_attempts"], 3)
            self.assertEqual(result["attempts"], 4)

    def test_invalid_nan_tampered_state_trace_and_config_are_refused(self):
        mutations = [lambda s: s.update(request_timeout_ms=501), lambda s: s.update(request_timeout_ms=True),
                     lambda s: s.update(retry_delay_ms=24), lambda s: s.update(srtt_ms=float("nan")),
                     lambda s: s.update(observed_attempts=1), lambda s: s.update(extra="untrusted"),
                     lambda s: s.update(request_timeout_ms=100)]
        for index, mutation in enumerate(mutations):
            queue = self.queue("bad" + str(index))
            with contextlib.closing(delivery.connect_db(queue)) as db, db:
                state = json.loads(db.execute("SELECT json FROM adaptive_state").fetchone()[0])
                mutation(state)
                db.execute("UPDATE adaptive_state SET json=?", (json.dumps(state),))
            with self.subTest(index=index), self.assertRaises(delivery.Refused):
                delivery.work(queue)
        for index, mutation in enumerate((lambda m: m.pop("delivery_mode"),
                                          lambda m: m.pop("adaptive_algorithm"),
                                          lambda m: m.update(adaptive_algorithm="unrecognized"),
                                          lambda m: m["policy"].update(max_attempts=20),
                                          lambda m: m.update(deadline_monotonic_ns=m["deadline_monotonic_ns"] + 1))):
            queue = self.queue("config" + str(index))
            with contextlib.closing(delivery.connect_db(queue)) as db, db:
                metadata = delivery.metadata_for(db)
                mutation(metadata)
                db.execute("UPDATE metadata SET json=?", (delivery.canonical(metadata),))
            with self.subTest(config=index), self.assertRaises(delivery.Refused):
                delivery.work(queue)
        queue = self.queue("trace")
        with contextlib.closing(delivery.connect_db(queue)) as db, db:
            db.execute("INSERT INTO calibration_events VALUES(1, '{}')")
        with self.assertRaises(delivery.Refused):
            delivery.work(queue)

    def test_invalid_observations_never_enter_estimator(self):
        queue = self.queue()
        with contextlib.closing(delivery.connect_db(queue)) as db:
            metadata = delivery.metadata_for(db)
        state = adaptive_policy.initial_state(metadata)
        for duration in (-1, float("nan"), float("inf"), True):
            with self.subTest(duration=duration), self.assertRaises(adaptive_policy.InvalidAdaptiveState):
                adaptive_policy.observe(metadata, state, {"ack_verified": True, "duration_ms": duration, "error": None}, 1)
        self.assertEqual(state, adaptive_policy.initial_state(metadata))

    def test_real_invalid_ack_is_never_recorded_as_success(self):
        ledger = self.root / "invalid-ledger.sqlite"
        witness.initialize(ledger)
        server = witness.Witness(ledger, lose_first_ack=False).start()
        self.addCleanup(server.stop)
        policy = {**delivery.DEFAULT_POLICY, "max_attempts": 2, "base_delay_ms": 25, "max_delay_ms": 50}
        queue = self.queue(port=server.port, policy=policy)
        real_commit = witness.commit_message

        def wrong_digest(*args, **kwargs):
            ack, dropped, status = real_commit(*args, **kwargs)
            return {**ack, "digest": "0" * 64}, dropped, status

        with patch.object(witness, "commit_message", side_effect=wrong_digest):
            result = delivery.work(queue)
        self.assertEqual(result["states"], {"exhausted": 3})
        self.assertEqual(result["adaptive"]["state"]["verified_acks"], 0)
        self.assertIsNone(result["adaptive"]["state"]["srtt_ms"])
        self.assertTrue(all(e["reason"] == "invalid_ack_no_rtt_sample" for e in result["adaptive"]["calibration_trace"]))
        self.assertEqual(witness.ledger_summary(ledger, 3)["channels"]["durable"]["duplicate_effects"], 0)

    def test_real_process_restart_preserves_calibration_and_unknown_attempt(self):
        ledger = self.root / "ledger.sqlite"
        witness.initialize(ledger)
        server = witness.Witness(ledger, lose_first_ack=False).start()
        self.addCleanup(server.stop)
        queue = self.queue(port=server.port)
        committed, release = threading.Event(), threading.Event()
        real_commit, held = witness.commit_message, False

        def held_commit(*args, **kwargs):
            nonlocal held
            result = real_commit(*args, **kwargs)
            if args[2]["sequence"] == 2 and not held:
                held = True
                committed.set()
                release.wait(3)
            return result

        command = [sys.executable, str(ROOT / "delivery.py"), "--queue", str(queue)]
        process = None
        try:
            with patch.object(witness, "commit_message", side_effect=held_commit):
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertTrue(committed.wait(3))
                process.kill()
                process.communicate(timeout=2)
                before = delivery.queue_summary(queue)
                self.assertEqual(before["adaptive"]["state"]["observed_attempts"], 1)
                self.assertEqual(before["states"].get("inflight"), 1)
                release.set()
                resumed = subprocess.run(command, capture_output=True, text=True, timeout=5)
                self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            after = delivery.queue_summary(queue)
            self.assertEqual(after["states"], {"acked": 3})
            self.assertEqual(after["adaptive"]["state"]["limits"], before["adaptive"]["state"]["limits"])
            self.assertEqual(after["adaptive"]["state"]["observed_attempts"], 3)
            self.assertEqual(after["adaptive"]["calibration_trace"][1]["old_request_timeout_ms"], before["adaptive"]["state"]["request_timeout_ms"])
            self.assertEqual([e["attempt_id"] for e in after["adaptive"]["calibration_trace"]], [1, 3, 4])
            with contextlib.closing(delivery.connect_db(queue)) as db:
                unknown = db.execute("SELECT result,finished_monotonic_ns FROM attempts WHERE id=2").fetchone()
                self.assertEqual(tuple(unknown), ("interrupted_unknown", None))
            recipient = witness.ledger_summary(ledger, 3)["channels"]["durable"]
            self.assertEqual((recipient["delivered_unique"], recipient["duplicate_requests"], recipient["duplicate_effects"]), (3, 1, 0))
        finally:
            release.set()
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=2)


if __name__ == "__main__":
    unittest.main()
