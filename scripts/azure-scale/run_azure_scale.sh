#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Scalable Azure CPU benchmarking pipeline using Juju + charmed-hpc.
# Deploys a parameterized cluster, runs a multi-node ReFrame benchmark suite,
# retrieves results, and emits a structured timing report.
#
# Usage: run_azure_scale.sh --nodes 64 --ubuntu 24.04 --repeats 5

set -e

# --- Parse CLI arguments ---
NODES=2
UBUNTU_SERIES=24.04
REPEATS=5
REPO_URL="https://github.com/canonical/charmed-hpc-benchmarks.git"
REPO_REF="main"

while [[ $# -gt 0 ]]; do
  case $1 in
    --nodes) NODES="$2"; shift 2 ;;
    --ubuntu) UBUNTU_SERIES="$2"; shift 2 ;;
    --repeats) REPEATS="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --ref) REPO_REF="$2"; shift 2 ;;
    --help|-h)
      echo "Usage: run_azure_scale.sh --nodes 64 --ubuntu 24.04 --repeats 5"
      echo ""
      echo "Options:"
      echo "  --nodes N       Number of HB120rs_v3 compute nodes (default: 2, max: 64+)"
      echo "  --ubuntu VER    Ubuntu LTS series: 24.04 or 26.04 (default: 24.04)"
      echo "  --repeats N     Repetitions per benchmark (default: 5)"
      echo "  --repo URL      Git repo to clone on the login node"
      echo "                  (default: canonical/charmed-hpc-benchmarks)"
      echo "  --ref REF       Git branch/tag/commit to check out (default: main)"
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
export UBUNTU_SERIES

MODEL=charmed-hpc
CONTROLLER=scale-controller
TOFU_VARS="-var hb120rs_v3_units=${NODES} -var ubuntu_series=${UBUNTU_SERIES}"

# Initialize timing
timer_init "juju" "juju_timing_report.json"

echo "Started at $(date)"
echo "Configuration: nodes=${NODES}, ubuntu=${UBUNTU_SERIES}, repeats=${REPEATS}, repo=${REPO_URL}@${REPO_REF}"

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
SSH_KEY_PATH="$(mktemp -p "$HOME/.ssh/" -u)"
ssh-keygen -t ed25519 -f "${SSH_KEY_PATH}" -N ""
juju add-ssh-key "$(cat "${SSH_KEY_PATH}.pub")"

# --- Phase 5: Run node-configured actions (parallelized for scale) ---
timer_start "node_configured"
echo "Running node-configured on CPU partition (all ${NODES} units)..."
# Run on all units at once for scalability; fall back to per-unit if the
# app-level action is unsupported.
if ! juju run hb120rs-v3 node-configured 2>/dev/null; then
  echo "App-level action failed, falling back to parallel per-unit execution..."
  juju status hb120rs-v3 --format=json | jq -r '.applications."hb120rs-v3".units | keys[]' | \
    xargs -P 0 -I{} juju run {} node-configured
fi
sleep 15
timer_end "node_configured"

# --- Phase 6: Install build dependencies (no-op placeholder for parity) ---
timer_start "install_deps"
# Build tooling is installed on the login node in the ReFrame phase below.
timer_end "install_deps"

# --- Phase 7: Install and run ReFrame suite ---
timer_start "reframe_suite"
echo "Installing and running ReFrame suite on the login node..."
juju ssh login/leader -i "${SSH_KEY_PATH}" "bash -s ${NODES} ${REPEATS} ${REPO_URL} ${REPO_REF}" << "REMOTE_EOF"
set -e
NUM_NODES="$1"
REPEATS="$2"
REPO_URL="$3"
REPO_REF="$4"

# Software necessary for building ReFrame test applications
sudo apt-get update
sudo apt-get -y install libopenmpi-dev openmpi-bin libopenblas-dev \
  build-essential python3-venv git

# Use shared file system for all tests
cd /nfs/home

# Install ReFrame
python3 -m venv reframe-venv
source reframe-venv/bin/activate
pip install ReFrame-HPC

# Clone the benchmark suite at the requested ref
rm -rf charmed-hpc-benchmarks
git clone "${REPO_URL}" charmed-hpc-benchmarks
cd charmed-hpc-benchmarks
git checkout "${REPO_REF}"
cd scripts/azure-scale

# num_tasks conventions:
#   *_per_core checks (HPL, HPCG): one MPI rank per core -> nodes * 120
#   collective checks (osu/imb allreduce): one rank per node -> nodes
#   concurrent checks: total task budget -> nodes * 120
CORE_TASKS=$((NUM_NODES * 120))
NODE_TASKS=$((NUM_NODES))

# Full-cluster checks (repeated N× via --repeats)
reframe \
  --config-file config/azure_scale_config.py \
  --checkpath ../../checks checks \
  --recursive \
  --run \
  --repeat "${REPEATS}" \
  -n 'charmed_osu_collective_check|imb_allreduce_check|hpl_cpu_check|hpcg_check|juju_agent_overhead|slurm_dispatch_latency' \
  --setvar charmed_osu_collective_check.num_tasks="${NODE_TASKS}" \
  --setvar imb_allreduce_check.num_tasks="${NODE_TASKS}" \
  --setvar hpl_cpu_check.num_tasks="${CORE_TASKS}" \
  --setvar hpcg_check.num_tasks="${CORE_TASKS}"

# Concurrent checks (run once, batch of 4 jobs each)
reframe \
  --config-file config/azure_scale_config.py \
  --checkpath ../../checks checks \
  --recursive \
  --run \
  -n 'hpl_concurrent_check|osu_concurrent_check' \
  --setvar hpl_concurrent_check.num_tasks="${CORE_TASKS}" \
  --setvar osu_concurrent_check.num_tasks="${CORE_TASKS}"
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
