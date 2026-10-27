"""Owned loopback HTTP witness with durable, channel-separated idempotency."""
from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time

from delivery import ACK_SCHEMA, HOST, Refused, canonical, connect_db, digest, strict_json, validate_message


def initialize(path):
    with path.open("xb"):
        pass
    with contextlib.closing(connect_db(path)) as db, db:
        db.executescript("""
        CREATE TABLE deliveries (
          channel TEXT NOT NULL, message_id TEXT NOT NULL, digest TEXT NOT NULL,
          sequence INTEGER NOT NULL, created_monotonic_ns INTEGER NOT NULL,
          accepted_monotonic_ns INTEGER NOT NULL, accepted_at_ms INTEGER NOT NULL,
          requests INTEGER NOT NULL, PRIMARY KEY(channel,message_id));
        CREATE TABLE dropped_acks (channel TEXT PRIMARY KEY, message_id TEXT NOT NULL);
        CREATE TABLE conflicts (channel TEXT NOT NULL, message_id TEXT NOT NULL);
        """)


def commit_message(path, channel, message, content_digest, lose_first_ack):
    validate_message(message)
    if channel not in {"volatile", "durable"} or digest(message) != content_digest:
        raise Refused("invalid_envelope")
    with contextlib.closing(connect_db(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT digest FROM deliveries WHERE channel=? AND message_id=?",
                           (channel, message["message_id"])).fetchone()
        if prior and prior[0] != content_digest:
            db.execute("INSERT INTO conflicts VALUES(?,?)", (channel, message["message_id"]))
            return None, False, 409
        duplicate = prior is not None
        if duplicate:
            db.execute("UPDATE deliveries SET requests=requests+1 WHERE channel=? AND message_id=?",
                       (channel, message["message_id"]))
        else:
            db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,1)",
                       (channel, message["message_id"], content_digest, message["sequence"], message["created_monotonic_ns"],
                        time.monotonic_ns(), time.time_ns() // 1_000_000))
        dropped = db.execute("SELECT 1 FROM dropped_acks WHERE channel=?", (channel,)).fetchone()
        drop_ack = lose_first_ack and message["sequence"] == 1 and dropped is None
        if drop_ack:
            db.execute("INSERT INTO dropped_acks VALUES(?,?)", (channel, message["message_id"]))
        ack = {"schema": ACK_SCHEMA, "message_id": message["message_id"], "digest": content_digest,
               "committed": True, "duplicate": duplicate}
    # The transaction's commit has completed before this return permits an ACK.
    return ack, drop_ack, 200


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = True

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(1.0)
        return sock, address

    def handle_error(self, request, client_address):
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        if self.path not in {"/deliver/durable", "/deliver/volatile"}:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 4096 or self.headers.get("Transfer-Encoding") is not None:
                raise Refused("invalid_body_length")
            envelope = strict_json(self.rfile.read(length))
            if type(envelope) is not dict or set(envelope) != {"message", "digest"}:
                raise Refused("invalid_envelope")
            ack, drop_ack, status = commit_message(self.server.ledger, self.path.rsplit("/", 1)[-1],
                                                    envelope["message"], envelope["digest"], self.server.lose_first_ack)
        except Exception:
            self.send_error(400)
            return
        if drop_ack:
            # The recipient has committed, but the sender receives no ACK.
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        body = canonical(ack if ack is not None else {"error": "message_id_content_conflict"}).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class Witness:
    def __init__(self, ledger, *, lose_first_ack=True):
        self.ledger = ledger
        self.lose_first_ack = lose_first_ack
        self.port = 0
        self.server = None
        self.thread = None

    def start(self):
        if self.server is not None:
            raise Refused("witness_already_running")
        server = Server((HOST, self.port), Handler)
        server.ledger, server.lose_first_ack = self.ledger, self.lose_first_ack
        self.port = server.server_address[1]
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                raise Refused("witness_did_not_stop")
            self.server = self.thread = None


def ledger_summary(path, planned):
    with contextlib.closing(connect_db(path)) as db:
        result = {}
        for channel in ("volatile", "durable"):
            rows = [dict(r) for r in db.execute("SELECT * FROM deliveries WHERE channel=? ORDER BY sequence", (channel,))]
            delays = [(r["accepted_monotonic_ns"] - r["created_monotonic_ns"]) / 1_000_000 for r in rows]
            unique_ids = {r["message_id"] for r in rows}
            result[channel] = {
                "delivered_unique": len(unique_ids), "not_delivered_at_end": planned - len(unique_ids),
                "duplicate_requests": sum(r["requests"] - 1 for r in rows),
                # One committed ledger row is the only modeled application effect.
                "duplicate_effects": len(rows) - len(unique_ids),
                "received_requests": sum(r["requests"] for r in rows),
                "end_to_end_ms": delays,
                "end_to_end_mean_ms": sum(delays) / len(delays) if delays else None,
                "end_to_end_max_ms": max(delays) if delays else None,
                "delivered_ids": [r["message_id"] for r in rows],
                "delivered_digests": {r["message_id"]: r["digest"] for r in rows},
            }
        return {"channels": result,
                "lost_ack_after_commit": [dict(r) for r in db.execute("SELECT * FROM dropped_acks ORDER BY channel")],
                "content_conflicts": db.execute("SELECT count(*) FROM conflicts").fetchone()[0]}
