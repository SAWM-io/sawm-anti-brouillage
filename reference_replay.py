#!/usr/bin/env python3
"""Replay the private 16-channel reference simulator; never substitute an API rank.

The external source tree and ODVL engine are required and remain private.
No HTTP, Cloud, radio hardware or live packet transport is exercised here.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import sys
import time

FILES = ("experiments/lien-adaptatif/lien.py", "experiments/lien-adaptatif/adaptation.py",
         "experiments/lien-adaptatif/equivalence.py", "experiments/lien-adaptatif/run_bench.py",
         "backend/services/basira_odvl.py")


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sources(root):
    root = Path(root).expanduser().resolve(strict=True)
    public = Path(__file__).resolve().parent
    if root == public or public in root.parents:
        raise ValueError("private_source_root_must_be_external_to_public_repository")
    resolved = {}
    for name in FILES:
        target = (root / name).resolve(strict=True)
        if root not in target.parents or not target.is_file():
            raise ValueError("private_source_outside_declared_root:" + name)
        resolved[name] = target
    return root, resolved


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def trace(L, A, genre, cls):
    # Instrument the existing play loop without changing observations or RNG calls.
    world = L.Monde(random.Random(1000), [L.Brouilleur(genre, 22.0, 150, 950,
        latence_poursuite_pas=4, largeur_canaux=3, graine=0)], 1200)
    arm = cls(random.Random(2000))
    rows = []
    choose = arm.choisir
    def recorded(step, measurements):
        previous = arm.canal
        selected = choose(step, measurements)
        decision = arm.decisions[-1] if arm.decisions and arm.decisions[-1].pas == step else None
        rows.append({"step": step, "model_elapsed_ms": step * L.PAS_MS,
            "current_channel": previous, "selected_channel": selected,
            "observed_snr_db": [measurements[i] for i in range(L.CANAUX)],
            "model_throughput": arm.par_pas[-1],
            "decision_reason": decision.motif if decision else "keep_current_channel"})
        return selected
    arm.choisir = recorded
    result = arm.jouer(world)
    return {"seed": 0, "scenario": genre, "arm": cls.nom, "result": result,
            "channel_count": L.CANAUX, "steps": rows,
            "scope": "Numerical model readings, not measured RF; no extra world queries added."}


def run(private_root, output, trace_only=False):
    root, paths = sources(private_root)
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output_directory_must_be_new")
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    output.mkdir(parents=True, mode=0o700)
    report = {"schema": "sawm-radio-reference-replay/v1", "source_sha256": hashes,
        "scope": {"simulation": True, "physical_radio": False, "live_transport": False,
                  "cloud_calls": 0, "tsst_exercised": False, "sawm_llm_exercised": False},
        "engine": "Private stateful ODVLNetwork; a stateless /odvl/rank call is not equivalent.",
        "protocol": {"seeds": 8, "steps": 1200, "channels": 16, "power_db": 22.0,
                     "jam_start_step": 150, "jam_duration_steps": 950, "pursuit_latency_steps": 4,
                     "sweep_width_channels": 3, "retune_cost_steps": 2},
        "trace_only": trace_only, "success": False}
    started = time.perf_counter()
    try:
        folder = root / "experiments/lien-adaptatif"
        L = load("lien", folder / "lien.py")
        A = load("adaptation", folder / "adaptation.py")
        load("equivalence", folder / "equivalence.py")
        B = load("sawm_reference_radio_bench", folder / "run_bench.py")
        if Path(A.odvl.__file__).resolve() != paths["backend/services/basira_odvl.py"]:
            raise ValueError("unexpected_odvl_engine_path")
        if not trace_only:
            report["results"] = B.brouillage(8, 1200, puissance_db=22.0)
        report["trace_files"] = []
        for scenario in B.GENRES:
            for label, cls in (("reactive", A.B_reactif), ("odvl", A.F_odvl)):
                name = "trace-%s-%s-seed0.json" % (scenario, label)
                data = trace(L, A, scenario, cls)
                save(output / name, data)
                report["trace_files"].append(name)
                if not trace_only:
                    expected = report["results"]["resultats"][scenario][cls.nom]["par_graine"][0]
                    if data["result"]["debit_relatif"] != expected:
                        raise ValueError("instrumented_trace_changed_reference_result")
        report["success"] = all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name]
                                for name, path in paths.items())
        if not report["success"]:
            raise ValueError("private_sources_changed_during_replay")
    except Exception as exc:
        report["error"] = type(exc).__name__ + ":" + str(exc)
        raise
    finally:
        report["elapsed_s"] = time.perf_counter() - started
        save(output / "replay.json", report)
        manifest = {file.name: hashlib.sha256(file.read_bytes()).hexdigest()
                    for file in output.glob("*.json") if file.name != "manifest.json"}
        save(output / "manifest.json", manifest)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--trace-only", action="store_true", help="Replay seed 0 for visualizations, without repeating the full campaign")
    args = parser.parse_args()
    try:
        report = run(args.private_source_root, args.output_dir, args.trace_only)
        print(json.dumps({"success": report["success"], "trace_only": report["trace_only"],
                          "output_dir": str(args.output_dir)}, ensure_ascii=False))
        return 0 if report["success"] else 2
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
