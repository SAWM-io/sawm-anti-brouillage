#!/usr/bin/env python3
"""Live HTTP resilience demo: two owned virtual paths, one shared SSH tunnel.

Linux root provisions only fresh veth/netns resources. A namespace worker sees
verified receipts and failures, never the impairment schedule. This demonstrates
application resilience, not RF jamming resistance or independent physical paths.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import sqlite3
from socketserver import TCPServer
import stat
import subprocess
import sys
import threading
import time
import uuid

PORT = 18058
TUNNEL_PORT = 18901
COUNT = 100
INTERVAL_MS = 400
BUDGET_SECONDS = 120
MAX_ATTEMPTS = 32
NAMESPACE = re.compile(r"sawm-live-[a-f0-9]{12}-tx\Z")
PROFILES = ("healthy", "degraded_a", "cut_a", "cut_both", "recovery")
TIMELINE = ((0, "healthy"), (5000, "degraded_a"), (12000, "cut_a"),
            (20000, "cut_both"), (25000, "recovery"))
ADDRESSES = {"a": ("198.18.20.1", "198.18.20.2", "198.18.20.0/30"),
             "b": ("198.18.21.1", "198.18.21.2", "198.18.21.0/30")}


class Refused(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def elapsed(start):
    return (time.monotonic_ns() - start) / 1e6


def token_from(path):
    path = Path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
            "token_file_must_be_regular_mode_0600")
    require(info.st_size <= 4096, "token_too_large")
    token = path.read_text(encoding="ascii").strip()
    require(re.fullmatch(r"[A-Za-z0-9_.~-]{16,512}", token) is not None, "invalid_token_format")
    return token


def command(args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=4)
    if check and result.returncode:
        raise Refused("command_failed:" + Path(args[0]).name + ":" + result.stderr[:240])
    return result


def in_namespace(name, *args):
    require(bool(NAMESPACE.fullmatch(name)), "unowned_namespace")
    return ["ip", "netns", "exec", name, *args]


def topology(snapshot):
    if {link["ifname"] for link in snapshot["links"]} != {"lo", "linka", "linkb"}:
        return False
    for key, (source, _, network) in ADDRESSES.items():
        link = next(row for row in snapshot["links"] if row["ifname"] == "link" + key)
        ipv4 = [a.get("local") for a in link.get("addr_info", []) if a.get("family") == "inet"]
        if ipv4 != [source]:
            return False
    routes = snapshot["routes"]
    return len(routes) == 2 and all(any(row.get("dst") == value[2] and
        row.get("dev") == "link" + key and "gateway" not in row for row in routes)
        for key, value in ADDRESSES.items())


def snapshot(name=None):
    prefix = lambda *a: in_namespace(name, *a) if name else list(a)
    return {"links": json.loads(command(prefix("ip", "-j", "address", "show")).stdout),
            "routes": json.loads(command(prefix("ip", "-j", "route", "show", "table", "main")).stdout)}


def network_signature():
    raw = snapshot()
    links = {row["ifname"]: {"mtu": row.get("mtu"), "address": row.get("address"),
        "addresses": sorted((a.get("family"), a.get("local"), a.get("prefixlen"))
                            for a in row.get("addr_info", []))} for row in raw["links"]}
    return json.loads(canonical({"links": links, "routes": raw["routes"]}))


def profile_parameters(profile, path):
    require(profile in PROFILES and path in ADDRESSES, "profile_out_of_scope")
    if profile == "cut_both" or (profile == "cut_a" and path == "a"):
        return (0, 0, 100)
    if profile == "degraded_a" and path == "a":
        return (80, 20, 10)
    return (3 if path == "a" else 15, 0, 0)


class Links:
    def __init__(self):
        suffix = uuid.uuid4().hex[:12]
        self.name = "sawm-live-" + suffix + "-tx"
        self.hosts = {key: "sl" + suffix[:9] + key for key in ADDRESSES}
        self.peers = {key: "sp" + suffix[:9] + key for key in ADDRESSES}
        self.created_namespace = False
        self.created_links = []
        self.log = []

    def call(self, args, check=True):
        result = command(args, check)
        self.log.append({"command": args, "returncode": result.returncode,
                         "at_monotonic_ns": time.monotonic_ns(),
                         "stdout": result.stdout, "stderr": result.stderr})
        return result

    def create(self):
        raw = snapshot()
        occupied = {a.get("local") for link in raw["links"] for a in link.get("addr_info", [])}
        require(not occupied.intersection(a for values in ADDRESSES.values() for a in values[:2]), "lab_address_in_use")
        require(not any(r.get("dst") in {v[2] for v in ADDRESSES.values()} for r in raw["routes"]), "lab_route_in_use")
        names = {row["ifname"] for row in raw["links"]}
        require(not names.intersection(set(self.hosts.values()) | set(self.peers.values())), "interface_collision")
        existing = {line.split()[0] for line in command(["ip", "netns", "list"]).stdout.splitlines() if line.strip()}
        require(self.name not in existing, "namespace_collision")
        # Reserve only names verified absent. A command timeout may happen after
        # creation; cleanup still attempts these newly owned resources.
        self.created_namespace = True
        self.call(["ip", "netns", "add", self.name])
        for key, (source, target, _) in ADDRESSES.items():
            host, peer = self.hosts[key], self.peers[key]
            self.created_links.append(host)
            self.call(["ip", "link", "add", host, "type", "veth", "peer", "name", peer])
            self.call(["ip", "link", "set", peer, "netns", self.name])
            self.call(in_namespace(self.name, "ip", "link", "set", peer, "name", "link" + key))
            self.call(in_namespace(self.name, "ip", "address", "add", source + "/30", "dev", "link" + key))
            self.call(in_namespace(self.name, "ip", "link", "set", "link" + key, "up"))
            self.call(["ip", "address", "add", target + "/30", "dev", host])
            self.call(["ip", "link", "set", host, "up"])
        self.call(in_namespace(self.name, "ip", "link", "set", "lo", "up"))
        current = snapshot(self.name)
        require(topology(current), "namespace_topology_invalid")
        return current

    def qdisc_args(self, profile, path, side):
        require(path in self.hosts and side in ("host", "sender"), "qdisc_target_invalid")
        delay, jitter, loss = profile_parameters(profile, path)
        dev = self.hosts[path] if side == "host" else "link" + path
        args = ["tc", "qdisc", "replace", "dev", dev, "root", "handle", "10:", "netem", "limit", "256"]
        if delay:
            args += ["delay", str(delay) + "ms"]
            if jitter:
                args += [str(jitter) + "ms"]
        args += ["loss", "random", str(loss) + "%"]
        return args if side == "host" else in_namespace(self.name, *args)

    def qdiscs(self):
        return {key + "_" + side: self.call(
            ["tc", "-s", "-j", "qdisc", "show", "dev", self.hosts[key]] if side == "host" else
            in_namespace(self.name, "tc", "-s", "-j", "qdisc", "show", "dev", "link" + key)).stdout
            for key in ADDRESSES for side in ("host", "sender")}

    def apply(self, profile, start):
        event = {"profile": profile, "started_ms": elapsed(start), "before": self.qdiscs()}
        for key in ADDRESSES:
            for side in ("host", "sender"):
                self.call(self.qdisc_args(profile, key, side))
        event.update({"applied_ms": elapsed(start), "after": self.qdiscs()})
        return event

    def close(self):
        ok = True
        # Each cleanup step is attempted independently, including partial setup.
        if self.created_namespace:
            try:
                ok = self.call(["ip", "netns", "delete", self.name], False).returncode == 0 and ok
            except Exception:
                ok = False
        for host in self.created_links:
            try:
                present = self.call(["ip", "link", "show", "dev", host], False)
                if present.returncode == 0:
                    removed = self.call(["ip", "link", "delete", host], False)
                    if removed.returncode != 0:
                        # Namespace destruction also removes its veth peers.
                        # Confirm absence after that race; other failures stay failures.
                        remaining = self.call(["ip", "-j", "link", "show"])
                        ok = host not in {row["ifname"] for row in json.loads(remaining.stdout)} and ok
            except Exception:
                ok = False
        return ok


class Selector:
    """Observation-only selection; no profile or schedule is an input."""
    def __init__(self):
        self.active = "a"
        self.paths = {k: {"srtt_ms": None, "rttvar_ms": None, "timeout_ms": 500.0,
            "failures": 0, "successes": 0, "consecutive_failures": 0,
            "last_attempt_ms": None, "last_success_ms": None} for k in ADDRESSES}

    def score(self, path):
        row = self.paths[path]
        prior = 6 if path == "a" else 30
        return (row["srtt_ms"] if row["srtt_ms"] is not None else prior) + min(1000, 180 * row["consecutive_failures"])

    def choose(self, now):
        if self.paths[self.active]["last_attempt_ms"] is None:
            return self.active, "initial_path_prior"
        other = "b" if self.active == "a" else "a"
        row = self.paths[other]
        # Probe a neglected path periodically; an explicit failed probe does not
        # authorize assuming recovery or replacing the working active path.
        if row["last_attempt_ms"] is None or now - row["last_attempt_ms"] >= 2500:
            return other, "periodic_recovery_probe"
        best = min(ADDRESSES, key=lambda path: (self.score(path), path))
        if best != self.active and self.score(best) + 10 < self.score(self.active):
            self.active = best
            return best, "lower_observed_cost_with_hysteresis"
        return self.active, "current_observed_path"

    def observe(self, path, duration, valid_ack, timeout, now):
        row = self.paths[path]
        row["last_attempt_ms"] = now
        if valid_ack:
            previous = row["srtt_ms"]
            row["rttvar_ms"] = duration / 2 if previous is None else .75 * row["rttvar_ms"] + .25 * abs(previous - duration)
            row["srtt_ms"] = duration if previous is None else .875 * previous + .125 * duration
            row["timeout_ms"] = min(500.0, max(80.0, row["srtt_ms"] + 4 * row["rttvar_ms"] + 25))
            row["successes"] += 1; row["consecutive_failures"] = 0; row["last_success_ms"] = now
            if path != self.active and self.score(path) + 10 < self.score(self.active):
                self.active = path
        else:
            row["failures"] += 1; row["consecutive_failures"] += 1
            if timeout:
                row["timeout_ms"] = min(500.0, max(80.0, row["timeout_ms"] * 2))
            other = "b" if path == "a" else "a"
            if path == self.active and self.score(other) + 10 < self.score(path):
                self.active = other


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=2000")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(path, run_id, start):
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE messages (message_id TEXT PRIMARY KEY, body TEXT NOT NULL,
          scheduled_ms REAL NOT NULL, enqueued_ms REAL NOT NULL, ack_ms REAL,
          attempts INTEGER NOT NULL DEFAULT 0, next_ms REAL NOT NULL DEFAULT 0);
        CREATE TABLE attempts (id INTEGER PRIMARY KEY, message_id TEXT NOT NULL,
          path TEXT NOT NULL, started_ms REAL NOT NULL, ended_ms REAL NOT NULL,
          outcome TEXT NOT NULL, receipt TEXT, selection_reason TEXT NOT NULL);
        CREATE TABLE events (id INTEGER PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        for key, value in {"run_id": run_id, "start_ns": start, "generated_done": False}.items():
            db.execute("INSERT INTO state VALUES (?,?)", (key, canonical(value)))


def state_put(db, key, value):
    db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, canonical(value)))


def state_get(db, key, default=None):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def event_put(db, event):
    db.execute("INSERT INTO events(body) VALUES (?)", (canonical(event),))


def enqueue(path, run_id, number, start):
    payload = "SAWM live resilience message %03d / fictitious demonstration" % number
    body = {"run_id": run_id, "message_id": "%s-%03d" % (run_id, number), "payload": payload,
            "sha256": hashlib.sha256(payload.encode()).hexdigest()}
    with connect(path) as db:
        require(db.execute("SELECT count(*) FROM messages").fetchone()[0] < 300, "queue_capacity")
        db.execute("INSERT INTO messages(message_id,body,scheduled_ms,enqueued_ms) VALUES (?,?,?,?)",
                   (body["message_id"], canonical(body), (number - 1) * INTERVAL_MS, elapsed(start)))
    return body


def verified_receipt(body, receipt):
    return (type(receipt) is dict and receipt.get("committed") is True and
        type(receipt.get("duplicate")) is bool and all(receipt.get(k) == body[k] for k in ("run_id", "message_id", "sha256")))


def http_json(host, port, path, body, token, timeout, limit=4096):
    encoded = body if isinstance(body, bytes) else canonical(body).encode()
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("POST", path, body=encoded,
                           headers={"Content-Type": "application/json", "Authorization": "Bearer " + token,
                                    **({"Host": "127.0.0.1:8898"} if port == TUNNEL_PORT else {})})
        response = connection.getresponse()
        raw = response.read(limit + 1)
        require(len(raw) <= limit, "response_too_large")
        require(response.status == 200, "http_status_" + str(response.status))
        return json.loads(raw)
    finally:
        connection.close()


def worker(queue, namespace, token_file):
    require(bool(NAMESPACE.fullmatch(namespace)), "unowned_namespace")
    require(os.stat('/proc/self/ns/net').st_ino == os.stat('/var/run/netns/' + namespace).st_ino, "wrong_namespace")
    require(topology(snapshot()), "worker_topology_invalid")
    token = token_from(token_file)
    controller = Selector()
    with connect(queue) as db:
        start = state_get(db, "start_ns")
        state_put(db, "controller", {"active_path": controller.active, "paths": controller.paths})
    while elapsed(start) < BUDGET_SECONDS * 1000:
        now = elapsed(start)
        with connect(queue) as db:
            row = db.execute("SELECT * FROM messages WHERE ack_ms IS NULL AND attempts<? AND next_ms<=? ORDER BY attempts,enqueued_ms LIMIT 1", (MAX_ATTEMPTS, now)).fetchone()
            remaining = db.execute("SELECT count(*) FROM messages WHERE ack_ms IS NULL").fetchone()[0]
            done = state_get(db, "generated_done", False)
        if row is None:
            if done and remaining == 0:
                return 0
            time.sleep(.01); continue
        path, reason = controller.choose(now)
        body = json.loads(row["body"])
        receipt = None
        outcome = "invalid_ack"
        began = elapsed(start)
        try:
            receipt = http_json(ADDRESSES[path][1], PORT, "/message", body, token,
                                controller.paths[path]["timeout_ms"] / 1000)
            if verified_receipt(body, receipt):
                outcome = "acked"
        except TimeoutError:
            outcome = "timeout"
        except Exception as exc:
            outcome = "transport_error:" + type(exc).__name__
        ended = elapsed(start)
        controller.observe(path, ended - began, outcome == "acked", outcome == "timeout", ended)
        with connect(queue) as db:
            db.execute("INSERT INTO attempts(message_id,path,started_ms,ended_ms,outcome,receipt,selection_reason) VALUES (?,?,?,?,?,?,?)",
                (body["message_id"], path, began, ended, outcome, canonical(receipt) if receipt else None, reason))
            db.execute("UPDATE messages SET attempts=attempts+1,next_ms=?,ack_ms=? WHERE message_id=?",
                (ended + (min(800, 80 * 2 ** min(row["attempts"], 3)) if outcome != "acked" else 0),
                 ended if outcome == "acked" else None, body["message_id"]))
            state_put(db, "controller", {"active_path": controller.active, "paths": controller.paths})
            event_put(db, {"at_ms": ended, "type": outcome, "path": path, "reason": reason,
                           "message_id": body["message_id"]})
    return 2


class Proxy(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        # Numeric lab addresses have no DNS record. HTTPServer.getfqdn would
        # consume the scenario clock while resolving them before traffic starts.
        TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            require(self.path == "/message", "route")
            require(self.client_address[0] == self.server.source, "source")
            require(hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.server.token), "authentication")
            require(not self.headers.get("Transfer-Encoding"), "chunked_not_supported")
            size = int(self.headers.get("Content-Length", "0"))
            require(0 < size <= 4096, "body_size")
            self.connection.settimeout(1)
            body = self.rfile.read(size)
            require(len(body) == size, "short_body")
            parsed = json.loads(body)
            require(type(parsed) is dict and parsed.get("run_id") == self.server.run_id, "run_id")
            connection = http.client.HTTPConnection("127.0.0.1", TUNNEL_PORT, timeout=1)
            try:
                connection.request("POST", "/api/message", body=body,
                    headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.server.token,
                             "Host": "127.0.0.1:8898"})
                response = connection.getresponse()
                result = response.read(4097)
                require(len(result) <= 4096, "response_size")
                code = response.status
            finally:
                connection.close()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result))); self.end_headers(); self.wfile.write(result)
        except Exception:
            try:
                result = b'{"error":"live_proxy_refused_or_unavailable"}'
                self.send_response(502); self.send_header("Content-Length", str(len(result)))
                self.end_headers(); self.wfile.write(result)
            except Exception:
                pass


def current_status(queue, run_id, phase, start):
    with connect(queue) as db:
        counts = db.execute("SELECT count(*),sum(ack_ms IS NOT NULL),sum(attempts) FROM messages").fetchone()
        state = state_get(db, "controller", {"active_path": "a", "paths": Selector().paths})
        timeouts = db.execute("SELECT count(*) FROM attempts WHERE outcome='timeout'").fetchone()[0]
        events = [json.loads(r[0]) for r in db.execute("SELECT body FROM events ORDER BY id DESC LIMIT 30")][::-1]
    generated, acked, attempts = (v or 0 for v in counts)
    return {"run_id": run_id, "phase": phase, "elapsed_ms": elapsed(start), "generated": generated,
            "acked": acked, "pending": generated - acked, "attempts": attempts, "timeouts": timeouts,
            **state, "events": events, "scope": "two_virtual_paths_shared_ssh_tunnel_no_radio"}


def analyze_phases(attempts, phases, finished_ms):
    """Keep transition/boundary ACKs; never infer cross-host delivery timestamps."""
    acknowledgements = [row for row in attempts if row["outcome"] == "acked"]
    result = []
    for index, phase in enumerate(phases):
        start = phase["applied_ms"]
        end = phases[index + 1]["started_ms"] if index + 1 < len(phases) else finished_ms
        guard = 200 if phase["profile"] == "cut_both" else 0
        in_phase = [r for r in acknowledgements if start <= r["ended_ms"] < end]
        stable = [r for r in in_phase if r["ended_ms"] >= start + guard]
        first_after = min((r["ended_ms"] for r in acknowledgements if r["ended_ms"] >= start), default=None)
        result.append({"profile": phase["profile"], "applied_ms": start, "until_ms": end,
            "ack_count": len(in_phase), "stable_after_guard_ms": guard,
            "stable_ack_count": len(stable), "boundary_acks": len(in_phase) - len(stable),
            "path_b_acks_attempted_during_phase": sum(r["path"] == "b" and r["started_ms"] >= start for r in in_phase),
            "first_ack_after_recovery_ms": first_after - start if phase["profile"] == "recovery" and first_after is not None else None})
    return result


def valid_command(value):
    return (type(value) is dict and type(value.get("command_version")) is int and value["command_version"] >= 0
            and value.get("profile") in PROFILES and value.get("mode") in ("manual", "scripted"))


def stop_process(process):
    if process is None:
        return True
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=2)
    return process.poll() is not None


def cleanup(process, proxies, links):
    results = {}
    try:
        results["worker_stopped"] = stop_process(process)
    except Exception as exc:
        results["worker_stopped"] = False
    for i, (server, thread) in enumerate(proxies):
        ok = True
        for operation in (server.shutdown, server.server_close, lambda: thread.join(timeout=2)):
            try:
                operation()
            except Exception:
                ok = False
        results["proxy_%s_stopped" % i] = not thread.is_alive() and ok
    try:
        results["network_removed"] = links.close()
    except Exception:
        results["network_removed"] = False
    return results


def run(output, token_file, scripted=False):
    require(platform.system() == "Linux" and os.geteuid() == 0, "linux_root_required")
    require(all(shutil.which(name) for name in ("ip", "tc")), "iproute2_required")
    token = token_from(token_file)
    token_file = Path(token_file).resolve()
    output = Path(output).absolute()
    require(not output.exists() and not output.is_symlink(), "new_output_directory_required")
    output.mkdir(parents=True, mode=0o700)
    run_id = "live-" + uuid.uuid4().hex
    start = time.monotonic_ns()
    queue = output / "sender.sqlite"
    initialize(queue, run_id, start)
    before = network_signature()
    report = {"schema": "sawm-live-vm/v1", "run_id": run_id,
              "scope": {"radio": False, "physical_path_independence": False,
                        "shared_tunnel": "VM loopback:18901 to Mac receiver", "core_delivery_modified": False},
              "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "started_at_ms": time.time_ns() // 1_000_000,
              "phase_events": [], "command_events": [], "control_errors": [], "checks": {}}
    links, proxies, process = Links(), [], None
    profile, mode, version = "healthy", "scripted" if scripted else "manual", 0
    count, next_status = 0, 0.0
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        def interrupted(signum, frame):
            raise InterruptedError("live_run_interrupted")
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, interrupted)
    worker_log = None
    try:
        report["topology"] = links.create()
        for key, (source, destination, _) in ADDRESSES.items():
            server = Proxy((destination, PORT), ProxyHandler)
            server.source, server.token, server.run_id = source, token, run_id
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
            thread.start(); proxies.append((server, thread))
        report["setup_elapsed_ms"] = elapsed(start)
        start = time.monotonic_ns()
        with connect(queue) as db:
            state_put(db, "start_ns", start)
        report["phase_events"].append(links.apply(profile, start))
        worker_log = (output / "worker.log").open("w")
        process = subprocess.Popen(in_namespace(links.name, sys.executable, str(Path(__file__).resolve()),
            "--worker", "--namespace", links.name, "--queue", str(queue), "--token-file", str(token_file)),
            stdout=worker_log, stderr=subprocess.STDOUT)
        while elapsed(start) < BUDGET_SECONDS * 1000:
            now = elapsed(start)
            while count < COUNT and now >= count * INTERVAL_MS:
                count += 1; enqueue(queue, run_id, count, start)
            if count == COUNT:
                with connect(queue) as db:
                    state_put(db, "generated_done", True)
            desired = profile
            if mode == "scripted":
                desired = next(p for t, p in reversed(TIMELINE) if now >= t)
            if desired != profile:
                report["phase_events"].append(links.apply(desired, start)); profile = desired
                with connect(queue) as db:
                    event_put(db, {"at_ms": elapsed(start), "type": "profile_applied", "reason": profile})
                save(output / "phase-events.json", report["phase_events"])
            if now >= next_status:
                status = current_status(queue, run_id, profile, start)
                status["mode"] = mode
                save(output / "status.json", status)
                try:
                    received = http_json("127.0.0.1", TUNNEL_PORT, "/api/status", status, token, .4)
                    require(valid_command(received), "invalid_dashboard_command")
                    if received["command_version"] > version:
                        version = received["command_version"]
                        mode = received["mode"]
                        report["command_events"].append({"at_ms": elapsed(start), **received})
                        if mode == "manual" and received["profile"] != profile:
                            profile = received["profile"]
                            report["phase_events"].append(links.apply(profile, start))
                            save(output / "phase-events.json", report["phase_events"])
                            with connect(queue) as db:
                                event_put(db, {"at_ms": elapsed(start), "type": "profile_applied", "reason": profile})
                except Exception as exc:
                    report["control_errors"].append({"at_ms": elapsed(start), "error": type(exc).__name__})
                next_status = elapsed(start) + 500
            if process.poll() is not None:
                break
            time.sleep(.01)
        report["worker_exit_code"] = process.poll()
        report["qdisc_final"] = links.qdiscs()
        report["final_status"] = current_status(queue, run_id, profile, start)
        report["checks"]["all_messages_acked"] = report["final_status"]["acked"] == COUNT
        with connect(queue) as db:
            messages = [dict(r) for r in db.execute("SELECT * FROM messages ORDER BY enqueued_ms")]
            attempts = [dict(r) for r in db.execute("SELECT * FROM attempts ORDER BY id")]
        save(output / "messages.json", messages); save(output / "attempts.json", attempts)
        report["phase_metrics"] = analyze_phases(attempts, report["phase_events"], elapsed(start))
        bodies = {r["message_id"]: json.loads(r["body"]) for r in messages}
        report["checks"]["all_acks_correlated"] = all(verified_receipt(bodies[r["message_id"]], json.loads(r["receipt"])) for r in attempts if r["outcome"] == "acked")
        report["checks"]["attempt_budgets"] = all(r["attempts"] <= MAX_ATTEMPTS for r in messages)
        report["scripted_scenario_unmodified"] = scripted and not any(
            item["mode"] == "manual" for item in report["command_events"])
        if report["scripted_scenario_unmodified"]:
            metrics = {row["profile"]: row for row in report["phase_metrics"]}
            report["checks"]["all_scripted_phases_exercised"] = [r["profile"] for r in report["phase_metrics"]] == [p for _, p in TIMELINE]
            report["checks"]["nominal_delivery"] = metrics.get("healthy", {}).get("ack_count", 0) > 0
            report["checks"]["degraded_delivery"] = metrics.get("degraded_a", {}).get("ack_count", 0) > 0
            report["checks"]["fallback_delivery"] = metrics.get("cut_a", {}).get("path_b_acks_attempted_during_phase", 0) > 0
            report["checks"]["total_cut_no_stable_ack"] = metrics.get("cut_both", {}).get("stable_ack_count") == 0
            report["checks"]["recovery_delivery"] = metrics.get("recovery", {}).get("ack_count", 0) > 0
    except Exception as exc:
        report["error"] = type(exc).__name__ + ":" + str(exc)[:300]
    finally:
        for signum in old_handlers:
            signal.signal(signum, signal.SIG_IGN)
        # A terminal status is emitted before withdrawing the network resources.
        # Sender RTT uses only the VM clock; no VM/Mac timestamp subtraction.
        try:
            report["final_status"] = current_status(queue, run_id, profile, start)
            terminal = dict(report["final_status"], phase="finished" if "error" not in report else "error",
                            completed=True, mode=mode)
            http_json("127.0.0.1", TUNNEL_PORT, "/api/status", terminal, token, .4)
        except Exception:
            pass
        report["cleanup"] = cleanup(process, proxies, links)
        if worker_log:
            worker_log.close()
        try:
            report["checks"]["primary_network_preserved"] = network_signature() == before
        except Exception:
            report["checks"]["primary_network_preserved"] = False
        report["checks"]["cleanup"] = all(report["cleanup"].values())
        report["elapsed_ms"] = elapsed(start)
        report["checks"]["source_unchanged"] = report["source_sha256"] == hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        report["success"] = "error" not in report and report.get("worker_exit_code") == 0 and bool(report["checks"]) and all(report["checks"].values())
        report["finished_at_ms"] = time.time_ns() // 1_000_000
        save(output / "setup-and-cleanup.json", links.log)
        save(output / "phase-events.json", report["phase_events"])
        save(output / "report.json", report)
        if "final_status" in report:
            final = dict(report["final_status"], phase="finished" if report["success"] else "error",
                         completed=True, success=report["success"], mode=mode)
            try:
                http_json("127.0.0.1", TUNNEL_PORT, "/api/status", final, token, .4)
            except Exception:
                pass
        for signum, old in old_handlers.items():
            signal.signal(signum, old)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--scripted", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--namespace", help=argparse.SUPPRESS)
    parser.add_argument("--queue", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.worker:
            require(args.namespace and args.queue, "worker_arguments_required")
            return worker(args.queue, args.namespace, args.token_file)
        require(args.output_dir is not None, "output_dir_required")
        report = run(args.output_dir, args.token_file, args.scripted)
        print(canonical({"run_id": report["run_id"], "success": report["success"],
                         "output_dir": str(args.output_dir), "checks": report["checks"]}))
        return 0 if report["success"] else 2
    except Exception as exc:
        print(canonical({"error": type(exc).__name__ + ":" + str(exc)[:300]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
