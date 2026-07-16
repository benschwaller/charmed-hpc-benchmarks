#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Scalable Azure benchmarking pipeline using Juju + charmed-hpc.
# Deploys a parameterized cluster, runs ReFrame benchmarks, retrieves results,
# and emits a structured timing report.
#
# Usage: run_azure_scale.sh --nodes 64 --gpu false --gpu_nodes 4 --ubuntu 24.04 --repeats 5

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
      echo "Usage: run_azure_scale.sh --nodes 64 --gpu false --gpu_nodes 4 --ubuntu 24.04 --repeats 5"
      echo ""
      echo "Options:"
      echo "  --nodes N       Number of HB120rs_v3 compute nodes (default: 2, max: 64+)"
      echo "  --gpu BOOL      Enable GPU partition nc4as-t4-v3 (default: true)"
      echo "  --gpu_nodes N   Number of NC4as_T4_v3 GPU nodes for multi-node HPL (default: 1)"
      echo "  --ubuntu VER    Ubuntu LTS series: 24.04 or 26.04 (default: 24.04)"
      echo "  --repeats N     Repetitions per benchmark (default: 5)"
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Resolve script directory for relative paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source timing utilities
source "${SCRIPT_DIR}/timing_utils.sh"

# --- Validate environment ---
if [ -z "${ARM_SUBSCRIPTION_ID}" ]; then
  echo "ERROR: environment variable ARM_SUBSCRIPTION_ID is not set."
  echo "Run \`export ARM_SUBSCRIPTION_ID=<your-azure-subscription-id>\` before launching this script."
  exit 1
fi

# Export for ReFrame config consumption
export ENABLE_GPU
export UBUNTU_SERIES

MODEL=charmed-hpc
CONTROLLER=scale-controller
TOFU_VARS="-var hb120rs_v3_units=${NODES} -var enable_gpu=${ENABLE_GPU} -var nc4as_t4_v3_units=${GPU_NODES} -var ubuntu_series=${UBUNTU_SERIES}"

# Initialize timing
timer_init "juju" "juju_timing_report.json"

echo "Started at $(date)"
echo "Configuration: nodes=${NODES}, gpu=${ENABLE_GPU}, gpu_nodes=${GPU_NODES}, ubuntu=${UBUNTU_SERIES}, repeats=${REPEATS}"

# --- Phase 1: Bootstrap Juju controller ---
timer_start "bootstrap"
echo "Bootstrapping azure controller: ${CONTROLLER}..."
juju bootstrap azure "${CONTROLLER}" --constraints "instance-role=auto"
timer_end "bootstrap"

# --- Phase 2: Deploy infrastructure via OpenTofu ---
timer_start "tofu_apply"
echo "Deploying plan to set up cluster..."
tofu init
tofu apply -auto-approve ${TOFU_VARS}
timer_end "tofu_apply"

# --- Phase 3: Wait for all applications to become active ---
timer_start "wait_active"
juju switch ${MODEL}
WAIT_LOOP_START=$(date +%s.%N)

while true; do
  echo "Waiting for all model applications to become active..."
  status_json=$(juju status --format=json)

  # Track per-application readiness
  track_app_status "$status_json" "$WAIT_LOOP_START"

  all_active=$(echo "$status_json" | jq -r '[.applications | to_entries[] | .value["application-status"].current == "active"] | all')
  [[ "$all_active" == "false" ]] || break
  sleep 5
done
timer_end "wait_active"

# --- Phase 4: Generate SSH key and register ---
echo "Generating temporary SSH key pair..."
SSH_KEY_PATH="$(mktemp -p $HOME/.ssh/ -u)"
ssh-keygen -t ed25519 -f "${SSH_KEY_PATH}" -N ""
juju add-ssh-key "$(cat ${SSH_KEY_PATH}.pub)"

# --- Phase 5: Run node-configured actions (parallelized for scale) ---
timer_start "node_configured_gpu"
if [ "$ENABLE_GPU" = "true" ]; then
  echo "Running node-configured on GPU partition (${GPU_NODES} nodes)..."
  if [ "$GPU_NODES" -eq 1 ]; then
    juju run nc4as-t4-v3/leader node-configured
  else
    if ! juju run nc4as-t4-v3 node-configured 2>/dev/null; then
      juju status nc4as-t4-v3 --format=json | jq -r '.applications."nc4as-t4-v3".units | keys[]' | \
        xargs -P 0 -I{} juju run {} node-configured
    fi
  fi
  sleep 15
fi
timer_end "node_configured_gpu"

timer_start "node_configured_cpu"
echo "Running node-configured on CPU partition (all ${NODES} units)..."
# Run on all units at once for scalability.
# Fallback to per-unit if app-level action fails.
if ! juju run hb120rs-v3 node-configured 2>/dev/null; then
  echo "App-level action failed, falling back to parallel per-unit execution..."
  juju status hb120rs-v3 --format=json | jq -r '.applications."hb120rs-v3".units | keys[]' | \
    xargs -P 0 -I{} juju run {} node-configured
fi
sleep 15
timer_end "node_configured_cpu"

# --- Phase 6: Install dependencies ---
timer_start "install_deps"
if [ "$ENABLE_GPU" = "true" ]; then
  echo "Installing libcublas12 and NCCL on GPU nodes..."
  juju ssh nc4as-t4-v3/leader -i "${SSH_KEY_PATH}" << "EOF"
sudo apt-get update
sudo apt-get install -y libcublas12 libnccl2 libnccl-dev
EOF
fi
timer_end "install_deps"

# --- Phase 7: Install and run ReFrame suite ---
timer_start "reframe_suite"
echo "Installing and running ReFrame suite on the login node..."
juju ssh login/leader -i "${SSH_KEY_PATH}" "bash -s ${NODES} ${GPU_NODES} ${REPEATS}" << "REMOTE_EOF"
set -e
NUM_NODES="$1"
GPU_NODES="$2"
REPEATS="$3"

# Software necessary for building ReFrame test applications
sudo apt-get update
sudo apt-get -y install libopenmpi-dev openmpi-bin libopenblas-dev \
  build-essential python3-venv nvidia-cuda-toolkit-gcc git

# Use shared file system for all tests
cd /nfs/home

# Install ReFrame and suite
python3 -m venv reframe-venv
source reframe-venv/bin/activate
pip install ReFrame-HPC

# Clone the benchmark suite
if [ -d charmed-hpc-benchmarks ]; then
  rm -rf charmed-hpc-benchmarks
fi
git clone https://github.com/canonical/charmed-hpc-benchmarks.git
cd charmed-hpc-benchmarks/scripts/azure-scale

# Calculate num_tasks: nodes * 120 cores per HB120rs_v3
NUM_TASKS=$((NUM_NODES * 120))
HALF_TASKS=$((NUM_NODES / 2 * 120))

# Run both existing top-level checks and new azure-scale checks.
# Filter to only multi-node / infrastructure checks (exclude point-to-point and single-node).
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

# --- Phase 8: Copy back test outputs ---
timer_start "copy_results"
echo "Copying back test outputs..."
juju scp -- -i "${SSH_KEY_PATH}" -r login/leader:/nfs/home/charmed-hpc-benchmarks/scripts/azure-scale/perflogs .
juju scp -- -i "${SSH_KEY_PATH}" -r login/leader:/nfs/home/charmed-hpc-benchmarks/scripts/azure-scale/output .
timer_end "copy_results"

# --- Phase 9: Teardown ---
timer_start "teardown_tofu"
echo "Destroying cluster..."
retries=0
max_retries=5
retry_timer=60
while ! tofu destroy -auto-approve ${TOFU_VARS} && [ $retries -lt $max_retries ]; do
    retries=$((retries+1))
    echo "Attempt $retries failed. Retrying in $retry_timer seconds..."
    sleep $retry_timer
done
timer_end "teardown_tofu"

timer_start "teardown_juju"
echo "Destroying controller: ${CONTROLLER}..."
juju destroy-controller ${CONTROLLER} --force --no-prompt --destroy-all-models --destroy-storage
echo "Deleting temporary SSH key pair at: ${SSH_KEY_PATH}..."
rm -f "${SSH_KEY_PATH}" "${SSH_KEY_PATH}.pub"
timer_end "teardown_juju"

# --- Emit timing report ---
emit_timing_report
print_timing_summary

echo "Tests completed at $(date). Check output and perflogs directories for results."
echo "Timing report: juju_timing_report.json"
