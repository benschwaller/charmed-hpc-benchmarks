#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Comparison reporter for Juju vs. raw VM baseline benchmarking.

Reads two timing_report.json files (one from the Juju pipeline, one from
the baseline pipeline) and produces a side-by-side markdown comparison
table showing the "Juju/charm tax" as deltas and percentages.

Optionally reads perflogs from both runs to compare HPC benchmark results.

Usage:
    python3 compare.py --juju juju_timing_report.json --baseline baseline_timing_report.json
    python3 compare.py --juju juju_timing_report.json --baseline baseline_timing_report.json \
        --juju-perflogs perflogs/ --baseline-perflogs baseline_perflogs/
"""

import argparse
import json
import os
import sys


def load_json(path):
    """Load JSON content from a file path."""
    if not os.path.exists(path):
        print(f"WARNING: File not found: {path}", file=sys.stderr)
        return None
    with open(path) as f:
        return json.load(f)


def fmt_seconds(val):
    """Format a seconds value as a string."""
    if val is None:
        return "N/A"
    return f"{val:.2f}s"


def fmt_delta(juju, baseline):
    """Format the delta between two seconds values."""
    if juju is None or baseline is None:
        return "N/A"
    delta = juju - baseline
    if baseline == 0:
        if delta == 0:
            return "0.00s (—)"
        sign = "+" if delta >= 0 else ""
        return f"{sign}{delta:.2f}s (new)"
    pct = (delta / baseline) * 100
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.2f}s ({sign}{pct:.1f}%)"


def fmt_value(val, unit=""):
    """Format a value with an optional unit suffix."""
    if val is None:
        return "N/A"
    if isinstance(val, float):
        return f"{val:.2f}{unit}"
    return f"{val}{unit}"


def fmt_overhead(juju, baseline, unit=""):
    """Format the overhead between two values as a delta and percentage."""
    if juju is None or baseline is None:
        return "N/A"
    delta = juju - baseline
    if baseline == 0:
        return f"+{delta:.2f}{unit} (new)"
    pct = (delta / baseline) * 100
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.2f}{unit} ({sign}{pct:.1f}%)"


def compare_timing(juju_data, baseline_data):
    """Build a markdown comparison of deployment timing phases and totals."""
    lines = []
    lines.append("## Deployment Timing Comparison\n")

    # Phase-by-phase comparison
    lines.append("### Phase Timings\n")
    lines.append("| Phase | Juju (s) | Baseline (s) | Delta |")
    lines.append("|-------|----------|--------------|-------|")

    all_phases = set()
    if juju_data and "phases" in juju_data:
        all_phases.update(juju_data["phases"].keys())
    if baseline_data and "phases" in baseline_data:
        all_phases.update(baseline_data["phases"].keys())

    for phase in sorted(all_phases):
        j_val = None
        b_val = None
        if juju_data and phase in juju_data.get("phases", {}):
            j_val = juju_data["phases"][phase].get("duration_seconds")
        if baseline_data and phase in baseline_data.get("phases", {}):
            b_val = baseline_data["phases"][phase].get("duration_seconds")

        lines.append(
            f"| {phase} | {fmt_seconds(j_val)} | {fmt_seconds(b_val)} | "
            f"{fmt_delta(j_val, b_val) if j_val is not None and b_val is not None else 'N/A'} |"
        )

    # Totals
    lines.append("\n### Totals\n")
    lines.append("| Metric | Juju | Baseline | Overhead |")
    lines.append("|--------|------|----------|----------|")

    for key in ["spinup_seconds", "teardown_seconds", "total_seconds"]:
        j_val = juju_data.get("totals", {}).get(key) if juju_data else None
        b_val = baseline_data.get("totals", {}).get(key) if baseline_data else None
        label = key.replace("_", " ").title()
        lines.append(
            f"| {label} | {fmt_seconds(j_val)} | {fmt_seconds(b_val)} | {fmt_overhead(j_val, b_val)} |"
        )

    # Application readiness
    lines.append("\n### Application Readiness Timeline\n")
    lines.append("| Application | Juju (s) | Baseline (s) | Delta |")
    lines.append("|-------------|----------|--------------|-------|")

    all_apps = set()
    if juju_data and "application_readiness" in juju_data:
        all_apps.update(juju_data["application_readiness"].keys())
    if baseline_data and "application_readiness" in baseline_data:
        all_apps.update(baseline_data["application_readiness"].keys())

    for app in sorted(all_apps):
        j_val = (
            juju_data.get("application_readiness", {}).get(app, {}).get("seconds_from_wait_start")
            if juju_data
            else None
        )
        b_val = (
            baseline_data.get("application_readiness", {})
            .get(app, {})
            .get("seconds_from_wait_start")
            if baseline_data
            else None
        )
        lines.append(
            f"| {app} | {fmt_seconds(j_val)} | {fmt_seconds(b_val)} | "
            f"{fmt_delta(j_val, b_val) if j_val is not None and b_val is not None else 'N/A'} |"
        )

    return "\n".join(lines)


def compare_perflogs(juju_dir, baseline_dir):
    """Build a markdown comparison of ReFrame benchmark results from perflogs."""
    lines = []
    lines.append("\n## ReFrame Benchmark Results Comparison\n")

    juju_results = parse_perflogs(juju_dir) if juju_dir else {}
    baseline_results = parse_perflogs(baseline_dir) if baseline_dir else {}

    if not juju_results and not baseline_results:
        lines.append("*No perflogs found in the specified directories.*\n")
        return "\n".join(lines)

    all_tests = set(juju_results.keys()) | set(baseline_results.keys())

    lines.append("| Test | Metric | Juju | Baseline | Overhead |")
    lines.append("|------|--------|------|----------|----------|")

    for test in sorted(all_tests):
        j_metrics = juju_results.get(test, {})
        b_metrics = baseline_results.get(test, {})

        all_metrics = set(j_metrics.keys()) | set(b_metrics.keys())
        for metric in sorted(all_metrics):
            j_val = j_metrics.get(metric)
            b_val = b_metrics.get(metric)
            unit = ""
            if j_val and len(j_val) > 2:
                unit = f" {j_val[2]}"
            elif b_val and len(b_val) > 2:
                unit = f" {b_val[2]}"

            j_display = f"{j_val[0]:.2f}{unit}" if j_val else "N/A"
            b_display = f"{b_val[0]:.2f}{unit}" if b_val else "N/A"

            if j_val is not None and b_val is not None:
                delta = j_val[0] - b_val[0]
                if b_val[0] == 0:
                    if delta == 0:
                        overhead = f"0.00{unit} (—)"
                    else:
                        sign = "+" if delta >= 0 else ""
                        overhead = f"{sign}{delta:.2f}{unit} (new)"
                else:
                    pct = (delta / b_val[0]) * 100
                    sign = "+" if delta >= 0 else ""
                    overhead = f"{sign}{delta:.2f}{unit} ({sign}{pct:.1f}%)"
            else:
                overhead = "N/A"

            lines.append(f"| {test} | {metric} | {j_display} | {b_display} | {overhead} |")

    return "\n".join(lines)


def parse_perflogs(perflogs_dir):
    """Parse ReFrame perflog files and extract test/metric/value/unit tuples."""
    results = {}
    if not os.path.exists(perflogs_dir):
        return results

    for root, _, files in os.walk(perflogs_dir):
        for fname in files:
            if not fname.endswith(".log"):
                continue
            fpath = os.path.join(root, fname)
            test_name = fname.replace(".log", "")
            results[test_name] = {}

            try:
                with open(fpath) as f:
                    header = f.readline().strip()
                    cols = header.split("|")
                    for line in f:
                        vals = line.strip().split("|")
                        if len(vals) < len(cols):
                            continue
                        row = dict(zip(cols, vals))
                        if "perf_var" in row and "perf_value" in row:
                            try:
                                val = float(row["perf_value"])
                            except (ValueError, TypeError):
                                continue
                            results[test_name][row["perf_var"]] = (
                                val,
                                None,
                                row.get("perf_unit", ""),
                            )
            except Exception as e:
                print(f"WARNING: Could not parse {fpath}: {e}", file=sys.stderr)

    return results


def main():
    """Parse arguments and render the comparison report."""
    parser = argparse.ArgumentParser(
        description="Compare Juju vs. raw VM baseline benchmarking results."
    )
    parser.add_argument("--juju", required=True, help="Juju pipeline timing_report.json")
    parser.add_argument("--baseline", required=True, help="Baseline pipeline timing_report.json")
    parser.add_argument("--juju-perflogs", default=None, help="Directory with Juju perflogs")
    parser.add_argument(
        "--baseline-perflogs", default=None, help="Directory with baseline perflogs"
    )
    parser.add_argument("-o", "--output", default=None, help="Output file (default: stdout)")
    args = parser.parse_args()

    juju_data = load_json(args.juju)
    baseline_data = load_json(args.baseline)

    report_lines = []
    report_lines.append("# Charmed-HPC Benchmarking Comparison Report\n")

    # Metadata
    if juju_data:
        report_lines.append(f"- **Juju run ID**: {juju_data.get('run_id', 'N/A')}")
    if baseline_data:
        report_lines.append(f"- **Baseline run ID**: {baseline_data.get('run_id', 'N/A')}")
    report_lines.append("")

    report_lines.append(compare_timing(juju_data, baseline_data))

    if args.juju_perflogs or args.baseline_perflogs:
        report_lines.append(compare_perflogs(args.juju_perflogs, args.baseline_perflogs))

    report = "\n".join(report_lines)

    if args.output:
        with open(args.output, "w") as f:
            f.write(report + "\n")
        print(f"Report written to {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
