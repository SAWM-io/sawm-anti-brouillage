#!/usr/bin/env python3
"""Isolated Linux packet impairment benchmark. No RF, external link or Cloud calls.

Run as root inside a disposable/owned Linux VM:
  python3 vm_netem_lab.py --output-dir NEW_DIR --repetitions 3 [--policy FILE]
Only newly owned network namespaces and their veth pair are modified. The
unchanged delivery implementation is bootstrapped with one fixed lab address;
its public CLI remains loopback-only. No default route is added to either side.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

import delivery

DESTINATION = "198.18.0.2"
SOURCE = "198.18.0.1"
PORT = 18057
DEVICE = "link0"
NAMESPACE = re.compile(r"sawm-nem-[a-f0-9]{12}-(?:tx|rx)\Z")
PHASES = (
    {"name": "healthy", "at_ms": 0, "delay_ms": 1, "jitter_ms": 0, "loss_percent": 0},
    {"name": "degraded", "at_ms": 1000, "delay_ms": 60, "jitter_ms": 20, "loss_percent": 3},
    {"name": "blackout", "at_ms": 3000, "delay_ms": 0, "jitter_ms": 0, "loss_percent": 100},
    {"name": "recovery", "at_ms": 4000, "delay_ms": 1, "jitter_ms": 0, "loss_percent": 0},
)
ARRIVAL_INTERVAL_MS = 350
TRIAL_CAP_SECONDS = 25
CAMPAIGN_CAP_SECONDS = 180
SOURCES = ("vm_netem_lab.py", "delivery.py", "adaptive_policy.py", "witness.py")


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_hashes():
    return {name: sha(Path(__file__).with_name(name)) for name in SOURCES}


def refuse(condition, reason):
    if not condition:
        raise delivery.Refused(reason)


def command(args, *, check=True):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=4)
    if check and result.returncode:
        raise delivery.Refused("lab_command_failed:" + Path(args[0]).name + ":" + result.stderr[:350].strip())
    return result


def require_namespace(name):
    refuse(type(name) is str and NAMESPACE.fullmatch(name), "namespace_out_of_scope")
    return name


def namespace_command(name, *args):
    return ["ip", "netns", "exec", require_namespace(name), *args]


def qdisc_command(name, phase):
    """Only the four predefined profiles may affect the owned link0 device."""
    refuse(phase in PHASES, "profile_out_of_scope")
    args = ["tc", "qdisc", "replace", "dev", DEVICE, "root", "handle", "10:", "netem", "limit", "256"]
    if phase["delay_ms"]:
        args += ["delay", str(phase["delay_ms"]) + "ms"]
        if phase["jitter_ms"]:
            args += [str(phase["jitter_ms"]) + "ms"]
    args += ["loss", "random", str(phase["loss_percent"]) + "%"]
    return namespace_command(name, *args)


def namespace_snapshot(name):
    require_namespace(name)
    return {"links": json.loads(command(namespace_command(name, "ip", "-j", "address", "show")).stdout),
            "routes": json.loads(command(namespace_command(name, "ip", "-j", "route", "show", "table", "main")).stdout)}


def isolated_topology(snapshot, role):
    expected = SOURCE if role == "tx" else DESTINATION
    links = snapshot["links"]
    if {row["ifname"] for row in links} != {"lo", DEVICE}:
        return False
    addresses = [entry["local"] for row in links if row["ifname"] == DEVICE
                 for entry in row.get("addr_info", []) if entry.get("family") == "inet"]
    routes = snapshot["routes"]
    return addresses == [expected] and len(routes) == 1 and routes[0].get("dst") == "198.18.0.0/30" and routes[0].get("dev") == DEVICE and "gateway" not in routes[0]


def host_network_signature():
    links = json.loads(command(["ip", "-j", "address", "show"]).stdout)
    result = {row["ifname"]: {"mtu": row.get("mtu"), "address": row.get("address"),
               "inet": sorted((entry.get("family"), entry.get("local"), entry.get("prefixlen"))
                              for entry in row.get("addr_info", []))} for row in links}
    return {"interfaces": result, "routes": json.loads(command(["ip", "-j", "route", "show", "table", "main"]).stdout)}


class Link:
    def __init__(self):
        token = uuid.uuid4().hex[:12]
        self.names = {role: "sawm-nem-" + token + "-" + role for role in ("tx", "rx")}
        self.veth = ("sv" + token[:9] + "t", "sv" + token[:9] + "r")
        self.created = []
        self.root_link_created = False
        self.log = []

    def call(self, args, check=True):
        result = command(args, check=check)
        self.log.append({"command": args, "returncode": result.returncode, "at_monotonic_ns": time.monotonic_ns(),
                         "stdout": result.stdout, "stderr": result.stderr})
        return result

    def create(self):
        existing = {line.split()[0] for line in command(["ip", "netns", "list"]).stdout.splitlines() if line.strip()}
        refuse(not existing.intersection(self.names.values()), "namespace_collision")
        for name in self.names.values():
            self.call(["ip", "netns", "add", name]); self.created.append(name)
        self.call(["ip", "link", "add", self.veth[0], "type", "veth", "peer", "name", self.veth[1]])
        self.root_link_created = True
        for role, address, interface in zip(("tx", "rx"), (SOURCE, DESTINATION), self.veth):
            name = self.names[role]
            self.call(["ip", "link", "set", interface, "netns", name])
            self.call(namespace_command(name, "ip", "link", "set", interface, "name", DEVICE))
            self.call(namespace_command(name, "ip", "address", "add", address + "/30", "dev", DEVICE))
            self.call(namespace_command(name, "ip", "link", "set", "lo", "up"))
            self.call(namespace_command(name, "ip", "link", "set", DEVICE, "up"))
        snapshots = {role: namespace_snapshot(name) for role, name in self.names.items()}
        refuse(all(isolated_topology(snapshots[role], role) for role in snapshots), "namespace_topology_invalid")
        return snapshots

    def capture_qdiscs(self):
        return {role: self.call(namespace_command(name, "tc", "-s", "-j", "qdisc", "show", "dev", DEVICE)).stdout
                for role, name in self.names.items()}

    def apply(self, phase):
        event = {"profile": phase, "started_monotonic_ns": time.monotonic_ns(), "sides": {}}
        for role, name in self.names.items():
            self.call(qdisc_command(name, phase))
            event["sides"][role] = {"applied_monotonic_ns": time.monotonic_ns()}
        event["completed_monotonic_ns"] = time.monotonic_ns()
        event["qdisc_after"] = self.capture_qdiscs()
        return event

    def close(self):
        # Keep attempting every owned resource even if one operation times out.
        success = True
        def attempt(args):
            nonlocal success
            try:
                result = self.call(args, check=False)
                return result
            except Exception as exc:
                success = False
                self.log.append({"command": args, "cleanup_error": type(exc).__name__,
                                 "at_monotonic_ns": time.monotonic_ns()})
                return None
        for name in reversed(self.created):
            result = attempt(["ip", "netns", "delete", name])
            if result is None or result.returncode:
                success = False
        if self.root_link_created:
            for interface in self.veth:
                result = attempt(["ip", "link", "show", "dev", interface])
                if result is not None and result.returncode == 0:
                    deleted = attempt(["ip", "link", "delete", interface])
                    success = deleted is not None and deleted.returncode == 0 and success
        result = attempt(["ip", "netns", "list"])
        if result is None or result.returncode:
            return False
        existing = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
        return success and not existing.intersection(self.created)


@contextlib.contextmanager
def lab_destination():
    """Scoped transport bootstrap for this lab; public delivery HOST is restored."""
    original = delivery.HOST
    delivery.HOST = DESTINATION
    try:
        yield
    finally:
        delivery.HOST = original


def verify_child_namespace(name, role):
    require_namespace(name)
    refuse(name.endswith("-" + role), "wrong_namespace_role")
    refuse(os.stat("/proc/self/ns/net").st_ino == os.stat("/var/run/netns/" + name).st_ino,
           "process_not_in_expected_namespace")
    # Query the current namespace, without a nested exec or use of host routing.
    snapshot = {"links": json.loads(command(["ip", "-j", "address", "show"]).stdout),
                "routes": json.loads(command(["ip", "-j", "route", "show", "table", "main"]).stdout)}
    refuse(isolated_topology(snapshot, role), "child_topology_invalid")


def child(role, namespace, path, ready=None):
    verify_child_namespace(namespace, "tx" if role == "worker" else "rx")
    with lab_destination():
        if role == "worker":
            result = delivery.work(path)
            print(delivery.canonical(result), flush=True)
            return 0 if result["outcome"] == "all_acknowledged" else 2
        import witness
        server = witness.Server((DESTINATION, PORT), witness.Handler)
        server.ledger, server.lose_first_ack = Path(path), False
        class Stopped(Exception): pass
        def stop(signum, frame): raise Stopped()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        save(ready, {"ready": True, "host": DESTINATION, "port": PORT, "pid": os.getpid()})
        try:
            server.serve_forever(poll_interval=.02)
        except Stopped:
            pass
        finally:
            server.server_close()
        return 0


def messages_for(repetition):
    refuse(type(repetition) is int and 1 <= repetition <= 3, "repetition_invalid")
    run_id = hashlib.sha256(("vm-netem-20261027-" + str(repetition)).encode()).hexdigest()[:32]
    return [{"schema": delivery.MESSAGE_SCHEMA, "message_id": f"demo-{run_id}-{i:04}", "sequence": i,
             "value": i * 7, "created_at_ms": 1700000000000 + (i - 1) * ARRIVAL_INTERVAL_MS,
             "created_monotonic_ns": (i - 1) * ARRIVAL_INTERVAL_MS * 1_000_000} for i in range(1, 21)]


def percentile(values, quantile=.95):
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)] if values else None


def phase_at(timestamp, phases):
    current = "setup"
    for phase in phases:
        if timestamp < phase["started_monotonic_ns"]:
            break
        if timestamp < phase["completed_monotonic_ns"]:
            return "transition"
        current = phase["profile"]["name"]
    return current


def analyze(queue, ledger, arrivals, phases, planned):
    with lab_destination(), contextlib.closing(delivery.connect_db(queue)) as db:
        metadata = delivery.metadata_for(db)
        rows = [dict(row) for row in db.execute("SELECT * FROM messages ORDER BY rowid")]
        attempts = [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY id")]
        summary = delivery.queue_summary(queue)
    with contextlib.closing(delivery.connect_db(ledger)) as db:
        recipient = [dict(row) for row in db.execute("SELECT * FROM deliveries WHERE channel='durable' ORDER BY sequence")]
        conflicts = db.execute("SELECT count(*) FROM conflicts").fetchone()[0]
    source = {message["message_id"]: delivery.digest(message) for message in planned}
    enqueue_times = {row["message_id"]: row["enqueue_started_monotonic_ns"] for row in arrivals}
    received = {row["message_id"] for row in recipient}
    verified = [row for row in attempts if row["result"] == "ack_verified"]
    acks = {row["message_id"] for row in verified}
    delivery_ms = [(row["accepted_monotonic_ns"] - enqueue_times[row["message_id"]]) / 1e6 for row in recipient]
    ack_ms = [(row["finished_monotonic_ns"] - enqueue_times[row["message_id"]]) / 1e6 for row in verified]
    buckets = {name: {"enqueued": 0, "committed": 0, "acked": 0} for name in ("setup", "healthy", "degraded", "blackout", "recovery", "transition")}
    for row in arrivals: buckets[phase_at(row["enqueue_started_monotonic_ns"], phases)]["enqueued"] += 1
    for row in recipient: buckets[phase_at(row["accepted_monotonic_ns"], phases)]["committed"] += 1
    for row in verified: buckets[phase_at(row["finished_monotonic_ns"], phases)]["acked"] += 1
    blackout, recovery = next((p for p in phases if p["profile"]["name"] == "blackout"), None), next((p for p in phases if p["profile"]["name"] == "recovery"), None)
    window = None
    if blackout and recovery:
        start, end = blackout["completed_monotonic_ns"] + 200_000_000, recovery["started_monotonic_ns"]
        window = {"start_monotonic_ns": start, "end_monotonic_ns": end, "guard_ms": 200,
                  "committed": sum(start <= row["accepted_monotonic_ns"] < end for row in recipient),
                  "acked": sum(start <= row["finished_monotonic_ns"] < end for row in verified)}
    recovered_acks = [row["finished_monotonic_ns"] for row in verified if recovery and row["finished_monotonic_ns"] >= recovery["completed_monotonic_ns"]]
    first_recovery_ack = (min(recovered_acks) - recovery["completed_monotonic_ns"]) / 1e6 if recovered_acks else None
    metrics = {"planned": len(planned), "enqueued": len(arrivals), "delivered_unique": len(received), "acked": len(acks),
               "not_delivered": len(planned) - len(received), "unacknowledged": len(planned) - len(acks),
               "states": summary["states"], "attempts": len(attempts), "duplicate_requests": sum(row["requests"] - 1 for row in recipient),
               "duplicate_effects": len(recipient) - len(received), "content_conflicts": conflicts,
               "timeouts": sum(row["error"] == "timeout" for row in attempts),
               "delivery_p95_ms": percentile(delivery_ms), "ack_p95_ms": percentile(ack_ms),
               "delivery_p50_ms": percentile(delivery_ms,.5), "ack_p50_ms": percentile(ack_ms,.5),
               "all_messages_delivered_and_acked": len(received) == len(acks) == len(planned),
               "phase_counts": buckets, "blackout_stabilized_window": window,
               "first_ack_after_recovery_ms": first_recovery_ack}
    checks = {"all_enqueued_match_plan": {row["message_id"]: row["digest"] for row in rows} == source,
              "received_content_matches_source": all(source.get(row["message_id"]) == row["digest"] for row in recipient),
              "ack_has_committed_effect": acks.issubset(received), "unique_effects": metrics["duplicate_effects"] == 0,
              "no_content_conflict": conflicts == 0,
              "attempt_counts_match": sum(row["attempts"] for row in rows) == len(attempts),
              "attempt_budget_respected": all(row["attempts"] <= metadata["policy"]["max_attempts"] for row in rows),
              "worker_known_outcome": bool(summary["worker_runs"]) and summary["worker_runs"][-1]["outcome"] in {"all_acknowledged", "budget_exhausted", "attempts_exhausted"},
              "latency_nonnegative": all(value >= 0 for value in delivery_ms + ack_ms)}
    trace = summary.get("adaptive", {}).get("calibration_trace", [])
    if "adaptive" in summary:
        checks["calibration_for_every_completed_attempt"] = len(trace) == sum(row["finished_monotonic_ns"] is not None for row in attempts)
        checks["adaptive_bounds_preserved"] = summary["adaptive"]["state"]["limits"]["policy"] == metadata["policy"]
    return {"metadata": metadata, "messages": rows, "attempts": attempts, "summary": summary,
            "recipient": recipient, "metrics": metrics, "checks": checks}


def stop_owned(process):
    if process is None: return True
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=2)
    return process.poll() is not None


def cleanup_owned(worker, server, link):
    """Every cleanup is attempted even if another child or command fails."""
    results = {}
    for label, process in (("worker", worker), ("witness", server)):
        try:
            results[label] = {"stopped": stop_owned(process)}
        except Exception as exc:
            results[label] = {"stopped": False, "error": type(exc).__name__}
    try:
        results["link"] = {"stopped": link.close()}
    except Exception as exc:
        results["link"] = {"stopped": False, "error": type(exc).__name__}
    return results


def run_trial(output, mode, repetition, policy, campaign_deadline):
    refuse(mode in {"static", "adaptive"}, "mode_invalid")
    output = Path(output); output.mkdir(mode=0o700)
    begun = time.monotonic(); deadline = min(campaign_deadline, begun + TRIAL_CAP_SECONDS - 3)
    report = {"schema": "sawm-vm-netem-trial/v1", "mode": mode, "repetition": repetition, "success": False,
              "started_at_ms": time.time_ns() // 1_000_000, "initial_policy": policy, "source_sha256": source_hashes(), "checks": {}}
    link, worker, server = Link(), None, None
    logs, arrivals, phases = [], [], []
    queue, ledger = output / "queue.sqlite", output / "witness.sqlite"
    planned = messages_for(repetition)
    save(output / "messages.json", planned)
    initial_network = host_network_signature()
    report["host_network_before"] = initial_network
    origin_ns = None
    try:
        import witness
        witness.initialize(ledger)
        report["namespace_topology"] = link.create()
        report["checks"]["namespaces_isolated"] = all(isolated_topology(report["namespace_topology"][role],role) for role in ("tx","rx"))
        with lab_destination():
            metadata = delivery.create_queue(queue, PORT, 20, policy, adaptive=mode == "adaptive")
        report["initial_queue_metadata"] = metadata
        for role, path in (("witness", ledger),):
            log = (output / "witness.log").open("x"); logs.append(log)
            server = subprocess.Popen(namespace_command(link.names["rx"], sys.executable, str(Path(__file__).absolute()),
                                      "--internal-role", role, "--namespace", link.names["rx"], "--path", str(path), "--ready", str(output / "ready.json")),
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        while not (output / "ready.json").is_file():
            refuse(server.poll() is None, "witness_exited_before_ready")
            refuse(time.monotonic() < min(deadline, begun + 5), "witness_ready_timeout")
            time.sleep(.01)
        phases.append(link.apply(PHASES[0]))
        origin_ns = time.monotonic_ns(); report["origin_monotonic_ns"] = origin_ns
        log = (output / "worker.log").open("x"); logs.append(log)
        worker = subprocess.Popen(namespace_command(link.names["tx"], sys.executable, str(Path(__file__).absolute()),
                                  "--internal-role", "worker", "--namespace", link.names["tx"], "--path", str(queue)),
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        actions = [(index * ARRIVAL_INTERVAL_MS, "enqueue", message) for index,message in enumerate(planned)]
        actions += [(p["at_ms"], "phase", p) for p in PHASES[1:]]
        for offset, kind, value in sorted(actions, key=lambda a:(a[0],a[1])):
            target = origin_ns / 1e9 + offset / 1000
            while time.monotonic() < target:
                refuse(time.monotonic() < deadline, "trial_deadline_reached")
                time.sleep(min(.005, max(0,target-time.monotonic())))
            refuse(time.monotonic() < deadline, "trial_deadline_reached")
            if kind == "phase":
                before = link.capture_qdiscs()
                event = link.apply(value); event["qdisc_before"] = before
                event["requested_offset_ms"] = offset; phases.append(event)
            else:
                now = time.monotonic_ns()
                with lab_destination(): delivery.enqueue(queue,value)
                arrivals.append({"message_id":value["message_id"], "scheduled_offset_ms":offset,
                                 "enqueue_started_monotonic_ns":now, "enqueue_finished_monotonic_ns":time.monotonic_ns(),
                                 "arrival_lateness_ms":(now-origin_ns)/1e6-offset})
        worker.wait(timeout=max(.001,deadline-time.monotonic()))
        report["worker_exit_code"] = worker.returncode
        report["qdisc_final"] = link.capture_qdiscs()
    except Exception as exc:
        report["error"] = {"type":type(exc).__name__, "code":str(exc) if isinstance(exc,delivery.Refused) else "local_trial_failure"}
    finally:
        report["cleanup"] = cleanup_owned(worker, server, link)
        cleanup = all(item["stopped"] for item in report["cleanup"].values())
        for stream in logs:
            try: stream.close()
            except Exception: cleanup = False
        save(output / "arrivals.json",arrivals); save(output / "phase-events.json",phases)
        try:
            data = analyze(queue,ledger,arrivals,phases,planned)
            save(output / "queue-final.json",{k:v for k,v in data.items() if k not in {"recipient","metrics","checks"}})
            save(output / "recipient.json",data["recipient"])
            report["metrics"],report["checks"] = data["metrics"],{**report["checks"],**data["checks"]}
            report["checks"]["policy_and_deadline_unchanged"] = data["metadata"] == report["initial_queue_metadata"]
            worker_lines = (output / "worker.log").read_text().strip().splitlines()
            result = delivery.strict_json(worker_lines[-1]) if worker_lines else {}
            outcome = data["summary"]["worker_runs"][-1]["outcome"] if data["summary"]["worker_runs"] else None
            report["checks"]["worker_result_verified"] = (result.get("schema") == "resilience-worker-result/v1" and result.get("outcome") == outcome
                and result.get("states") == data["summary"]["states"] and result.get("attempts") == data["summary"]["attempts"]
                and report.get("worker_exit_code") == (0 if outcome == "all_acknowledged" else 2))
        except Exception as exc:
            report["analysis_error"] = type(exc).__name__
        save(output / "setup-and-cleanup.json",link.log)
        report["checks"]["owned_resources_removed"] = cleanup
        try:
            report["host_network_after"] = host_network_signature()
            report["checks"]["host_network_preserved"] = initial_network == report["host_network_after"]
        except Exception:
            report["checks"]["host_network_preserved"] = False
        report["elapsed_seconds"] = time.monotonic()-begun
        report["checks"]["within_trial_cap"] = report["elapsed_seconds"] < TRIAL_CAP_SECONDS
        report["success"] = not report.get("error") and not report.get("analysis_error") and all(report["checks"].values())
        report["success_meaning"] = "Valid measured execution/integrity; full delivery is separately metrics.all_messages_delivered_and_acked."
        report["artifact_sha256"] = {p.name:sha(p) for p in sorted(output.iterdir()) if p.is_file()}
        save(output / "report.json",report)
    return report


def run(output,repetitions=3,policy_path=None):
    refuse(sys.platform.startswith("linux") and os.geteuid()==0,"linux_vm_root_required")
    refuse(type(repetitions) is int and 1<=repetitions<=3,"repetitions_out_of_bounds")
    refuse(all(shutil.which(tool) for tool in ("ip","tc")),"iproute2_tools_required")
    policy = delivery.load_policy(policy_path)
    output = Path(output).absolute(); output.mkdir(parents=True,exist_ok=False,mode=0o700)
    start=time.monotonic(); deadline=start+CAMPAIGN_CAP_SECONDS-5
    report={"schema":"sawm-vm-netem-campaign/v1","success":False,"started_at_ms":time.time_ns()//1_000_000,
            "platform":{"system":platform.system(),"kernel":platform.release(),"machine":platform.machine(),"python":platform.python_version()},
            "tools":{"ip":command(["ip","-Version"]).stdout.strip(),"tc":command(["tc","-Version"]).stdout.strip()},
            "source_sha256":source_hashes(),"initial_policy":policy,"planned_trials":2*repetitions,"trials":[],
            "scope":{"real_linux_packets":True,"radio_validation":False,"cloud_access":False,"physical_radio_equipment":False,
                     "destination":DESTINATION,"subnet":"198.18.0.0/30","network":"two owned namespaces joined only to each other, no default route",
                     "transport_bootstrap":"Fixed lab address replaces module HOST only in this isolated benchmark; public core unchanged.",
                     "faults":"Linux tc netem in both directions on the two owned link0 devices only; VM primary interfaces unchanged.",
                     "radio_claim":"Packet-level surrogate for degraded communications, not an RF propagation/interference model; no RSSI/SNR/modulation.",
                     "random_seed":None,"seed_support":"not requested; iproute2 compatibility; stochastic loss draws are not paired",
                     "pairing":"Same message bytes, arrivals, policy and requested impairment profile; not identical packet loss draws or exposure.",
                     "effect":"One unique committed witness ledger row per message ID; not third-party API exactly-once assurance.",
                     "blackout":"No claim of communication through 100% loss. In-flight requests around transitions remain distinguishable.",
                     "recipient_timestamp":"accepted_monotonic_ns is sampled inside the transaction before commit completes; an ACK confirms completed commit.",
                     "max_trial_seconds":TRIAL_CAP_SECONDS,"max_campaign_seconds":CAMPAIGN_CAP_SECONDS},
            "requested_phases":PHASES,"arrival_interval_ms":ARRIVAL_INTERVAL_MS,"checks":{}}
    save(output/"report.json",report)
    trials=[]
    try:
        for repetition in range(1,repetitions+1):
            order=("static","adaptive") if repetition%2 else ("adaptive","static")
            for position,mode in enumerate(order):
                refuse(time.monotonic()<deadline,"campaign_deadline_reached")
                name=f"r{repetition}-{mode}"
                trial=run_trial(output/name,mode,repetition,policy,deadline)
                trials.append(trial)
                report["trials"].append({"directory":name,"mode":mode,"repetition":repetition,"order_position":position+1,
                                         "success":trial["success"],"metrics":trial.get("metrics"),"error":trial.get("error")})
                save(output/"report.json",report)
    except Exception as exc:
        report["error"]={"type":type(exc).__name__,"code":str(exc) if isinstance(exc,delivery.Refused) else "local_campaign_failure"}
    finally:
        groups=[]
        for mode in ("static","adaptive"):
            selected=[t for t in trials if t["mode"]==mode]
            groups.append({"mode":mode,"valid_trials":sum(t["success"] for t in selected),"trials":len(selected),
                           "metrics":{key:{"observed":[t.get("metrics",{}).get(key) for t in selected],
                            "mean":sum(values)/len(values) if values else None}
                            for key in ("delivered_unique","acked","attempts","timeouts","ack_p95_ms","delivery_p95_ms")
                            for values in [[t["metrics"][key] for t in selected if t.get("metrics",{}).get(key) is not None]]}})
        save(output/"aggregate.json",{"groups":groups,"interpretation":"All measured trials retained, including failures. Compare delivery, ACKs, attempts and variability; three repetitions do not prove a general advantage."})
        report["completed_trials"]=len(trials);report["elapsed_seconds"]=time.monotonic()-start
        paired=[]
        for repetition in range(1,repetitions+1):
            files=[output/f"r{repetition}-{m}"/"messages.json" for m in ("static","adaptive")]
            if all(p.exists() for p in files):paired.append(sha(files[0])==sha(files[1]))
        report["checks"]={"all_trials_completed":len(trials)==repetitions*2,"valid_trials":bool(trials) and all(t["success"] for t in trials),
                          "paired_message_bytes_identical":len(paired)==repetitions and all(paired),
                          "source_unchanged":source_hashes()==report["source_sha256"],"within_campaign_cap":report["elapsed_seconds"]<CAMPAIGN_CAP_SECONDS}
        report["success"]=not report.get("error") and all(report["checks"].values())
        save(output/"report.json",report)
        save(output/"manifest.json",{"schema":"sawm-vm-artifact-manifest/v1","sha256":{str(p.relative_to(output)):sha(p) for p in sorted(output.rglob('*')) if p.is_file() and p.name!='manifest.json'}})
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--repetitions',type=int,choices=(1,2,3),default=3)
    parser.add_argument('--policy',type=Path)
    parser.add_argument('--internal-role',choices=('worker','witness'),help=argparse.SUPPRESS)
    parser.add_argument('--namespace',help=argparse.SUPPRESS)
    parser.add_argument('--path',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--ready',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    try:
        if args.internal_role:
            return child(args.internal_role,args.namespace,args.path,args.ready)
        refuse(args.output_dir is not None,'output_dir_required')
        result=run(args.output_dir,args.repetitions,args.policy)
        print(delivery.canonical({"success":result['success'],"completed_trials":result['completed_trials'],"elapsed_seconds":result['elapsed_seconds'],"radio_validation":False}))
        return 0 if result['success'] else 2
    except Exception as exc:
        print(delivery.canonical({"success":False,"error":str(exc) if isinstance(exc,delivery.Refused) else type(exc).__name__}))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
