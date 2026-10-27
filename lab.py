#!/usr/bin/env python3
"""Bounded civil application-delivery experiment on owned loopback sockets."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
import uuid

import delivery
import witness

BUDGET_SECONDS = 30.0
OUTAGE_SECONDS = 2.0


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def wait_until(predicate, deadline):
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise delivery.Refused("phase_deadline_exceeded")


def received_failures(queue):
    with contextlib.closing(delivery.connect_db(queue)) as db:
        return db.execute("SELECT count(*) FROM attempts WHERE error='connection_refused'").fetchone()[0]


def run(output, *, messages=10, policy_path=None):
    if type(messages) is not int or not 3 <= messages <= 20:
        raise delivery.Refused("messages_must_be_3_to_20")
    policy = delivery.load_policy(policy_path)
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    started = time.monotonic()
    deadline = started + BUDGET_SECONDS - 1.0
    run_id = uuid.uuid4().hex
    queue, ledger = output / "queue.sqlite", output / "witness.sqlite"
    server = witness.Witness(ledger)
    worker = None
    logs = []
    all_messages, volatile_results = [], []
    report = {"schema": "resilience-applicative-lab/v1", "success": False, "run_id": run_id,
              "started_at_ms": time.time_ns() // 1_000_000,
              "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
              "scope": {"civil_application_alternative": True, "host": delivery.HOST, "cloud_access": False,
                        "system_network_changes": False, "radio_or_vehicle_access": False,
                        "effect": "One unique committed witness ledger row per channel and message ID.",
                        "clock_scope": "End-to-end uses the same host monotonic clock; process restart within this host boot only.",
                        "comparison": "Same IDs/content, one shared server/port/outage; independent per-channel application ledgers.",
                        "duration_limit_seconds": BUDGET_SECONDS, "outage_requested_seconds": OUTAGE_SECONDS},
              "policy": policy, "planned_messages_per_channel": messages, "phases": [], "checks": {}}
    report["source_sha256"] = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                               for name in ("delivery.py", "witness.py", "lab.py")}
    baseline = min(2, messages // 3)
    recovery = min(3, messages // 3)
    during_outage = messages - baseline - recovery

    def spawn_worker():
        log = (output / f"worker-{len(logs) + 1}.log").open("x", encoding="utf-8")
        logs.append(log)
        return subprocess.Popen([sys.executable, str(Path(__file__).with_name("delivery.py")), "--queue", str(queue)],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log)

    def publish(count, phase):
        for _ in range(count):
            if time.monotonic() >= deadline:
                raise delivery.Refused("lab_budget_exhausted")
            message = delivery.make_message(run_id, len(all_messages) + 1)
            all_messages.append(message)
            delivery.enqueue(queue, message)
            result = delivery.send_once(server.port, "volatile", message, policy["request_timeout_ms"])
            result["phase"] = phase
            volatile_results.append(result)
            save(output / "messages.json", all_messages)
            save(output / "volatile-attempts.json", volatile_results)

    try:
        witness.initialize(ledger)
        server.start()
        metadata = delivery.create_queue(queue, server.port, messages, policy)
        report["endpoint"] = {"host": delivery.HOST, "port": server.port}
        report["queue_metadata"] = metadata
        save(output / "policy.json", policy)
        worker = spawn_worker()
        publish(baseline, "server_available")
        wait_until(lambda: delivery.queue_summary(queue)["states"].get("acked", 0) == baseline,
                   min(deadline, time.monotonic() + 5))
        report["phases"].append({"phase": "server_available", "messages": baseline,
                                 "durable_acknowledged": baseline, "finished_at_ms": time.time_ns() // 1_000_000})

        server.stop()
        outage_start = time.monotonic()
        report["phases"].append({"phase": "server_stopped", "started_at_ms": time.time_ns() // 1_000_000})
        publish(during_outage, "server_stopped")
        wait_until(lambda: received_failures(queue) > 0, min(deadline, time.monotonic() + 1))
        killed_pid = worker.pid
        worker.kill()
        worker.wait(timeout=2)
        report["sender_interruption"] = {"owned_pid": killed_pid, "exit_code": worker.returncode,
                                         "queue_after_kill": delivery.queue_summary(queue),
                                         "at_ms": time.time_ns() // 1_000_000}
        worker = spawn_worker()
        report["sender_restart"] = {"owned_pid": worker.pid, "at_ms": time.time_ns() // 1_000_000}
        remaining = OUTAGE_SECONDS - (time.monotonic() - outage_start)
        if remaining > 0:
            time.sleep(remaining)
        server.start()
        report["outage_measured_seconds"] = time.monotonic() - outage_start
        report["phases"].append({"phase": "server_restored", "started_at_ms": time.time_ns() // 1_000_000})
        publish(recovery, "server_restored")
        worker.wait(timeout=max(0.01, deadline - time.monotonic()))
        report["worker_final_exit_code"] = worker.returncode
        summary = delivery.queue_summary(queue)
        received = witness.ledger_summary(ledger, messages)
        report["queue_final"] = summary
        report["recipient"] = received
        durable, volatile = received["channels"]["durable"], received["channels"]["volatile"]
        report["volatile_acknowledged"] = sum(r["ack_verified"] for r in volatile_results)
        report["volatile_unacknowledged"] = messages - report["volatile_acknowledged"]
        report["comparison"] = {
            "durable": {**durable, "lost_at_end": durable["not_delivered_at_end"],
                        "sender_acknowledged": summary["states"].get("acked", 0),
                        "pending_or_exhausted": sum(v for k, v in summary["states"].items() if k != "acked")},
            "volatile": {**volatile, "lost_at_end": volatile["not_delivered_at_end"],
                         "sender_acknowledged": report["volatile_acknowledged"], "retained_for_retry": 0},
        }
        checks = {
            "same_message_set_submitted_to_both_channels": len(all_messages) == len(volatile_results) == messages,
            "all_recipient_digests_match_source": all(
                {m["message_id"]: delivery.digest(m) for m in all_messages}.get(message_id) == content_digest
                for channel in received["channels"].values()
                for message_id, content_digest in channel["delivered_digests"].items()),
            "outage_at_least_two_seconds": report["outage_measured_seconds"] >= 2.0,
            "volatile_failures_during_outage": all(r["error"] == "connection_refused" for r in volatile_results if r["phase"] == "server_stopped"),
            "sender_process_restarted": report["sender_interruption"]["exit_code"] != 0 and len(summary["worker_runs"]) == 2,
            "durable_all_unique_delivered": set(durable["delivered_ids"]) == {m["message_id"] for m in all_messages},
            "durable_all_ack_verified": summary["states"] == {"acked": messages} and worker.returncode == 0,
            "volatile_loss_matches_offline_messages": volatile["not_delivered_at_end"] == during_outage,
            "lost_ack_injected_after_commit_both_channels": len(received["lost_ack_after_commit"]) == 2,
            "durable_retry_after_lost_ack": durable["duplicate_requests"] >= 1,
            "no_duplicate_application_effects": durable["duplicate_effects"] == volatile["duplicate_effects"] == 0,
            "no_content_conflict": received["content_conflicts"] == 0,
            "nonnegative_end_to_end": all(t >= 0 for c in received["channels"].values() for t in c["end_to_end_ms"]),
        }
        report["checks"].update(checks)
        report["success"] = all(checks.values())
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "code": str(exc) if isinstance(exc, delivery.Refused) else "local_lab_failure"}
    finally:
        worker_stopped = True
        try:
            if worker is not None and worker.poll() is None:
                worker.kill()
                worker.wait(timeout=2)
        except Exception:
            worker_stopped = False
            report["worker_cleanup_error"] = "owned_worker_cleanup_failed"
            report["success"] = False
        try:
            server.stop()
            report["owned_processes_and_server_stopped"] = worker_stopped
        except Exception:
            report["owned_processes_and_server_stopped"] = False
            report["success"] = False
        for log in logs:
            log.close()
        report["elapsed_seconds"] = time.monotonic() - started
        report["checks"]["duration_below_30_seconds"] = report["elapsed_seconds"] < 30.0
        report["success"] = report["success"] and report["checks"]["duration_below_30_seconds"]
        report["finished_at_ms"] = time.time_ns() // 1_000_000
        report["artifact_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir()) if p.is_file()}
        save(output / "report.json", report)
        save(output / "measurements.json", {"schema": "resilience-applicative-measurements/v1", "run_id": run_id,
                                              "observed_at_ms": report["finished_at_ms"], "success": report["success"],
                                              "scope": report["scope"], "comparison": report.get("comparison"),
                                              "outage_measured_seconds": report.get("outage_measured_seconds"),
                                              "checks": report["checks"], "cloud_sent": False})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--messages", type=int, default=10)
    parser.add_argument("--policy", type=Path)
    args = parser.parse_args()
    try:
        result = run(args.output_dir, messages=args.messages, policy_path=args.policy)
    except Exception as exc:
        print(delivery.canonical({"success": False, "error": str(exc) if isinstance(exc, delivery.Refused) else type(exc).__name__}))
        return 2
    print(delivery.canonical({"success": result["success"], "report": str(args.output_dir / "report.json"),
                             "elapsed_seconds": result["elapsed_seconds"], "cloud_sent": False}))
    return 0 if result["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
