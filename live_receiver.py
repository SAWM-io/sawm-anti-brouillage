#!/usr/bin/env python3
"""Loopback-only durable witness and live dashboard for the owned VM demo."""
from __future__ import annotations

import argparse
import contextlib
import html
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

PROFILES = {"healthy", "degraded_a", "cut_a", "cut_both", "recovery"}
IDENTITY = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
LABEL = re.compile(r"[A-Za-z0-9._:-]{1,48}\Z")
MAX_STATUS = 16384


class Rejected(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def require(condition, code="invalid_input", status=400):
    if not condition:
        raise Rejected(code, status)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    def reject(_):
        raise Rejected("nonfinite_json")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, Rejected):
            raise
        raise Rejected("invalid_json") from exc
    require(type(value) is dict, "json_object_required")
    return value


def identity(value, maximum=128):
    require(type(value) is str and len(value) <= maximum and IDENTITY.fullmatch(value), "invalid_identity")
    return value


def number(value, maximum, *, integer=False, nullable=False):
    if nullable and value is None:
        return
    require(type(value) is int if integer else type(value) in (int, float), "invalid_number")
    require(0 <= value <= maximum and math.isfinite(value), "number_out_of_bounds")


def private_file(path, maximum=16384):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise Rejected("private_regular_file_required") from exc
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_size <= maximum, "file_requires_mode_600")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        require(len(raw) <= maximum, "file_too_large")
        return raw
    finally:
        os.close(fd)


def validate_status(value):
    required = {"run_id", "phase", "elapsed_ms", "generated", "acked", "pending", "attempts", "timeouts", "active_path", "paths", "events"}
    optional = {"mode", "scope", "completed", "success"}
    require(required <= set(value) and set(value) <= required | optional, "invalid_status_fields")
    identity(value["run_id"], 96)
    require(type(value["phase"]) is str and LABEL.fullmatch(value["phase"]), "invalid_phase")
    number(value["elapsed_ms"], 180000)
    for key in ("generated", "acked", "pending"):
        number(value[key], 10000, integer=True)
    for key in ("attempts", "timeouts"):
        number(value[key], 100000, integer=True)
    require(value["acked"] <= value["generated"] and value["pending"] <= value["generated"]
            and value["timeouts"] <= value["attempts"], "invalid_status_counts")
    require(value["active_path"] in ("a", "b", None), "invalid_path")
    require(type(value["paths"]) is dict and set(value["paths"]) == {"a", "b"}, "invalid_paths")
    path_required = {"srtt_ms", "timeout_ms", "failures"}
    path_optional = {"rttvar_ms", "successes", "consecutive_failures", "last_attempt_ms", "last_success_ms"}
    for item in value["paths"].values():
        require(type(item) is dict and path_required <= set(item) and set(item) <= path_required | path_optional, "invalid_path_fields")
        for key, val in item.items():
            if key in {"failures", "successes", "consecutive_failures"}:
                number(val, 100000, integer=True)
            else:
                number(val, 180000, nullable=key != "timeout_ms")
    require(type(value["events"]) is list and len(value["events"]) <= 30, "too_many_events")
    for event in value["events"]:
        require(type(event) is dict and {"at_ms", "type"} <= set(event)
                and set(event) <= {"at_ms", "type", "path", "reason", "message_id"}, "invalid_event")
        number(event["at_ms"], 180000)
        require(type(event["type"]) is str and LABEL.fullmatch(event["type"]), "invalid_event_type")
        if "path" in event:
            require(event["path"] in ("a", "b", None), "invalid_event_path")
        if "reason" in event:
            require(type(event["reason"]) is str and len(event["reason"]) <= 240
                    and all(ord(c) >= 32 for c in event["reason"]), "invalid_event_reason")
        if "message_id" in event:
            identity(event["message_id"])
    if "mode" in value:
        require(type(value["mode"]) is str and value["mode"] in {"manual", "scripted"}, "invalid_mode")
    if "scope" in value:
        require(value["scope"] == "two_virtual_paths_shared_ssh_tunnel_no_radio", "invalid_scope")
    for key in ("completed", "success"):
        if key in value:
            require(type(value[key]) is bool, "invalid_completion")
    return value


class Store:
    def __init__(self, directory, token, launcher_config=None):
        self.directory = Path(directory)
        require(not self.directory.is_symlink(), "state_directory_symlink")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(self.directory.is_dir(), "state_directory_required")
        self.db_path = self.directory / "live.sqlite"
        require(not self.db_path.is_symlink(), "database_symlink")
        self.token, self.nonce = token, secrets.token_hex(32)
        self.lock = threading.RLock()
        self.seen = {}
        self.process = None
        self.launch = None
        self.launcher = None
        if launcher_config:
            config = strict_json(private_file(launcher_config))
            require(set(config) == {"argv", "cwd", "log_dir"}, "invalid_launcher_config")
            require(type(config["argv"]) is list and 1 <= len(config["argv"]) <= 32
                    and all(type(x) is str and 0 < len(x) <= 2048 and "\x00" not in x for x in config["argv"])
                    and Path(config["argv"][0]).is_absolute(), "invalid_launcher_argv")
            require(all(type(config[k]) is str and Path(config[k]).is_absolute() for k in ("cwd", "log_dir")), "absolute_launcher_paths_required")
            require(Path(config["cwd"]).is_dir(), "launcher_cwd_missing")
            self.launcher = config
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, started_ms INTEGER NOT NULL, last_status_ms INTEGER,
                    last_message_ms INTEGER, status_json TEXT, conflicts INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS receipts (
                    run_id TEXT NOT NULL, message_id TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL,
                    accepted_ms INTEGER NOT NULL, requests INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(run_id,message_id));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, event_key TEXT NOT NULL, json TEXT NOT NULL,
                    UNIQUE(run_id,event_key));
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS controls (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL, profile TEXT NOT NULL, mode TEXT NOT NULL);
                INSERT OR IGNORE INTO controls VALUES(1,0,'healthy','scripted');
            """)
        os.chmod(self.db_path, 0o600)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _ensure_run(self, db, run_id, now):
        db.execute("INSERT OR IGNORE INTO runs(run_id,started_ms) VALUES(?,?)", (run_id, now))

    def _event(self, db, run_id, event):
        encoded = canonical(event)
        db.execute("INSERT OR IGNORE INTO events(run_id,event_key,json) VALUES(?,?,?)",
                   (run_id, hashlib.sha256(encoded.encode()).hexdigest(), encoded))
        db.execute("DELETE FROM events WHERE run_id=? AND id NOT IN (SELECT id FROM events WHERE run_id=? ORDER BY id DESC LIMIT 300)", (run_id, run_id))

    def message(self, value):
        require(set(value) == {"run_id", "message_id", "payload", "sha256"}, "invalid_message_fields")
        run_id, message_id = identity(value["run_id"], 96), identity(value["message_id"])
        require(type(value["payload"]) is str, "payload_string_required")
        try:
            body = value["payload"].encode("utf-8")
        except UnicodeError as exc:
            raise Rejected("invalid_payload_encoding") from exc
        require(len(body) <= 3000 and type(value["sha256"]) is str
                and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]), "invalid_payload_or_digest")
        require(hmac.compare_digest(hashlib.sha256(body).hexdigest(), value["sha256"]), "digest_mismatch")
        now, duplicate, conflict = int(time.time() * 1000), False, False
        with self.lock, self.db() as db:
            self._ensure_run(db, run_id, now)
            prior = db.execute("SELECT sha256,payload FROM receipts WHERE run_id=? AND message_id=?", (run_id, message_id)).fetchone()
            if prior:
                conflict = prior["sha256"] != value["sha256"] or prior["payload"] != value["payload"]
                if conflict:
                    db.execute("UPDATE runs SET conflicts=conflicts+1 WHERE run_id=?", (run_id,))
                else:
                    duplicate = True
                    db.execute("UPDATE receipts SET requests=requests+1 WHERE run_id=? AND message_id=?", (run_id, message_id))
            else:
                require(db.execute("SELECT count(*) FROM receipts WHERE run_id=?", (run_id,)).fetchone()[0] < 2000, "run_capacity", 413)
                db.execute("INSERT INTO receipts(run_id,message_id,payload,sha256,accepted_ms) VALUES(?,?,?,?,?)", (run_id, message_id, value["payload"], value["sha256"], now))
            db.execute("UPDATE runs SET last_message_ms=? WHERE run_id=?", (now, run_id))
        # Leaving the transaction commits the effect before HTTP can return its ACK.
        require(not conflict, "message_content_conflict", 409)
        return {"run_id": run_id, "message_id": message_id, "sha256": value["sha256"], "committed": True, "duplicate": duplicate}

    def status(self, value):
        value = validate_status(value)
        now, run_id = int(time.time() * 1000), value["run_id"]
        with self.lock, self.db() as db:
            self._ensure_run(db, run_id, now)
            prior = db.execute("SELECT status_json FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
            if prior:
                previous = strict_json(prior)
                require(value["elapsed_ms"] >= previous["elapsed_ms"], "status_out_of_order", 409)
                require(not previous.get("completed") or value.get("completed"), "run_already_completed", 409)
            db.execute("UPDATE runs SET last_status_ms=?,status_json=? WHERE run_id=?", (now, canonical(value), run_id))
            for event in value["events"]:
                self._event(db, run_id, event)
            received = db.execute("SELECT count(*) FROM receipts WHERE run_id=?", (run_id,)).fetchone()[0]
            point = {"elapsed_ms": value["elapsed_ms"], "received": received, "pending": value["pending"],
                     "generated": value["generated"], "acked": value["acked"],
                     "srtt_a_ms": value["paths"]["a"]["srtt_ms"], "srtt_b_ms": value["paths"]["b"]["srtt_ms"]}
            db.execute("INSERT INTO history(run_id,json) VALUES(?,?)", (run_id, canonical(point)))
            db.execute("DELETE FROM history WHERE run_id=? AND id NOT IN (SELECT id FROM history WHERE run_id=? ORDER BY id DESC LIMIT 360)", (run_id, run_id))
            self.seen[run_id] = time.monotonic()
            # Bound process memory; persistent history remains in SQLite.
            if len(self.seen) > 100:
                del self.seen[min(self.seen, key=self.seen.get)]
            return self._command(db)

    def _command(self, db):
        row = db.execute("SELECT version,profile,mode FROM controls WHERE singleton=1").fetchone()
        return {"command_version": row["version"], "profile": row["profile"], "mode": row["mode"]}

    def control(self, value):
        require(set(value) == {"profile"} and type(value["profile"]) is str and value["profile"] in PROFILES, "profile_required")
        with self.lock, self.db() as db:
            db.execute("UPDATE controls SET version=version+1,profile=?,mode='manual' WHERE singleton=1", (value["profile"],))
            latest = db.execute("SELECT run_id,status_json FROM runs ORDER BY started_ms DESC,rowid DESC LIMIT 1").fetchone()
            if latest:
                elapsed = strict_json(latest["status_json"])["elapsed_ms"] if latest["status_json"] else 0
                command = self._command(db)
                self._event(db, latest["run_id"], {"at_ms": elapsed, "type": "operator_profile", "reason": value["profile"], "command_version": command["command_version"]})
            return self._command(db)

    def start(self, value):
        require(value == {}, "start_has_no_arguments")
        with self.lock:
            require(self.launcher is not None, "launcher_not_configured", 409)
            require(self.process is None or self.process.poll() is not None, "demonstration_already_running", 409)
            logs = Path(self.launcher["log_dir"])
            require(not logs.is_symlink(), "log_directory_symlink")
            logs.mkdir(mode=0o700, parents=True, exist_ok=True)
            name = "demo-" + secrets.token_hex(8) + ".log"
            fd = os.open(logs / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            started_ms = int(time.time() * 1000)
            with self.db() as db:
                previous = db.execute("SELECT run_id FROM runs ORDER BY started_ms DESC,rowid DESC LIMIT 1").fetchone()
            try:
                with os.fdopen(fd, "wb") as stream:
                    process = subprocess.Popen(self.launcher["argv"], cwd=self.launcher["cwd"], shell=False,
                                               stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                               start_new_session=True)
            except OSError as exc:
                raise Rejected("launcher_failed", 500) from exc
            self.process = process
            self.launch = {"started_ms": started_ms, "log_name": name, "previous_run_id": previous[0] if previous else None}
            with self.db() as db:
                db.execute("UPDATE controls SET version=version+1,profile='healthy',mode='scripted' WHERE singleton=1")
            return {"started": True}

    def state(self, run_id=None):
        if run_id:
            identity(run_id, 96)
        with self.lock, self.db() as db:
            runs = [dict(row) for row in db.execute("SELECT run_id,started_ms,last_status_ms FROM runs ORDER BY started_ms DESC,rowid DESC LIMIT 100")]
            selected = run_id or (runs[0]["run_id"] if runs else None)
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (selected,)).fetchone() if selected else None
            status = strict_json(row["status_json"]) if row and row["status_json"] else None
            age = max(0, time.monotonic() - self.seen[selected]) if selected in self.seen else None
            finished = bool(status and (status.get("completed") or status["phase"] in {"finished", "completed", "done", "error"}))
            launch_active = self.process is not None and self.process.poll() is None
            run_registered = bool(self.launch and any(run["started_ms"] >= self.launch["started_ms"] and run["run_id"] != self.launch["previous_run_id"] for run in runs))
            # A completed launcher must not make an old run appear connected.
            ended_launch = bool(self.process is not None and not launch_active and self.launch and row
                                and row["started_ms"] >= self.launch["started_ms"])
            fresh = age is not None and age < 3 and not finished and not ended_launch
            receipts, history, events, actual = [], [], [], {"received_unique": 0, "duplicate_requests": 0, "duplicate_effects": 0, "conflicts": 0}
            if row:
                counts = db.execute("SELECT count(*) AS n,count(DISTINCT message_id) AS unique_ids,coalesce(sum(requests-1),0) AS duplicates FROM receipts WHERE run_id=?", (selected,)).fetchone()
                actual.update(received_unique=counts["unique_ids"], duplicate_requests=counts["duplicates"], duplicate_effects=counts["n"]-counts["unique_ids"], conflicts=row["conflicts"])
                receipts = [dict(x) for x in db.execute("SELECT message_id,payload,sha256,accepted_ms,requests FROM receipts WHERE run_id=? ORDER BY accepted_ms DESC,rowid DESC LIMIT 8", (selected,))]
                for receipt in receipts:
                    receipt["payload"] = receipt["payload"].replace(self.token, "[redacted]")[:180]
                history = [json.loads(x[0]) for x in db.execute("SELECT json FROM history WHERE run_id=? ORDER BY id", (selected,))]
                events = [json.loads(x[0]) for x in db.execute("SELECT json FROM events WHERE run_id=? ORDER BY id DESC LIMIT 100", (selected,))]
                for event in events:
                    if "reason" in event:
                        event["reason"] = event["reason"].replace(self.token, "[redacted]")
            if status:
                status = {key: val for key, val in status.items() if key != "events"}
            result = {"run_id": selected, "runs": runs, "status": status, "actual": actual,
                    "fresh": fresh, "finished": finished or ended_launch, "age_seconds": round(age, 2) if age is not None else None,
                    "last_status_ms": row["last_status_ms"] if row else None,
                    "last_message_ms": row["last_message_ms"] if row else None,
                    "history": history, "events": events, "receipts": receipts, "command": self._command(db),
                    "launcher": {"available": self.launcher is not None, "active": launch_active,
                                 "returncode": self.process.poll() if self.process else None,
                                 "started_ms": self.launch["started_ms"] if self.launch else None, "run_registered": run_registered}}
            def public(value):
                if isinstance(value, str):
                    return value.replace(self.token, "[redacted]")
                if isinstance(value, list):
                    return [public(item) for item in value]
                if isinstance(value, dict):
                    return {key: public(item) for key, item in value.items()}
                return value
            return public(result)

    def reference(self):
        value = {"title": "Rapport d’origine", "summary": "La référence sera ajoutée ici. Cette démonstration montre le transport et l’adaptation locale ; elle ne reproduit pas à elle seule le modèle du rapport d’origine.", "source_label": "Référence à documenter"}
        path = self.directory / "report-reference.json"
        if path.exists():
            require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 16384, "invalid_reference_file")
            candidate = strict_json(path.read_bytes())
            for key, maximum in (("title", 160), ("summary", 8000), ("source_label", 500)):
                if key in candidate:
                    require(type(candidate[key]) is str and len(candidate[key]) <= maximum, "invalid_reference_text")
                    value[key] = candidate[key].replace(self.token, "[redacted]")
            if "source_url" in candidate:
                url = candidate["source_url"]
                require(type(url) is str and len(url) <= 2048, "invalid_reference_url")
                parsed = urlsplit(url)
                require(parsed.scheme in {"http", "https"} and parsed.netloc and parsed.username is None, "invalid_reference_url")
                value["source_url"] = url
        return value

    def close(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, *_args):
        pass

    def reply(self, status, value, *, html=False):
        body = value.encode("utf-8") if html else canonical(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'nonce-" + self.server.store.nonce + "'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def host(self):
        require(self.headers.get("Host") == self.server.origin.removeprefix("http://"), "host_not_allowed", 403)

    def body(self, maximum):
        require(self.headers.get("Transfer-Encoding") is None, "transfer_encoding_not_allowed")
        require(self.headers.get_content_type() == "application/json", "application_json_required", 415)
        lengths = self.headers.get_all("Content-Length", [])
        require(len(lengths) == 1 and lengths[0].isdigit(), "content_length_required", 411)
        require(len(lengths[0]) <= 8, "body_too_large", 413)
        length = int(lengths[0])
        require(0 < length <= maximum, "body_too_large", 413)
        raw = self.rfile.read(length)
        require(len(raw) == length, "truncated_body")
        return strict_json(raw)

    def bearer(self):
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.store.token
        require(hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")), "unauthorized", 401)

    def browser_auth(self):
        require(self.headers.get("Origin") == self.server.origin, "origin_not_allowed", 403)
        require(hmac.compare_digest(self.headers.get("X-Demo-Token", "").encode("utf-8"), self.server.store.nonce.encode("ascii")), "invalid_demo_token", 403)

    def do_GET(self):
        try:
            self.host()
            parsed = urlsplit(self.path)
            if parsed.path == "/":
                dashboard = Path(__file__).with_name("live_dashboard.html").read_text()
                self.reply(200, dashboard.replace("__DEMO_NONCE__", self.server.store.nonce), html=True)
            elif parsed.path == "/report-reference":
                value = self.server.store.reference()
                title, summary, label = (html.escape(value[key]) for key in ("title", "summary", "source_label"))
                source = ('<a href="' + html.escape(value["source_url"], quote=True) + '" rel="noopener noreferrer">' + label + '</a>') if "source_url" in value else label
                page = ('<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        '<title>' + title + '</title><style>body{background:#101012;color:#f4f4f5;font:16px/1.7 system-ui;max-width:850px;margin:60px auto;padding:24px}a{color:inherit}h1{font-size:34px}p{white-space:pre-wrap}.label{color:#aaa;font-size:12px}</style>'
                        '<a href="/">← Démonstration transport</a><h1>' + title + '</h1><p>' + summary + '</p><div class="label">' + source + '</div></html>')
                self.reply(200, page, html=True)
            elif parsed.path == "/api/state":
                query = parse_qs(parsed.query)
                require(set(query) <= {"run_id"} and len(query.get("run_id", [])) <= 1, "invalid_query")
                self.reply(200, self.server.store.state(query.get("run_id", [None])[0]))
            else:
                self.reply(404, {"error": "not_found"})
        except Rejected as exc:
            self.reply(exc.status, {"error": exc.code})
        except (sqlite3.Error, OSError, ValueError, TypeError):
            self.reply(500, {"error": "local_read_failed"})

    def do_POST(self):
        try:
            self.host()
            require(self.path in {"/api/message", "/api/status", "/api/control", "/api/start"}, "not_found", 404)
            if self.path in {"/api/message", "/api/status"}:
                self.bearer()
                value = self.body(4096 if self.path == "/api/message" else MAX_STATUS)
                result = self.server.store.message(value) if self.path == "/api/message" else self.server.store.status(value)
            else:
                self.browser_auth()
                value = self.body(1024)
                result = self.server.store.control(value) if self.path == "/api/control" else self.server.store.start(value)
            self.reply(200, result)
        except Rejected as exc:
            self.reply(exc.status, {"error": exc.code})
        except (sqlite3.Error, OSError, ValueError, TypeError):
            self.reply(500, {"error": "local_operation_failed"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, store, port=8898):
        super().__init__(("127.0.0.1", port), Handler)
        self.store = store
        self.origin = "http://127.0.0.1:" + str(self.server_port)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8898)
    parser.add_argument("--launcher-config", type=Path)
    args = parser.parse_args()
    store = server = None
    try:
        token = private_file(args.token_file, 512).decode("ascii").strip()
        require(re.fullmatch(r"[A-Za-z0-9._~-]{24,256}", token), "invalid_token_file")
        require(1 <= args.port <= 65535, "invalid_port")
        store = Store(args.state_dir, token, args.launcher_config)
        server = Server(store, args.port)
        print("Live receiver: " + server.origin, flush=True)
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    except (Rejected, OSError, UnicodeError) as exc:
        print(canonical({"error": exc.code if isinstance(exc, Rejected) else "local_setup_failed"}), flush=True)
        return 2
    finally:
        if server:
            server.server_close()
        if store:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
