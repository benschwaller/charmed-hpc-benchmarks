#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Wrapper script that runs the full sequential benchmarking pipeline:
#   1. Juju pipeline (deploy → benchmark → teardown)
#   2. Baseline pipeline (deploy raw VMs → setup Slurm → benchmark → teardown)
#   3. Comparison report
#
# VMs are never held simultaneously — the Juju cluster is fully torn down
# before the baseline VMs are provisioned, minimizing Azure quota pressure.
#
# Usage: run_all.sh --nodes 64 --ubuntu 24.04 --repeats 5 [--repo URL --ref REF]

set -e

# --- Parse CLI arguments (pass through to sub-scripts) ---
ARGS=()
while [[ $# -gt 0 ]]; do
  ARGS+=("$1")
  shift
done

# Default values if not provided
NODES=2
UBUNTU_SERIES=24.04
REPEATS=5

# Re-parse to extract for display
i=0
while [ $i -lt ${#ARGS[@]} ]; do
  case "${ARGS[$i]}" in
    --nodes) NODES="${ARGS[$((i+1))]}"; i=$((i+2)) ;;
    --ubuntu) UBUNTU_SERIES="${ARGS[$((i+1))]}"; i=$((i+2)) ;;
    --repeats) REPEATS="${ARGS[$((i+1))]}"; i=$((i+2)) ;;
    *) i=$((i+1)) ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "============================================================"
echo "  Charmed-HPC Comprehensive Benchmarking Pipeline"
echo "============================================================"
echo "  Nodes:       ${NODES}"
echo "  Ubuntu:      ${UBUNTU_SERIES}"
echo "  Repeats:     ${REPEATS}"
echo "  Started:     $(date)"
echo "============================================================"
echo ""

# --- Phase 1: Juju pipeline ---
echo "========== PHASE 1/3: Juju + Charmed-HPC Pipeline =========="
echo "Starting at $(date)"
echo ""
chmod +x "${SCRIPT_DIR}/run_azure_scale.sh"
bash "${SCRIPT_DIR}/run_azure_scale.sh" "${ARGS[@]}"
echo ""
echo "Juju pipeline completed at $(date)"
echo ""

# --- Phase 2: Baseline pipeline ---
echo "========== PHASE 2/3: Raw VM Baseline Pipeline =========="
echo "Starting at $(date)"
echo ""
chmod +x "${SCRIPT_DIR}/baseline/run_baseline.sh"
bash "${SCRIPT_DIR}/baseline/run_baseline.sh" "${ARGS[@]}"
echo ""
echo "Baseline pipeline completed at $(date)"
echo ""

# --- Phase 3: Comparison ---
echo "========== PHASE 3/3: Comparison Report =========="
echo "Generating comparison report at $(date)"
echo ""
python3 "${SCRIPT_DIR}/compare.py" \
  --juju juju_timing_report.json \
  --baseline baseline_timing_report.json \
  --juju-perflogs perflogs/ \
  --baseline-perflogs baseline_perflogs/ \
  -o comparison_report.md

echo ""
echo "============================================================"
echo "  Pipeline Complete"
echo "  Finished: $(date)"
echo "  Reports:"
echo "    - juju_timing_report.json"
echo "    - baseline_timing_report.json"
echo "    - comparison_report.md"
echo "    - perflogs/              (Juju ReFrame results)"
echo "    - baseline_perflogs/     (baseline ReFrame results)"
echo "    - output/                (Juju ReFrame output)"
echo "    - baseline_output/       (baseline ReFrame output)"
echo "============================================================"
