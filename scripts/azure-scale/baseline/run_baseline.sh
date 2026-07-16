#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Raw VM baseline pipeline — deploys Azure VMs WITHOUT Juju/charms,
# manually installs Slurm, runs the same ReFrame suite, and emits
# a timing report. Used for comparison with the Juju pipeline.
#
# Usage: run_baseline.sh --nodes 64 --gpu false --gpu_nodes 4 --ubuntu 24.04 --repeats 5

set -e

# --- Parse CLI arguments ---
NODES=2
ENABLE_GPU=true
GPU_NODES=1
UBUNTU_SERIES=24.04
REPEATS=5

while [[ $# -gt 0 ]]; do
  case $1 in
    --nodes) NODES="$2"; shift 2 ;;
    --gpu) ENABLE_GPU="$2"; shift 2 ;;
    --gpu_nodes) GPU_NODES="$2"; shift 2 ;;
    --ubuntu) UBUNTU_SERIES="$2"; shift 2 ;;
    --repeats) REPEATS="$2"; shift 2 ;;
    --help|-h)
      echo "Usage: run_baseline.sh --nodes 64 --gpu false --gpu_nodes 4 --ubuntu 24.04 --repeats 5"
      echo ""
      echo "Options:"
      echo "  --nodes N       Number of HB120rs_v3 compute nodes (default: 2, max: 64+)"
      echo "  --gpu BOOL      Enable GPU node nc4as-t4-v3 (default: true)"
      echo "  --gpu_nodes N   Number of NC4as_T4_v3 GPU nodes for multi-node HPL (default: 1)"
      echo "  --ubuntu VER    Ubuntu LTS series: 24.04 or 26.04 (default: 24.04)"
      echo "  --repeats N     Repetitions per benchmark (default: 5)"
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source shared timing utilities
source "${PARENT_DIR}/timing_utils.sh"

# Export for ReFrame config consumption
export ENABLE_GPU
export UBUNTU_SERIES

SSH_KEY="${HOME}/.ssh/id_rsa"
TOFU_VARS="-var compute_nodes=${NODES} -var enable_gpu=${ENABLE_GPU} -var ubuntu_series=${UBUNTU_SERIES}"

if [ ! -f "${SSH_KEY}" ]; then
    echo "ERROR: SSH key not found at ${SSH_KEY}. Generate one with: ssh-keygen -t rsa -b 4096"
    exit 1
fi

SSH_PUB_KEY=$(cat "${SSH_KEY}.pub")
TOFU_VARS="${TOFU_VARS} -var ssh_public_key=\"${SSH_PUB_KEY}\""

# Initialize timing
timer_init "baseline" "baseline_timing_report.json"

echo "Started at $(date)"
echo "Configuration: nodes=${NODES}, gpu=${ENABLE_GPU}, gpu_nodes=${GPU_NODES}, ubuntu=${UBUNTU_SERIES}, repeats=${REPEATS}"

# --- Phase 1: Provision raw VMs ---
timer_start "tofu_apply"
echo "Provisioning raw Azure VMs..."
tofu init
eval tofu apply -auto-approve ${TOFU_VARS}
timer_end "tofu_apply"

# Get login node IP
LOGIN_IP=$(tofu output -raw login_public_ip)
echo "Login node public IP: ${LOGIN_IP}"

# --- Phase 2: Wait for SSH ---
timer_start "wait_ssh"
echo "Waiting for SSH access on login node..."
while ! ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 ubuntu@${LOGIN_IP} "echo SSH ready" 2>/dev/null; do
  echo "  Waiting for SSH..."
  sleep 10
done
timer_end "wait_ssh"

# --- Phase 3: Manual Slurm setup ---
timer_start "setup_slurm"
echo "Setting up Slurm manually on raw VMs..."
chmod +x "${SCRIPT_DIR}/setup_slurm.sh"
bash "${SCRIPT_DIR}/setup_slurm.sh" "${LOGIN_IP}" "${NODES}" "${ENABLE_GPU}" "${GPU_NODES}" "${UBUNTU_SERIES}"
timer_end "setup_slurm"

# --- Phase 4: Install deps (same as Juju pipeline) ---
timer_start "install_deps"
echo "Installing additional dependencies on login node..."
ssh -o StrictHostKeyChecking=no ubuntu@${LOGIN_IP} << "EOF"
sudo apt-get update
sudo apt-get -y install libopenmpi-dev openmpi-bin libopenblas-dev build-essential
EOF
timer_end "install_deps"

# --- Phase 5: Clone repo and run ReFrame suite ---
timer_start "reframe_suite"
echo "Installing and running ReFrame suite on login node..."
ssh -o StrictHostKeyChecking=no ubuntu@${LOGIN_IP} "bash -s ${NODES} ${GPU_NODES} ${REPEATS}" << "REMOTE_EOF"
set -e
NUM_NODES="$1"
GPU_NODES="$2"
REPEATS="$3"

cd /nfs/home
source reframe-venv/bin/activate

if [ -d charmed-hpc-benchmarks ]; then
  rm -rf charmed-hpc-benchmarks
fi
git clone https://github.com/canonical/charmed-hpc-benchmarks.git
cd charmed-hpc-benchmarks/scripts/azure-scale

NUM_TASKS=$((NUM_NODES * 120))
HALF_TASKS=$((NUM_NODES / 2 * 120))

# Run the same set of multi-node / infrastructure checks as the Juju pipeline.
reframe \
  --config-file config/azure_scale_config.py \
  --checkpath ../../checks checks \
  --recursive \
  --run \
  --repeat "${REPEATS}" \
  -n 'charmed_osu_collective_check|imb_allreduce_check|hpl_cpu_check|hpl_gpu_check|hpcg_check|hpl_concurrent_check|osu_concurrent_check|juju_agent_overhead|slurm_dispatch_latency' \
  --setvar charmed_osu_collective_check.num_tasks="${NUM_TASKS}" \
  --setvar imb_allreduce_check.num_tasks="${NUM_TASKS}" \
  --setvar hpl_cpu_check.num_tasks="${NUM_TASKS}" \
  --setvar hpcg_check.num_tasks="${NUM_TASKS}" \
  --setvar hpl_concurrent_check.num_tasks="${HALF_TASKS}" \
  --setvar hpl_gpu_check.num_tasks="${GPU_NODES}"
REMOTE_EOF
timer_end "reframe_suite"

# --- Phase 6: Copy back results (to separate dir to avoid collision with Juju perflogs) ---
timer_start "copy_results"
echo "Copying back test outputs..."
scp -o StrictHostKeyChecking=no -r ubuntu@${LOGIN_IP}:/nfs/home/charmed-hpc-benchmarks/scripts/azure-scale/perflogs baseline_perflogs
scp -o StrictHostKeyChecking=no -r ubuntu@${LOGIN_IP}:/nfs/home/charmed-hpc-benchmarks/scripts/azure-scale/output baseline_output
timer_end "copy_results"

# --- Phase 7: Teardown ---
timer_start "teardown_tofu"
echo "Destroying raw VMs..."
retries=0
max_retries=5
retry_timer=60
while ! eval tofu destroy -auto-approve ${TOFU_VARS} && [ $retries -lt $max_retries ]; do
    retries=$((retries+1))
    echo "Attempt $retries failed. Retrying in $retry_timer seconds..."
    sleep $retry_timer
done
timer_end "teardown_tofu"

# --- Emit timing report ---
emit_timing_report
print_timing_summary

echo "Baseline tests completed at $(date). Check baseline_perflogs and baseline_output directories for results."
echo "Timing report: baseline_timing_report.json"
