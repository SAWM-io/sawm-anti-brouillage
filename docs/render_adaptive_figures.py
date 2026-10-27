#!/usr/bin/env python3
"""Render measured calibration examples from preserved benchmark JSON (stdlib only).

Usage: python3 docs/render_adaptive_figures.py --input-dir CAMPAIGN --output-dir NEW_DIR
The campaign can be an external raw-evidence folder or its preserved repository copy.
No network access, package installation, or absolute paths appear in the figures.
"""
from __future__ import annotations

import argparse
import hashlib
from html import escape
import json
import math
from pathlib import Path

PROFILES = (("latency_jump_recovery", "Latency jump and recovery"),
            ("blackout_return", "Blackout and worker restart"))
CONTROLS = (("request_timeout_ms", "Request timeout / RTO", "request_timeout_ms"),
            ("retry_delay_ms", "Next retry delay", "base_delay_ms"))


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def label(x, y, text, *, size=14, anchor="start", fill="#222", weight="normal"):
    return f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" text-anchor="{anchor}" fill="{fill}" font-weight="{weight}">{escape(str(text))}</text>'


def line(x1, y1, x2, y2, *, stroke="#777", width=1, dash=None):
    extra = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" stroke="{stroke}" stroke-width="{width}"{extra}/>'


def load_example(root, profile, repetition):
    relative = Path(f"r{repetition}-{profile}-adaptive")
    report_file, trace_file = root / relative / "report.json", root / relative / "calibration-postanalysis.json"
    report, trace = read(report_file), read(trace_file)
    if report["mode"] != "adaptive" or report["profile"] != profile or report["repetition"] != repetition:
        raise ValueError("report identity does not match selected example")
    previous_time = -1
    controls = {"request_timeout_ms": report["initial_policy"]["request_timeout_ms"],
                "retry_delay_ms": report["initial_policy"]["base_delay_ms"]}
    for event in trace:
        timestamp = event["attempt_finished_offset_ms"]
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp < previous_time:
            raise ValueError("trace completion timestamps must be finite and ordered")
        previous_time = timestamp
        for key in controls:
            if event["old_" + key] != controls[key]:
                raise ValueError("trace must continue the prior controller state")
            controls[key] = event["new_" + key]
    if not trace:
        raise ValueError("no completed calibration observations")
    return report, trace, {str(path.relative_to(root)): digest(path) for path in (report_file, trace_file)}


def render(root, output, repetition=1):
    if repetition not in (1, 2, 3):
        raise ValueError("repetition must be 1, 2, or 3")
    examples, sources = [], {}
    for profile, title in PROFILES:
        report, trace, hashes = load_example(root, profile, repetition)
        examples.append((profile, title, report, trace))
        sources.update(hashes)
    xmax = max(1250, math.ceil(max(trace[-1]["attempt_finished_offset_ms"] for _, _, _, trace in examples) / 250) * 250)
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800" viewBox="0 0 1280 800" role="img" aria-label="Measured adaptive HTTP controller settings over time">',
             '<rect width="1280" height="800" fill="#fafafa"/>', '<g font-family="Arial,Helvetica,sans-serif">',
             label(44, 42, "Continuous calibration from completed HTTP attempts", size=25, weight="bold"),
             label(44, 70, f"Repetition {repetition} examples, not an aggregate · Owned localhost processes · No radio validation", size=15, fill="#555")]
    for column, (profile, title, report, trace) in enumerate(examples):
        x = 115 + column * 600
        parts.append(label(x, 115, title, size=19, weight="bold"))
        metrics = report["metrics"]
        parts.append(label(x, 140, f'{metrics["delivered_unique"]}/20 committed · {metrics["acked"]}/20 verified ACKs · {metrics["attempts"]} attempts', size=14, fill="#555"))
        for row, (key, title, initial_key) in enumerate(CONTROLS):
            top, height, width = 190 + row * 255, 180, 465
            cap_key = "request_timeout_ms" if key == "request_timeout_ms" else "max_delay_ms"
            ymax = report["initial_policy"][cap_key]
            initial = report["initial_policy"][initial_key]
            px = lambda value: x + value / xmax * width
            py = lambda value: top + height - value / ymax * height
            parts.append(label(x, top - 18, title + " (ms)", size=15, weight="bold"))
            parts.append(f'<rect x="{px(300):.2f}" y="{top}" width="{px(1000) - px(300):.2f}" height="{height}" fill="#e5e5e5"/>')
            for tick in (0, ymax / 2, ymax):
                parts.append(line(x, py(tick), x + width, py(tick), stroke="#d1d1d1"))
                parts.append(label(x - 12, py(tick) + 5, f"{tick:g}", size=13, anchor="end", fill="#666"))
            for tick in sorted({0, 300, 600, 1000, xmax}):
                parts.append(line(px(tick), top + height, px(tick), top + height + 5))
                parts.append(label(px(tick), top + height + 24, f"{tick / 1000:g}", size=13, anchor="middle", fill="#666"))
            parts.append(line(x, top, x, top + height, stroke="#444"))
            parts.append(line(x, top + height, x + width, top + height, stroke="#444"))
            parts.append(line(x, py(initial), x + width, py(initial), stroke="#888", dash="5 4"))
            restart = report.get("restart")
            if restart:
                for mark in (restart["kill_offset_ms"], restart["restart_offset_ms"]):
                    parts.append(line(px(mark), top, px(mark), top + height, stroke="#999", dash="2 4"))
                if row == 0:
                    parts.append(label(px(restart["restart_offset_ms"]) + 6, top + 20, "restart", size=12, fill="#555"))
            path = f"M {px(0):.2f} {py(initial):.2f}"
            for event in trace:
                path += f' H {px(event["attempt_finished_offset_ms"]):.2f} V {py(event["new_" + key]):.2f}'
            parts.append(f'<path d="{path}" stroke="#111" stroke-width="2.3" fill="none"/>')
            for event in trace:
                parts.append(f'<circle cx="{px(event["attempt_finished_offset_ms"]):.2f}" cy="{py(event["new_" + key]):.2f}" r="2.5" fill="#111"/>')
            if row == 1:
                parts.append(label(x + width / 2, top + height + 49, "Elapsed since trial origin (seconds)", size=14, anchor="middle"))
    parts += [line(45, 705, 80, 705, stroke="#111", width=2.3), label(90, 710, "Persisted setting after each completed attempt", size=13),
              line(438, 705, 473, 705, stroke="#888", dash="5 4"), label(483, 710, "Initial setting", size=13),
              '<rect x="650" y="696" width="32" height="17" fill="#e5e5e5"/>', label(692, 710, "Planned disruption: 0.3–1.0 s", size=13),
              label(45, 750, "Phase shading is added after the run. The controller receives completed network observations only; it never sees the fault schedule.", size=13, fill="#555"),
              label(45, 773, "Timeout observations are censored. Interrupted attempts produce no calibration observation. Restart markers use measured times.", size=13, fill="#555"),
              '</g></svg>']
    output.mkdir(parents=True, exist_ok=False)
    figure = output / "calibration-examples.svg"
    figure.write_text("\n".join(parts) + "\n", encoding="utf-8")
    provenance = {"schema": "resilience-calibration-figure/v1", "repetition": repetition,
                  "examples_not_aggregate": True, "source_sha256": sources,
                  "renderer_sha256": digest(Path(__file__)), "figure_sha256": {figure.name: digest(figure)},
                  "axes": {"x": "real completed-attempt offset from trial origin, milliseconds displayed as seconds",
                           "y": "persisted integer controller setting, milliseconds"}}
    (output / "figure-provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return figure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetition", type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args()
    figure = render(args.input_dir, args.output_dir, args.repetition)
    print(figure.name)


if __name__ == "__main__":
    main()
