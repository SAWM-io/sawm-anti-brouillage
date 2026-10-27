#!/usr/bin/env python3
"""Bounded real-HTTP comparison on owned loopback sockets; no radio validation."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import platform
import socket
import subprocess
import sys
import threading
import time

import delivery
import witness

PROFILES = ("normal", "latency_jump_recovery", "ack_loss_after_commit", "blackout_return")
SEED = 20261027
ARRIVAL_INTERVAL_MS = 50
CAMPAIGN_CAP_SECONDS = 180.0
RUN_CAP_SECONDS = 25.0
RESTART_AT_MS = 600
RESTART_PAUSE_MS = 100
SOURCE_NAMES = ("delivery.py", "adaptive_policy.py", "witness.py", "adaptive_lab.py")


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes():
    return {name: sha256(Path(__file__).with_name(name)) for name in SOURCE_NAMES}


def payloads(profile, repetition, *, seed=SEED, messages=20):
    """Identical fictitious bytes across modes; payload clocks are logical, not timing inputs."""
    if profile not in PROFILES or type(repetition) is not int or not 1 <= repetition <= 3 or messages != 20:
        raise delivery.Refused("invalid_workload")
    run_id = hashlib.sha256(f"{seed}/{profile}/{repetition}".encode("ascii")).hexdigest()[:32]
    return [{"schema": delivery.MESSAGE_SCHEMA, "message_id": f"demo-{run_id}-{i:04}",
             "sequence": i, "value": i * 7, "created_at_ms": 1700000000000 + (i - 1) * 50,
             "created_monotonic_ns": (i - 1) * 50_000_000} for i in range(1, messages + 1)]


def schedule(repetitions):
    if type(repetitions) is not int or not 1 <= repetitions <= 3:
        raise delivery.Refused("repetitions_must_be_1_to_3")
    return [{"profile": profile, "repetition": repetition,
             "order": ["static", "adaptive"] if (repetition + index) % 2 else ["adaptive", "static"]}
            for repetition in range(1, repetitions + 1) for index, profile in enumerate(PROFILES)]


def phase_for(profile, elapsed_ms):
    if profile == "normal":
        return "healthy"
    if elapsed_ms < 300:
        return "before_disruption"
    if elapsed_ms < 1000:
        return "disruption"
    return "recovery"


def fault_decision(profile, elapsed_ms, sequence, request_number, seed=SEED):
    """Fixture-only schedule. The worker never receives this information."""
    if profile not in PROFILES:
        raise delivery.Refused("invalid_fault_profile")
    phase = phase_for(profile, elapsed_ms)
    jitter = hashlib.sha256(f"{seed}/{profile}/{sequence}/{request_number}".encode("ascii")).digest()[0] % 4
    delay = (180 if profile == "latency_jump_recovery" and phase == "disruption" else 8) + jitter
    action = "ack"
    if profile == "blackout_return" and phase == "disruption":
        action = "hold_without_commit"
    elif profile == "ack_loss_after_commit" and 7 <= sequence <= 14 and request_number == 1:
        # Message-keyed loss keeps the identical loss set despite different retry timing.
        action = "drop_ack_after_commit"
    return {"phase": phase, "action": action, "response_delay_ms": delay}


class FaultServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(1)
        return connection, address

    def handle_error(self, request, client_address):
        pass


class FaultHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        fixture = self.server.fixture
        event = None
        try:
            if self.path != "/deliver/durable":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 4096 or self.headers.get("Transfer-Encoding") is not None:
                raise delivery.Refused("invalid_body_length")
            envelope = delivery.strict_json(self.rfile.read(length))
            if type(envelope) is not dict or set(envelope) != {"message", "digest"}:
                raise delivery.Refused("invalid_envelope")
            message = delivery.validate_message(envelope["message"])
            if delivery.digest(message) != envelope["digest"]:
                raise delivery.Refused("invalid_digest")
            event = fixture.begin(message)
            if event["action"] == "hold_without_commit":
                fixture.stopping.wait(fixture.blackout_hold_ms / 1000)
                return
            if fixture.stopping.wait(event["response_delay_ms"] / 1000):
                return
            ack, _, status = witness.commit_message(fixture.ledger, "durable", message, envelope["digest"], False)
            event["committed"] = status == 200
            event["commit_returned_monotonic_ns"] = time.monotonic_ns()
            if event["action"] == "drop_ack_after_commit":
                return
            body = delivery.canonical(ack).encode("ascii")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            event["ack_write_completed"] = True
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            if event is not None:
                event["client_disconnected"] = True
        except Exception as exc:
            fixture.errors.append(type(exc).__name__)
            with contextlib.suppress(OSError):
                self.send_error(400)
        finally:
            if event is not None:
                event["finished_monotonic_ns"] = time.monotonic_ns()
                fixture.finish(event)
            self.close_connection = True
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)


class FaultFixture:
    """Owns its ephemeral loopback listener; accepts no external destination."""
    def __init__(self, ledger, profile, *, seed=SEED, timeout_ms=500):
        if profile not in PROFILES:
            raise delivery.Refused("invalid_fault_profile")
        self.ledger, self.profile, self.seed = Path(ledger), profile, seed
        self.blackout_hold_ms = timeout_ms + 150
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.counts, self.events, self.errors = {}, [], []
        self.active = 0
        self.origin_ns = time.monotonic_ns()
        self.server = FaultServer((delivery.HOST, 0), FaultHandler)
        self.server.fixture = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def begin(self, message):
        now = time.monotonic_ns()
        elapsed = (now - self.origin_ns) / 1_000_000
        with self.lock:
            message_id = message["message_id"]
            number = self.counts.get(message_id, 0) + 1
            self.counts[message_id] = number
            self.active += 1
        return {"message_id": message_id, "sequence": message["sequence"], "request_number": number,
                "started_monotonic_ns": now, "started_offset_ms": elapsed, "committed": False,
                "ack_write_completed": False,
                **fault_decision(self.profile, elapsed, message["sequence"], number, self.seed)}

    def finish(self, event):
        with self.lock:
            self.events.append(event)
            self.active -= 1

    def stop(self):
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        deadline = time.monotonic() + 2
        while self.active and time.monotonic() < deadline:
            time.sleep(.005)
        if self.thread.is_alive() or self.active:
            raise delivery.Refused("fixture_cleanup_incomplete")


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def snapshot(queue):
    with contextlib.closing(delivery.connect_db(queue)) as db:
        return {"metadata": delivery.metadata_for(db),
                "messages": [dict(row) for row in db.execute("SELECT * FROM messages ORDER BY rowid")],
                "attempts": [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY id")],
                "summary": delivery.queue_summary(queue)}


def restart_checks(before, after):
    before_attempts, after_attempts = before["attempts"], after["attempts"]
    counts = {row["message_id"]: row["attempts"] for row in after["messages"]}
    checks = {"restart_kept_metadata_and_deadline": before["metadata"] == after["metadata"],
              "restart_kept_attempt_counts": all(counts.get(row["message_id"], -1) >= row["attempts"] for row in before["messages"]),
              "restart_kept_attempt_history": all(
                  a["id"] == b["id"] and a["message_id"] == b["message_id"] and a["attempt"] == b["attempt"]
                  and a["started_monotonic_ns"] == b["started_monotonic_ns"]
                  for a, b in zip(before_attempts, after_attempts)) and len(after_attempts) >= len(before_attempts)}
    if "adaptive" in before["summary"]:
        old, new = before["summary"]["adaptive"], after["summary"]["adaptive"]
        trace, continued = old["calibration_trace"], new["calibration_trace"]
        checks["restart_kept_calibration_trace"] = continued[:len(trace)] == trace
        next_event = continued[len(trace):len(trace) + 1]
        checks["restart_resumed_persisted_controller"] = bool(next_event) and all(
            next_event[0]["old_" + key] == old["state"][key] for key in ("request_timeout_ms", "retry_delay_ms"))
        checks["controller_was_calibrated_before_restart"] = old["state"]["observed_attempts"] > 0
    return checks


def analyze(queue, ledger, arrivals, planned):
    data = snapshot(queue)
    with contextlib.closing(delivery.connect_db(ledger)) as db:
        delivered = [dict(row) for row in db.execute("SELECT * FROM deliveries WHERE channel='durable' ORDER BY sequence")]
        conflicts = db.execute("SELECT count(*) FROM conflicts").fetchone()[0]
    enqueued = {row["message_id"]: row["enqueue_started_monotonic_ns"] for row in arrivals}
    expected = {message["message_id"]: delivery.digest(message) for message in planned}
    delivery_ms = [(row["accepted_monotonic_ns"] - enqueued[row["message_id"]]) / 1_000_000 for row in delivered]
    ack_ms = [(row["finished_monotonic_ns"] - enqueued[row["message_id"]]) / 1_000_000
              for row in data["attempts"] if row["result"] == "ack_verified"]
    completed = [row for row in data["attempts"] if row["finished_monotonic_ns"] is not None]
    states = data["summary"]["states"]
    adaptive = data["summary"].get("adaptive", {})
    trace = adaptive.get("calibration_trace", [])
    delivered_ids = {row["message_id"] for row in delivered}
    metrics = {"planned": len(planned), "enqueued": len(arrivals), "delivered_unique": len(delivered_ids),
               "not_delivered": len(planned) - len(delivered_ids), "acked": states.get("acked", 0),
               "pending": states.get("pending", 0), "inflight": states.get("inflight", 0),
               "exhausted": states.get("exhausted", 0), "unacknowledged": len(planned) - states.get("acked", 0),
               "attempts": len(data["attempts"]), "completed_attempts": len(completed),
               "timeouts": sum(row["error"] == "timeout" for row in completed),
               "duplicate_requests": sum(row["requests"] - 1 for row in delivered),
               "duplicate_effects": len(delivered) - len(delivered_ids), "content_conflicts": conflicts,
               "delivery_p50_ms": percentile(delivery_ms, .5), "delivery_p95_ms": percentile(delivery_ms, .95),
               "ack_p50_ms": percentile(ack_ms, .5), "ack_p95_ms": percentile(ack_ms, .95),
               "policy_changes": sum(any(event["old_" + key] != event["new_" + key]
                                         for key in ("request_timeout_ms", "retry_delay_ms")) for event in trace),
               "calibration_observations": len(trace),
               "all_messages_delivered_and_acked": len(delivered_ids) == states.get("acked", 0) == len(planned),
               "delivered_digests": {row["message_id"]: row["digest"] for row in delivered}}
    checks = {"all_enqueued_content_matches_plan": {row["message_id"]: row["digest"] for row in data["messages"]} == expected,
              "all_received_content_matches_source": all(expected.get(row["message_id"]) == row["digest"] for row in delivered),
              "unique_application_effects": metrics["duplicate_effects"] == 0,
              "no_content_conflicts": conflicts == 0,
              "acks_have_committed_effect": all(row["message_id"] in delivered_ids for row in data["messages"] if row["state"] == "acked"),
              "attempt_limits_respected": all(row["attempts"] <= data["metadata"]["policy"]["max_attempts"] for row in data["messages"]),
              "attempt_count_matches_history": sum(row["attempts"] for row in data["messages"]) == len(data["attempts"]),
              "nonnegative_measured_latency": all(value >= 0 for value in delivery_ms + ack_ms)}
    if adaptive:
        checks["exactly_one_calibration_per_completed_attempt"] = [event["attempt_id"] for event in trace] == [row["id"] for row in completed]
        checks["immutable_controller_limits"] = adaptive["state"]["limits"]["policy"] == data["metadata"]["policy"]
    return data, delivered, metrics, checks


def run_trial(output, *, profile, mode, repetition, policy, messages, campaign_deadline, seed=SEED):
    """A trial preserves failure artifacts and stops only its own worker/listener."""
    if mode not in {"static", "adaptive"} or profile not in PROFILES:
        raise delivery.Refused("invalid_trial")
    output.mkdir(mode=0o700)
    started = time.monotonic()
    run_deadline = min(campaign_deadline, started + RUN_CAP_SECONDS - 2.5)
    report = {"schema": "resilience-adaptive-trial/v1", "success": False, "profile": profile, "mode": mode,
              "repetition": repetition, "seed": seed, "checks": {}, "started_at_ms": time.time_ns() // 1_000_000,
              "initial_policy": policy, "source_sha256": source_hashes()}
    queue, ledger = output / "queue.sqlite", output / "witness.sqlite"
    fixture = worker = None
    logs, arrivals = [], []
    before_restart = None
    save(output / "messages.json", messages)
    save(output / "policy.json", policy)

    def spawn():
        log = (output / f"worker-{len(logs) + 1}.log").open("x", encoding="utf-8")
        logs.append(log)
        return subprocess.Popen([sys.executable, str(Path(__file__).with_name("delivery.py")), "--queue", str(queue)],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log)

    try:
        witness.initialize(ledger)
        fixture = FaultFixture(ledger, profile, seed=seed, timeout_ms=policy["request_timeout_ms"]).start()
        metadata = delivery.create_queue(queue, fixture.port, len(messages), policy, adaptive=mode == "adaptive")
        report["queue_metadata_initial"] = metadata
        report["endpoint"] = {"host": delivery.HOST, "port": fixture.port}
        origin_ns = fixture.origin_ns = time.monotonic_ns()
        report["origin_monotonic_ns"] = origin_ns
        worker = spawn()
        actions = [(index * ARRIVAL_INTERVAL_MS, "enqueue", message) for index, message in enumerate(messages)]
        if profile == "blackout_return":
            actions += [(RESTART_AT_MS, "kill", None), (RESTART_AT_MS + RESTART_PAUSE_MS, "restart", None)]
        for offset_ms, action, message in sorted(actions, key=lambda value: value[0]):
            target = origin_ns / 1e9 + offset_ms / 1000
            while time.monotonic() < target:
                if time.monotonic() >= run_deadline:
                    raise delivery.Refused("trial_time_cap_reached")
                time.sleep(min(.005, max(0, target - time.monotonic())))
            if time.monotonic() >= run_deadline:
                raise delivery.Refused("trial_time_cap_reached")
            if action == "enqueue":
                now = time.monotonic_ns()
                delivery.enqueue(queue, message)
                arrivals.append({"message_id": message["message_id"], "scheduled_offset_ms": offset_ms,
                                 "enqueue_started_monotonic_ns": now, "enqueue_finished_monotonic_ns": time.monotonic_ns(),
                                 "arrival_lateness_ms": (now - origin_ns) / 1_000_000 - offset_ms})
            elif action == "kill":
                if worker.poll() is not None:
                    raise delivery.Refused("worker_exited_before_scheduled_restart")
                worker.kill()
                worker.wait(timeout=2)
                before_restart = snapshot(queue)
                save(output / "restart-snapshot.json", before_restart)
                report["restart"] = {"killed_owned_pid": worker.pid, "exit_code": worker.returncode,
                                     "kill_offset_ms": (time.monotonic_ns() - origin_ns) / 1_000_000}
            else:
                worker = spawn()
                report["restart"]["new_owned_pid"] = worker.pid
                report["restart"]["restart_offset_ms"] = (time.monotonic_ns() - origin_ns) / 1_000_000
        worker.wait(timeout=max(.001, run_deadline - time.monotonic()))
        report["worker_exit_code"] = worker.returncode
        report["worker_completed"] = worker.returncode in {0, 2}
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "code": str(exc) if isinstance(exc, delivery.Refused) else "local_trial_failure"}
    finally:
        cleanup = True
        try:
            if worker is not None and worker.poll() is None:
                worker.kill()
                worker.wait(timeout=2)
        except Exception:
            cleanup = False
        try:
            if fixture is not None:
                fixture.stop()
        except Exception:
            cleanup = False
        for log in logs:
            log.close()
        save(output / "arrivals.json", arrivals)
        if fixture is not None:
            save(output / "fixture-events.json", sorted(fixture.events, key=lambda row: row["started_monotonic_ns"]))
            report["fixture_errors"] = fixture.errors
            report["injected_ack_losses_after_commit"] = sum(event["action"] == "drop_ack_after_commit" and event["committed"] for event in fixture.events)
            report["blackout_requests_without_commit"] = sum(event["action"] == "hold_without_commit" for event in fixture.events)
        try:
            final, delivered, metrics, checks = analyze(queue, ledger, arrivals, messages)
            save(output / "queue-final.json", final)
            save(output / "recipient.json", delivered)
            report["metrics"], report["checks"] = metrics, checks
            report["queue_summary"] = final["summary"]
            worker_runs = final["summary"]["worker_runs"]
            report["worker_outcome"] = worker_runs[-1]["outcome"] if worker_runs else None
            report["checks"]["worker_reached_known_outcome"] = report["worker_outcome"] in {"all_acknowledged", "attempts_exhausted", "budget_exhausted"}
            report["checks"]["initial_policy_and_deadline_unchanged"] = final["metadata"] == report["queue_metadata_initial"]
            if before_restart is not None:
                report["checks"].update(restart_checks(before_restart, final))
                report["checks"]["both_restart_actions_inside_blackout"] = 300 <= report["restart"]["kill_offset_ms"] < report["restart"]["restart_offset_ms"] < 1000
            attempts_by_id = {row["id"]: row for row in final["attempts"]}
            trace = final["summary"].get("adaptive", {}).get("calibration_trace", [])
            save(output / "calibration-postanalysis.json", [{**event,
                "attempt_finished_offset_ms": (attempts_by_id[event["attempt_id"]]["finished_monotonic_ns"] - report["origin_monotonic_ns"]) / 1_000_000,
                "phase_annotation_after_run": phase_for(profile, (attempts_by_id[event["attempt_id"]]["started_monotonic_ns"] - report["origin_monotonic_ns"]) / 1_000_000)} for event in trace])
        except Exception as exc:
            report["analysis_error"] = type(exc).__name__
        report["elapsed_seconds"] = time.monotonic() - started
        report["checks"]["owned_resources_stopped"] = cleanup
        report["checks"]["fixture_without_internal_errors"] = not report.get("fixture_errors", [])
        report["checks"]["within_trial_cap"] = report["elapsed_seconds"] < RUN_CAP_SECONDS
        report["success"] = bool(report.get("worker_completed")) and not report.get("error") and not report.get("analysis_error") and all(report["checks"].values())
        report["finished_at_ms"] = time.time_ns() // 1_000_000
        report["artifact_sha256"] = {path.name: sha256(path) for path in sorted(output.iterdir()) if path.is_file()}
        save(output / "report.json", report)
    return report


def aggregate_reports(reports):
    metrics = ("delivered_unique", "acked", "attempts", "timeouts", "delivery_p50_ms", "delivery_p95_ms", "ack_p50_ms", "ack_p95_ms", "policy_changes")
    groups = []
    for profile in PROFILES:
        for mode in ("static", "adaptive"):
            selected = [report for report in reports if report["profile"] == profile and report["mode"] == mode]
            values = {}
            for key in metrics:
                observed = [report.get("metrics", {}).get(key) for report in selected]
                numeric = [value for value in observed if value is not None]
                values[key] = {"observed": observed, "mean": sum(numeric) / len(numeric) if numeric else None,
                               "min": min(numeric) if numeric else None, "max": max(numeric) if numeric else None}
            groups.append({"profile": profile, "mode": mode, "runs": len(selected),
                           "valid_runs": sum(report["success"] for report in selected), "metrics": values})
    pairs = []
    for profile in PROFILES:
        for repetition in (1, 2, 3):
            pair = {report["mode"]: report for report in reports if report["profile"] == profile and report["repetition"] == repetition}
            if set(pair) != {"static", "adaptive"}:
                continue
            deltas = {}
            for key in metrics:
                left, right = pair["static"].get("metrics", {}).get(key), pair["adaptive"].get("metrics", {}).get(key)
                deltas[key] = right - left if left is not None and right is not None else None
            pairs.append({"profile": profile, "repetition": repetition,
                          "both_runs_valid": all(report["success"] for report in pair.values()),
                          "adaptive_minus_static": deltas})
    return {"groups": groups, "paired_deltas": pairs,
            "interpretation": "Raw measured observations, including failed runs. Positive latency/attempt deltas mean adaptive used more. No adaptive gain is assumed; three repetitions do not establish general performance."}


def chart_svg(aggregate):
    """Small standalone evidence chart; standard-library SVG, never fabricated values."""
    rows = aggregate["groups"]
    measured = [row["metrics"]["ack_p95_ms"]["mean"] for row in rows]
    maximum = max([value for value in measured if value is not None] + [1])
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="530" viewBox="0 0 1000 530">',
             '<rect width="1000" height="530" fill="#fafafa"/>',
             '<g font-family="Arial,sans-serif" fill="#171717">',
             '<text x="30" y="36" font-size="22">Owned localhost HTTP: mean trial ACK p95</text>',
             '<text x="30" y="61" font-size="14">Transport-fault emulation only; radio anti-jamming is not validated.</text>']
    for index, row in enumerate(rows):
        y, value = 110 + index * 45, measured[index]
        width = 0 if value is None else value / maximum * 500
        color = "#171717" if row["mode"] == "adaptive" else "#929292"
        label = row["profile"] + " / " + row["mode"]
        parts.append(f'<text x="30" y="{y + 18}" font-size="14">{label}</text>')
        parts.append(f'<rect x="345" y="{y}" width="{width:.1f}" height="26" rx="3" fill="{color}"/>')
        text = "no data" if value is None else f"{value:.1f} ms"
        parts.append(f'<text x="{355 + width:.1f}" y="{y + 18}" font-size="14">{text}</text>')
    parts.append('<text x="30" y="500" font-size="13">Nearest-rank p95 per run, then mean across runs; paired raw observations are in aggregate.json.</text></g></svg>')
    return "\n".join(parts) + "\n"


def run(output, *, repetitions=3, messages=20, policy_path=None, budget_seconds=CAMPAIGN_CAP_SECONDS):
    plan = schedule(repetitions)
    if type(messages) is not int or messages != 20:
        raise delivery.Refused("benchmark_requires_20_messages")
    if type(budget_seconds) not in (int, float) or not 1 <= budget_seconds <= CAMPAIGN_CAP_SECONDS:
        raise delivery.Refused("campaign_budget_out_of_bounds")
    policy = delivery.load_policy(policy_path)
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    start, trials = time.monotonic(), []
    deadline = start + budget_seconds - 5.0
    report = {"schema": "resilience-adaptive-comparison/v1", "success": False, "seed": SEED,
              "started_at_ms": time.time_ns() // 1_000_000, "initial_policy": policy,
              "source_sha256": source_hashes(), "repetitions": repetitions, "planned_trials": len(plan) * 2,
              "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                           "python": platform.python_version()},
              "scope": {"host": delivery.HOST, "owned_processes_only": True, "real_http": True, "real_sqlite_commit_and_ack": True,
                        "cloud_access": False, "system_network_changes": False, "radio_validation": False,
                        "claim": "Transport-fault emulation on this host; not validated radio anti-jamming.",
                        "effect": "One unique committed witness ledger row per message ID.",
                        "success_semantics": "success means the experiment completed with valid evidence; complete delivery is separately reported as all_messages_delivered_and_acked.",
                        "clock": "Same-host monotonic clock; latency measured from actual enqueue start, not fictitious payload clocks.",
                        "payloads": "20 nonsecret fictitious messages; identical canonical bytes across paired modes.",
                        "fault_schedule": "Fixed 300–1000 ms disruption. ACK losses keyed to first requests for messages 7–14.",
                        "arrival_schedule": "20 messages scheduled every 50 ms; actual enqueue lateness is retained.",
                        "restart": "Both modes killed at 600 ms and restarted at 700 ms in blackout_return.",
                        "controller_inputs": "Only completed network observations; profile and phase labels are fixture/postanalysis only.",
                        "campaign_cap_seconds": budget_seconds, "trial_cap_seconds": RUN_CAP_SECONDS},
              "schedule": plan, "trials": [], "checks": {}}
    save(output / "policy.json", policy)
    save(output / "report.json", report)
    try:
        for pair in plan:
            workload = payloads(pair["profile"], pair["repetition"])
            for position, mode in enumerate(pair["order"]):
                if time.monotonic() >= deadline:
                    raise delivery.Refused("campaign_time_cap_reached")
                name = f"r{pair['repetition']}-{pair['profile']}-{mode}"
                trial = run_trial(output / name, profile=pair["profile"], mode=mode, repetition=pair["repetition"],
                                  policy=policy, messages=workload, campaign_deadline=deadline)
                trials.append(trial)
                report["trials"].append({"directory": name, "profile": pair["profile"], "mode": mode,
                                         "repetition": pair["repetition"], "order_position": position + 1,
                                         "success": trial["success"], "metrics": trial.get("metrics"), "error": trial.get("error")})
                save(output / "report.json", report)
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "code": str(exc) if isinstance(exc, delivery.Refused) else "local_campaign_failure"}
    finally:
        aggregate = aggregate_reports(trials)
        save(output / "aggregate.json", aggregate)
        (output / "ack-p95.svg").write_text(chart_svg(aggregate), encoding="utf-8")
        report["elapsed_seconds"] = time.monotonic() - start
        report["completed_trials"] = len(trials)
        paired_equal = []
        for pair in plan:
            paths = [output / f"r{pair['repetition']}-{pair['profile']}-{mode}" / "messages.json" for mode in ("static", "adaptive")]
            if all(path.exists() for path in paths):
                paired_equal.append(sha256(paths[0]) == sha256(paths[1]))
        report["checks"] = {"all_planned_trials_completed": len(trials) == len(plan) * 2,
                            "all_trial_integrity_checks_pass": bool(trials) and all(trial["success"] for trial in trials),
                            "paired_message_bytes_identical": len(paired_equal) == len(plan) and all(paired_equal),
                            "within_campaign_cap": report["elapsed_seconds"] < budget_seconds}
        report["source_sha256_at_end"] = source_hashes()
        report["checks"]["source_unchanged_during_campaign"] = report["source_sha256"] == report["source_sha256_at_end"]
        report["success"] = not report.get("error") and all(report["checks"].values())
        report["finished_at_ms"] = time.time_ns() // 1_000_000
        save(output / "report.json", report)
        # Manifest excludes itself and hashes final report bytes, including failures.
        save(output / "manifest.json", {"schema": "resilience-artifact-manifest/v1", "sha256": {
            str(path.relative_to(output)): sha256(path) for path in sorted(output.rglob("*")) if path.is_file() and path.name != "manifest.json"}})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory only; raw evidence is retained here.")
    parser.add_argument("--repetitions", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--messages", type=int, choices=(20,), default=20)
    parser.add_argument("--policy", type=Path, help="Optional locally saved initial SAWM policy; never fetched in the hot loop.")
    args = parser.parse_args()
    try:
        report = run(args.output_dir, repetitions=args.repetitions, messages=args.messages, policy_path=args.policy)
    except Exception as exc:
        print(delivery.canonical({"success": False, "error": str(exc) if isinstance(exc, delivery.Refused) else type(exc).__name__}))
        return 2
    print(delivery.canonical({"success": report["success"], "completed_trials": report["completed_trials"],
                              "elapsed_seconds": report["elapsed_seconds"], "report": str(args.output_dir / "report.json"),
                              "radio_validation": False, "cloud_sent": False}))
    return 0 if report["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
