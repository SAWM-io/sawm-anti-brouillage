#!/usr/bin/env python3
"""Active civil HTTP delivery to a fixed loopback endpoint, with a durable queue."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
from pathlib import Path
import re
import socket
import sqlite3
import time

import adaptive_policy

HOST = "127.0.0.1"
POLICY_SCHEMA = "resilience-retry-policy/v1"
MESSAGE_SCHEMA = "resilience-demo-message/v1"
ACK_SCHEMA = "resilience-demo-ack/v1"
DEFAULT_POLICY = {"schema": POLICY_SCHEMA, "max_attempts": 12, "base_delay_ms": 100,
                  "max_delay_ms": 1000, "request_timeout_ms": 500, "total_budget_ms": 25000}
MESSAGE_KEYS = {"schema", "message_id", "sequence", "created_at_ms", "created_monotonic_ns", "value"}
ID = re.compile(r"demo-[a-f0-9]{32}-[0-9]{4}\Z")


class Refused(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(message):
    return hashlib.sha256(canonical(message).encode("ascii")).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused("duplicate_json_key")
        result[key] = value
    return result


def strict_json(text):
    def reject_constant(_):
        raise Refused("invalid_json_constant")
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=reject_constant)


def validate_policy(value):
    if type(value) is not dict or set(value) != set(DEFAULT_POLICY) or value["schema"] != POLICY_SCHEMA:
        raise Refused("invalid_policy_fields")
    bounds = {"max_attempts": (1, 20), "base_delay_ms": (25, 1000), "max_delay_ms": (25, 2000),
              "request_timeout_ms": (50, 2000), "total_budget_ms": (1000, 25000)}
    for key, (low, high) in bounds.items():
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise Refused("policy_value_out_of_bounds")
    if value["max_delay_ms"] < value["base_delay_ms"]:
        raise Refused("policy_delay_order")
    return dict(value)


def load_policy(path=None):
    if path is None:
        return dict(DEFAULT_POLICY)
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise Refused("policy_requires_small_regular_file")
    return validate_policy(strict_json(path.read_text(encoding="utf-8")))


def make_message(run_id, sequence):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id) or type(sequence) is not int or not 1 <= sequence <= 20:
        raise Refused("invalid_demo_identity")
    return {"schema": MESSAGE_SCHEMA, "message_id": f"demo-{run_id}-{sequence:04}", "sequence": sequence,
            "created_at_ms": time.time_ns() // 1_000_000, "created_monotonic_ns": time.monotonic_ns(), "value": sequence * 7}


def validate_message(message):
    if type(message) is not dict or set(message) != MESSAGE_KEYS or message["schema"] != MESSAGE_SCHEMA:
        raise Refused("invalid_message_fields")
    if type(message["message_id"]) is not str or not ID.fullmatch(message["message_id"]):
        raise Refused("invalid_message_id")
    for key in ("sequence", "created_at_ms", "created_monotonic_ns", "value"):
        if type(message[key]) is not int or not 0 <= message[key] <= 2**63 - 1:
            raise Refused("invalid_message_number")
    if not 1 <= message["sequence"] <= 20 or message["value"] != message["sequence"] * 7:
        raise Refused("invalid_demo_message")
    if not message["message_id"].endswith(f'-{message["sequence"]:04}'):
        raise Refused("message_identity_mismatch")
    return message


def connect_db(path):
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise Refused("sqlite_requires_regular_file")
    connection = sqlite3.connect(path, timeout=1.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=1000")
    return connection


def create_queue(path, port, count, policy=None, *, adaptive=False):
    policy = validate_policy(policy or DEFAULT_POLICY)
    if type(adaptive) is not bool:
        raise Refused("invalid_adaptive_mode")
    if type(port) is not int or not 1 <= port <= 65535 or type(count) is not int or not 3 <= count <= 20:
        raise Refused("invalid_queue_parameters")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation prevents accidentally adopting another campaign's queue.
    with path.open("xb"):
        pass
    now_ns = time.monotonic_ns()
    metadata = {"schema": "resilience-queue/v1", "host": HOST, "port": port, "planned_messages": count,
                "policy": policy, "origin_monotonic_ns": now_ns,
                "deadline_monotonic_ns": now_ns + policy["total_budget_ms"] * 1_000_000,
                "created_at_ms": time.time_ns() // 1_000_000}
    if adaptive:
        metadata["delivery_mode"] = "adaptive"
        metadata["adaptive_algorithm"] = adaptive_policy.ALGORITHM
    with contextlib.closing(connect_db(path)) as db, db:
        db.executescript("""
        CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), json TEXT NOT NULL);
        CREATE TABLE messages (
          message_id TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('pending','inflight','acked','exhausted')),
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt_ns INTEGER NOT NULL,
          acked_at_ms INTEGER, last_error TEXT);
        CREATE TABLE attempts (
          id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, attempt INTEGER NOT NULL,
          started_monotonic_ns INTEGER NOT NULL, finished_monotonic_ns INTEGER,
          result TEXT NOT NULL, duration_ms REAL, error TEXT, http_status INTEGER,
          duplicate_ack INTEGER);
        CREATE TABLE worker_runs (
          id INTEGER PRIMARY KEY, started_at_ms INTEGER NOT NULL, finished_at_ms INTEGER, outcome TEXT);
        """)
        db.execute("INSERT INTO metadata VALUES(1,?)", (canonical(metadata),))
        if adaptive:
            db.execute("CREATE TABLE adaptive_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), json TEXT NOT NULL)")
            db.execute("CREATE TABLE calibration_events (attempt_id INTEGER PRIMARY KEY, json TEXT NOT NULL)")
            db.execute("INSERT INTO adaptive_state VALUES(1,?)", (canonical(adaptive_policy.initial_state(metadata)),))
    return metadata


def metadata_for(db):
    row = db.execute("SELECT json FROM metadata WHERE singleton=1").fetchone()
    if row is None:
        raise Refused("queue_metadata_missing")
    value = strict_json(row[0])
    if value.get("schema") != "resilience-queue/v1" or value.get("host") != HOST:
        raise Refused("queue_scope_invalid")
    mode = value.get("delivery_mode", "static")
    adaptive_tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('adaptive_state','calibration_events')")}
    if (mode not in {"static", "adaptive"} or
            (mode == "adaptive" and adaptive_tables != {"adaptive_state", "calibration_events"}) or
            (mode == "static" and adaptive_tables)):
        raise Refused("queue_delivery_mode_invalid")
    if "adaptive_algorithm" in value and (mode != "adaptive" or value["adaptive_algorithm"] not in
                                           (adaptive_policy.LEGACY_ALGORITHM, adaptive_policy.ALGORITHM)):
        raise Refused("queue_adaptive_algorithm_invalid")
    validate_policy(value["policy"])
    if type(value.get("port")) is not int or not 1 <= value["port"] <= 65535:
        raise Refused("queue_port_invalid")
    if type(value.get("planned_messages")) is not int or not 3 <= value["planned_messages"] <= 20:
        raise Refused("queue_count_invalid")
    if not 0 <= value["origin_monotonic_ns"] < value["deadline_monotonic_ns"]:
        raise Refused("queue_clock_invalid")
    if value["deadline_monotonic_ns"] - value["origin_monotonic_ns"] != value["policy"]["total_budget_ms"] * 1_000_000:
        raise Refused("queue_budget_invalid")
    return value


def adaptive_snapshot(db, metadata):
    # Multiple SELECTs must see one committed version, including while another
    # process is completing an attempt. A savepoint also preserves any caller's
    # surrounding transaction without committing it.
    db.execute("SAVEPOINT adaptive_read")
    try:
        return _adaptive_snapshot(db, metadata)
    finally:
        db.execute("RELEASE SAVEPOINT adaptive_read")


def _adaptive_snapshot(db, metadata):
    """Replay completed attempts to reject inconsistent state and trace on restart.

    The SQLite file is local trusted storage, not an authenticated remote log.
    Replay detects state/trace/config corruption; it does not authenticate a
    database rewritten consistently by someone controlling the entire file.
    """
    try:
        row = db.execute("SELECT json FROM adaptive_state WHERE singleton=1").fetchone()
        if row is None:
            raise Refused("adaptive_state_missing")
        saved = strict_json(row[0])
        adaptive_policy.validate_state(metadata, saved)
        state = adaptive_policy.initial_state(metadata)
        events = {r[0]: strict_json(r[1]) for r in db.execute("SELECT attempt_id,json FROM calibration_events ORDER BY attempt_id")}
        trace = []
        for attempt in db.execute("SELECT * FROM attempts ORDER BY id"):
            if attempt["result"] in {"started", "interrupted_unknown"}:
                if attempt["finished_monotonic_ns"] is not None or attempt["id"] in events:
                    raise Refused("adaptive_unfinished_observation")
                continue
            if attempt["result"] not in {"ack_verified", "unacknowledged"} or attempt["finished_monotonic_ns"] is None:
                raise Refused("adaptive_attempt_invalid")
            verified = attempt["result"] == "ack_verified"
            if verified and (attempt["http_status"] != 200 or attempt["duplicate_ack"] not in (0, 1)):
                raise Refused("adaptive_ack_record_invalid")
            result = {"ack_verified": verified, "duration_ms": attempt["duration_ms"], "error": attempt["error"]}
            state, event = adaptive_policy.observe(metadata, state, result, attempt["id"])
            if canonical(events.pop(attempt["id"], None)) != canonical(event):
                raise Refused("adaptive_trace_mismatch")
            trace.append(event)
        if events or canonical(saved) != canonical(state):
            raise Refused("adaptive_state_mismatch")
        return {"state": state, "calibration_trace": trace}
    except adaptive_policy.InvalidAdaptiveState as exc:
        raise Refused(str(exc)) from exc


def enqueue(path, message):
    validate_message(message)
    body, content_digest = canonical(message), digest(message)
    with contextlib.closing(connect_db(path)) as db, db:
        metadata = metadata_for(db)
        prior = db.execute("SELECT digest FROM messages WHERE message_id=?", (message["message_id"],)).fetchone()
        if prior:
            if prior[0] != content_digest:
                raise Refused("message_id_content_conflict")
            return False
        count = db.execute("SELECT count(*) FROM messages").fetchone()[0]
        if count >= metadata["planned_messages"]:
            raise Refused("queue_capacity_reached")
        db.execute("INSERT INTO messages(message_id,digest,body,state,next_attempt_ns) VALUES(?,?,?,'pending',?)",
                   (message["message_id"], content_digest, body, time.monotonic_ns()))
    return True


def validate_ack(body, message, content_digest):
    if len(body) > 2048:
        raise Refused("ack_too_large")
    ack = strict_json(body)
    if (type(ack) is not dict or set(ack) != {"schema", "message_id", "digest", "committed", "duplicate"}
            or ack["schema"] != ACK_SCHEMA or ack["message_id"] != message["message_id"]
            or ack["digest"] != content_digest or ack["committed"] is not True
            or type(ack["duplicate"]) is not bool):
        raise Refused("ack_not_verified")
    return ack


def error_code(exc):
    if isinstance(exc, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, (ConnectionResetError, http.client.RemoteDisconnected)):
        return "connection_closed_without_ack"
    if isinstance(exc, Refused):
        return "invalid_ack"
    if isinstance(exc, (json.JSONDecodeError, UnicodeError)):
        return "invalid_ack"
    if isinstance(exc, http.client.HTTPException):
        return "http_protocol_error"
    return "transport_failure"


def send_once(port, channel, message, timeout_ms):
    validate_message(message)
    if channel not in {"durable", "volatile"} or type(port) is not int or not 1 <= port <= 65535:
        raise Refused("invalid_local_destination")
    if type(timeout_ms) is not int or not 50 <= timeout_ms <= 2000:
        raise Refused("invalid_request_timeout")
    content_digest = digest(message)
    body = canonical({"message": message, "digest": content_digest}).encode("ascii")
    started = time.monotonic_ns()
    connection = http.client.HTTPConnection(HOST, port, timeout=timeout_ms / 1000)
    result = {"message_id": message["message_id"], "channel": channel, "ack_verified": False,
              "http_status": None, "error": None, "duplicate_ack": False,
              "started_monotonic_ns": started}
    try:
        connection.request("POST", "/deliver/" + channel, body=body,
                           headers={"Content-Type": "application/json", "Connection": "close"})
        response = connection.getresponse()
        result["http_status"] = response.status
        response_body = response.read(2049)
        if response.status != 200:
            result["error"] = "http_status_not_success"
        else:
            ack = validate_ack(response_body, message, content_digest)
            result.update(ack_verified=True, duplicate_ack=ack["duplicate"])
    except Exception as exc:
        result["error"] = error_code(exc)
    finally:
        connection.close()
    result["finished_monotonic_ns"] = time.monotonic_ns()
    result["duration_ms"] = (result["finished_monotonic_ns"] - started) / 1_000_000
    return result


@contextlib.contextmanager
def worker_guard(path):
    # A separate SQLite transaction provides a portable process lock. An OS
    # process exit releases it without deleting/reclaiming a PID lock file.
    guard_path = Path(path).with_name(Path(path).stem + ".worker.sqlite")
    db = connect_db(guard_path)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS guard (id INTEGER PRIMARY KEY)")
        db.commit()
        db.execute("PRAGMA busy_timeout=0")
        try:
            db.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise Refused("worker_already_running") from exc
        yield
    finally:
        db.rollback()
        db.close()


def queue_summary(path):
    with contextlib.closing(connect_db(path)) as db:
        metadata = metadata_for(db)
        states = {row[0]: row[1] for row in db.execute("SELECT state,count(*) FROM messages GROUP BY state")}
        result = {"planned_messages": metadata["planned_messages"], "states": states,
                "attempts": db.execute("SELECT count(*) FROM attempts").fetchone()[0],
                "worker_runs": [dict(r) for r in db.execute("SELECT * FROM worker_runs ORDER BY id")]}
        if metadata.get("delivery_mode") == "adaptive":
            result["adaptive"] = adaptive_snapshot(db, metadata)
        return result


def work(path):
    path = Path(path)
    if not path.is_file():
        raise Refused("queue_missing")
    with worker_guard(path), contextlib.closing(connect_db(path)) as db:
        metadata = metadata_for(db)
        policy = metadata["policy"]
        adaptive = metadata.get("delivery_mode") == "adaptive"
        controller = adaptive_snapshot(db, metadata)["state"] if adaptive else None
        if time.monotonic_ns() < metadata["origin_monotonic_ns"]:
            raise Refused("host_monotonic_clock_changed")
        with db:
            # An interrupted attempt is ambiguous, so preserve its attempt count
            # and retry the same ID/digest rather than guessing delivery failed.
            db.execute("UPDATE attempts SET result='interrupted_unknown' WHERE result='started'")
            db.execute("UPDATE messages SET state='pending',last_error='interrupted_unknown' WHERE state='inflight'")
            run_id = db.execute("INSERT INTO worker_runs(started_at_ms) VALUES(?)", (time.time_ns() // 1_000_000,)).lastrowid
        outcome = "budget_exhausted"
        try:
            while time.monotonic_ns() < metadata["deadline_monotonic_ns"]:
                with db:
                    db.execute("UPDATE messages SET state='exhausted' WHERE state='pending' AND attempts>=?", (policy["max_attempts"],))
                counts = {r[0]: r[1] for r in db.execute("SELECT state,count(*) FROM messages GROUP BY state")}
                if counts.get("acked", 0) == metadata["planned_messages"]:
                    outcome = "all_acknowledged"
                    break
                if sum(counts.values()) == metadata["planned_messages"] and not counts.get("pending", 0):
                    outcome = "attempts_exhausted"
                    break
                row = db.execute("SELECT * FROM messages WHERE state='pending' AND next_attempt_ns<=? ORDER BY rowid LIMIT 1",
                                 (time.monotonic_ns(),)).fetchone()
                if row is None:
                    time.sleep(0.01)
                    continue
                message = validate_message(strict_json(row["body"]))
                if digest(message) != row["digest"]:
                    raise Refused("queued_message_digest_mismatch")
                attempt = row["attempts"] + 1
                started = time.monotonic_ns()
                remaining_ms = (metadata["deadline_monotonic_ns"] - started) // 1_000_000
                request_timeout_ms = controller["request_timeout_ms"] if adaptive else policy["request_timeout_ms"]
                if remaining_ms < request_timeout_ms * 3:
                    break
                with db:
                    db.execute("UPDATE messages SET state='inflight',attempts=? WHERE message_id=?", (attempt, row["message_id"]))
                    attempt_id = db.execute("INSERT INTO attempts(message_id,attempt,started_monotonic_ns,result) VALUES(?,?,?,'started')",
                                            (row["message_id"], attempt, started)).lastrowid
                result = send_once(metadata["port"], "durable", message, request_timeout_ms)
                with db:
                    if adaptive:
                        try:
                            controller, calibration = adaptive_policy.observe(metadata, controller, result, attempt_id)
                        except adaptive_policy.InvalidAdaptiveState as exc:
                            raise Refused(str(exc)) from exc
                    if result["ack_verified"]:
                        db.execute("UPDATE messages SET state='acked',acked_at_ms=?,last_error=NULL WHERE message_id=?",
                                   (time.time_ns() // 1_000_000, row["message_id"]))
                    else:
                        delay_ms = (controller["retry_delay_ms"] if adaptive else
                                    min(policy["max_delay_ms"], policy["base_delay_ms"] * 2 ** (attempt - 1)))
                        state = "exhausted" if attempt >= policy["max_attempts"] else "pending"
                        db.execute("UPDATE messages SET state=?,next_attempt_ns=?,last_error=? WHERE message_id=?",
                                   (state, time.monotonic_ns() + delay_ms * 1_000_000, result["error"], row["message_id"]))
                    db.execute("UPDATE attempts SET finished_monotonic_ns=?,result=?,duration_ms=?,error=?,http_status=?,duplicate_ack=? WHERE id=?",
                               (result["finished_monotonic_ns"], "ack_verified" if result["ack_verified"] else "unacknowledged",
                                result["duration_ms"], result["error"], result["http_status"], int(result["duplicate_ack"]), attempt_id))
                    if adaptive:
                        db.execute("UPDATE adaptive_state SET json=? WHERE singleton=1", (canonical(controller),))
                        db.execute("INSERT INTO calibration_events VALUES(?,?)", (attempt_id, canonical(calibration)))
        except BaseException:
            outcome = "worker_error"
            raise
        finally:
            with db:
                db.execute("UPDATE worker_runs SET finished_at_ms=?,outcome=? WHERE id=?",
                           (time.time_ns() // 1_000_000, outcome, run_id))
        return {"schema": "resilience-worker-result/v1", "outcome": outcome, **queue_summary(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = work(args.queue)
    except Exception as exc:
        # Local paths, SQL diagnostics and response bodies must not leak.
        print(canonical({"ok": False, "error": str(exc) if isinstance(exc, Refused) else type(exc).__name__}))
        return 2
    print(canonical(result))
    return 0 if result["outcome"] == "all_acknowledged" else 2


if __name__ == "__main__":
    raise SystemExit(main())
