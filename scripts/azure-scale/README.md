# Azure-Scale Charmed-HPC Benchmarking

Comprehensive benchmarking infrastructure for measuring Charmed-HPC cluster
instantiation time, Juju/charm overhead, and HPC workload performance at scale
on Microsoft Azure.

## Overview

This directory contains a self-contained benchmarking suite that answers two
questions:

1. **How long does a Charmed-HPC cluster take to instantiate, and what is
   the overhead of the Juju/charm infrastructure?**
2. **How does that compare to a raw VM deployment with manually-installed
   Slurm (no Juju)?**

To answer these, the suite deploys a Slurm cluster on Azure via Juju, runs
a comprehensive ReFrame benchmark suite, tears it down, then deploys raw
Azure VMs with manual Slurm, runs the same benchmarks, and produces a
side-by-side comparison report.

## Architecture

```
   run_all.sh
   (wrapper)
      │
      ├── 1. run_azure_scale.sh          ──► juju_timing_report.json
      │      juju bootstrap + tofu apply       perflogs/
      │      + ReFrame suite
      │      + teardown
      │
      ├── 2. baseline/run_baseline.sh     ──► baseline_timing_report.json
      │      raw VMs via tofu                  baseline_perflogs/
      │      + setup_slurm.sh
      │      + ReFrame suite
      │      + teardown
      │
      └── 3. compare.py                   ──► comparison_report.md
             reads both timing reports
             + both perflogs dirs
```

VMs are never held simultaneously — the Juju cluster is fully torn down
before the baseline VMs are provisioned, minimizing Azure quota pressure.

## Quick Start

```bash
# Full pipeline: Juju → teardown → baseline → teardown → compare
./run_all.sh --nodes 64 --gpu false --ubuntu 24.04 --repeats 5

# Or run each phase separately:
./run_azure_scale.sh --nodes 64 --gpu false --ubuntu 24.04 --repeats 5
./baseline/run_baseline.sh --nodes 64 --gpu false --ubuntu 24.04 --repeats 5
python3 compare.py --juju juju_timing_report.json --baseline baseline_timing_report.json
```

## CLI Parameters

| Parameter | Default | Values | Description |
|-----------|---------|--------|-------------|
| `--nodes` | 2 | Any positive integer (64+ for large scale) | Number of HB120rs_v3 CPU compute nodes |
| `--gpu` | true | `true` / `false` | Whether to deploy the nc4as-t4-v3 GPU partition |
| `--gpu_nodes` | 1 | Any positive integer | Number of NC4as_T4_v3 GPU nodes (for multi-node GPU HPL with NCCL) |
| `--ubuntu` | 24.04 | `24.04` / `26.04` | Ubuntu LTS series |
| `--repeats` | 5 | Any positive integer (min 5 recommended) | Repetitions per full-cluster benchmark |

## ReFrame Checks

The pipeline runs checks from two directories:
- `../../checks` — the existing top-level charmed-hpc-benchmarks checks
- `checks/` — new checks specific to this scaling suite

Point-to-point and single-node checks (OSU pt2pt, IMB pingpong, gpu_burn, fio,
slurmdbd, slurmrestd) are **excluded** via ReFrame's `-n` name filter — this
suite focuses on multi-node / cluster-scaling benchmarks and infrastructure
overhead.

### Full-cluster tests (each repeated N× via `--repeats`)

| Check | Source | Nodes used | What it measures |
|-------|--------|-----------|------------------|
| `charmed_osu_collective_check` | `../../checks` | All N | OSU MPI collective (allreduce, alltoall) |
| `imb_allreduce_check` | `../../checks` | All N | Intel MPI allreduce latency |
| `hpl_cpu_check` | `checks/` | All N | Peak FLOPS, full cluster |
| `hpcg_check` | `checks/` | All N | Memory bandwidth / latency stress |
| `hpl_gpu_check` | `checks/` | All GPU nodes | Multi-node GPU LINPACK via NCCL |
| `juju_agent_overhead` | `checks/` | 1 per partition | jujud CPU/RAM/disk/threads |
| `slurm_dispatch_latency` | `checks/` | 1 (login → compute) | srun/sbatch/sinfo/scontrol latency |

### Concurrent split-cluster tests (single run, requires `--nodes >= 4`)

| Check | Source | Layout | What it demonstrates |
|-------|--------|--------|----------------------|
| `hpl_concurrent_check` | `checks/` | Two HPL jobs on N/2 nodes each, simultaneously | Scheduler concurrency + full performance under load |
| `osu_concurrent_check` | `checks/` | Two osu_bw jobs on separate node pairs, simultaneously | RDMA network handles concurrent traffic |

Concurrent checks always run once regardless of `--repeats`.

### Check selection

The ReFrame invocation uses `-n` to select only multi-node and infrastructure
checks:

```bash
reframe ... \
  -n 'charmed_osu_collective_check|imb_allreduce_check|hpl_cpu_check|hpl_gpu_check|hpcg_check|hpl_concurrent_check|osu_concurrent_check|juju_agent_overhead|slurm_dispatch_latency' \
  --setvar charmed_osu_collective_check.num_tasks="${NUM_TASKS}" \
  --setvar imb_allreduce_check.num_tasks="${NUM_TASKS}" \
  --setvar hpl_cpu_check.num_tasks="${NUM_TASKS}" \
  --setvar hpcg_check.num_tasks="${NUM_TASKS}" \
  --setvar hpl_concurrent_check.num_tasks="${HALF_TASKS}" \
  --setvar hpl_gpu_check.num_tasks="${GPU_NODES}"
```

### Automatic problem sizing

HPL and HPCG generate their `.dat` input files at runtime based on `num_tasks`
and available memory per node. The P×Q process grid is factored from
`num_tasks` to be as square as possible. For HPL:

- **CPU**: N = `sqrt(nodes × 400GB × 1e9 / 8)`, NB=384
- **GPU**: N = `sqrt(gpu_nodes × 14GB × 1e9 / 8)`, NB=256

The `num_tasks` values are computed from `--nodes` and `--gpu_nodes` in the
orchestrator scripts and passed via `--setvar`.

## Workstreams

### 1. Per-Phase Deployment Timing + Application Readiness Timeline

**Files:** `timing_utils.sh`, `run_azure_scale.sh`

Instruments every phase of the deployment pipeline with structured timing.
During the wait-for-active loop, per-application readiness is tracked — the
first time each application flips to `active` is recorded.

**Phases timed:**

| Phase | Description |
|-------|-------------|
| `bootstrap` | `juju bootstrap azure` controller creation |
| `tofu_apply` | `tofu init && tofu apply` (Azure provisioning + charm deployment) |
| `wait_active` | Polling loop until all applications report `active` |
| `node_configured_gpu` | `node-configured` action on GPU partition |
| `node_configured_cpu` | `node-configured` action on all CPU compute units (parallelized) |
| `install_deps` | Package installation (libcublas, NCCL, build tools) |
| `reframe_suite` | ReFrame installation + full benchmark suite execution |
| `copy_results` | `scp` of perflogs and output directories |
| `teardown_tofu` | `tofu destroy` (with retry logic) |
| `teardown_juju` | `juju destroy-controller` |

**Output:** `juju_timing_report.json` (Juju) / `baseline_timing_report.json`
(baseline) with the following schema:

```json
{
  "run_id": "2026-07-15T10:30:00Z",
  "deployment_type": "juju",
  "phases": {
    "bootstrap": {"start": ..., "end": ..., "duration_seconds": 85.3},
    ...
  },
  "application_readiness": {
    "mysql": {"seconds_from_wait_start": 45.2},
    ...
  },
  "totals": {
    "spinup_seconds": ...,
    "teardown_seconds": ...,
    "total_seconds": ...
  }
}
```

### 2. Juju Agent Runtime Overhead Check

**File:** `checks/infrastructure/juju_overhead.py`

Measures the resource footprint of `jujud` on each partition.

| Metric | Unit | Description |
|--------|------|-------------|
| `jujud_cpu_percent` | % | CPU usage of jujud process |
| `jujud_rss_mb` | MB | Resident memory of jujud |
| `juju_disk_mb` | MB | Disk usage of `/var/lib/juju` |
| `jujud_threads` | count | Thread count of jujud |

### 3. Slurm Scheduling Latency Check

**File:** `checks/infrastructure/slurm_latency.py`

| Metric | Unit | Reference | Description |
|--------|------|-----------|-------------|
| `srun_latency` | s | 2.0 | `srun hostname` dispatch time |
| `sinfo_latency` | s | 0.5 | `sinfo` query time |
| `scontrol_latency` | s | 0.5 | `scontrol show nodes` time |
| `sbatch_latency` | s | 3.0 | Time from `sbatch` to job start |

### 4. HPL + HPCG Benchmarks

**Files:** `checks/hpl/hpl.py`, `checks/hpl/hpl_concurrent.py`,
`checks/hpl/src/Make.Linux_PII_FBLAS`, `checks/hpcg/hpcg.py`

#### HPL (High-Performance Linpack)

| Variant | Partition | Build Method | Scales? |
|---------|-----------|--------------|---------|
| CPU HPL | `hb120rs-v3` | netlib source + OpenBLAS | Yes — P×Q auto-calculated from `num_tasks` |
| GPU HPL | `nc4as-t4-v3` | NVIDIA pre-built binary + NCCL | Yes — multi-node over Ethernet (no RDMA) |

GPU HPL uses NCCL for inter-GPU communication. The NC4as_T4_v3 instances have
no RDMA, so NCCL runs over TCP (`NCCL_NET=Socket`, `NCCL_SOCKET_IFNAME=eth0`).

#### HPCG

Runs on the CPU partition, scales to N nodes.

#### Concurrent Split-Cluster Tests

Two HPL jobs and two OSU bandwidth jobs submitted simultaneously to Slurm,
each using half the cluster. Verifies true concurrency via `sacct`.

| Metric | Description |
|--------|-------------|
| `job_a_gflops` / `job_b_gflops` | Per-half-cluster HPL performance |
| `total_wall_seconds` | Wall-clock for both to complete |
| `concurrency_gap_seconds` | Gap between job starts (should be near-zero) |

### 5. Raw VM Baseline Comparison

**Files:** `baseline/run_baseline.sh`, `baseline/main.tf`,
`baseline/setup_slurm.sh`

Deploys the same Azure VMs **without Juju/charms**, manually installs Slurm
via bash scripts, and runs the same ReFrame suite.

**`baseline/setup_slurm.sh`** manually installs:
1. Munge on all nodes (shared key for Slurm auth)
2. Slurm — slurmctld on login, slurmd on compute nodes
3. NFS — export `/nfs/home` from login, mount on compute nodes
4. MPI — OpenMPI on all nodes
5. CUDA + NCCL — toolkit + libcublas + libnccl on GPU nodes (if enabled)
6. ReFrame — venv + clone repo + run same check suite

### 6. Comparison Reporter

**File:** `compare.py`

Reads both timing report JSON files and optionally perflogs, producing a
markdown comparison report with phase-by-phase timing, per-app readiness,
and benchmark results.

**Output:** `comparison_report.md`

## GPU Toggle

When `--gpu false`:
- The Terraform plan creates 0 GPU VMs
- The ReFrame config omits the `nc4as-t4-v3` partition
- GPU-specific checks are automatically skipped (target partition doesn't exist)
- Only CPU HPL (from source with OpenBLAS) runs on `hb120rs-v3`

When `--gpu true` (default):
- `--gpu_nodes` controls how many NC4as_T4_v3 nodes to deploy (default: 1)
- GPU HPL runs with NCCL across all GPU nodes
- NCCL is installed via `apt-get install libnccl2 libnccl-dev`

## Ubuntu Version Support

| Version | Codename | Status |
|---------|----------|--------|
| 24.04 | Noble Numbat | Default; proven stability; all charms support it |
| 26.04 | (latest LTS) | Supported; verify charmed-hpc charm compatibility first |

The `UBUNTU_SERIES` environment variable is exported by the orchestrator
scripts and consumed by the ReFrame config and Terraform plan at runtime.

## Scaling to 64+ Nodes

### What scales automatically

- **Wait-for-active loop** — polls `juju status --format=json` generically
- **Per-app readiness tracking** — JSON parsing is app-agnostic
- **`node-configured` action** — parallelized via `xargs -P 0`
- **HPL/HPCG problem sizing** — `num_tasks` set via `--setvar`, P×Q grid
  and problem size N auto-calculated from node count and available memory
- **GPU HPL** — `num_tasks` set to `--gpu_nodes`, NCCL handles multi-node

### Azure quota requirements

64× `Standard_HB120rs_v3` = 7,680 vCPUs. Default Azure quota is usually
10-100 vCPUs for HB-series. Request a quota increase via the Azure portal.

## Directory Structure

```
scripts/azure-scale/
├── README.md                          # This file
├── run_all.sh                         # Wrapper: Juju → baseline → compare
├── run_azure_scale.sh                 # Juju pipeline orchestrator
├── main.tf                            # Parameterized Terraform (Juju + charms)
├── timing_utils.sh                    # Shared bash timing library
├── compare.py                         # Side-by-side comparison reporter
├── config/
│   └── azure_scale_config.py          # ReFrame config (GPU toggle via env var)
├── checks/
│   ├── infrastructure/
│   │   ├── juju_overhead.py           # jujud CPU/RAM/disk/threads
│   │   └── slurm_latency.py           # srun/sbatch/sinfo/scontrol latency
│   ├── hpl/
│   │   ├── hpl.py                     # CPU HPL (OpenBLAS) + GPU HPL (NCCL)
│   │   ├── hpl_concurrent.py          # Split-cluster concurrent HPL
│   │   └── src/
│   │       └── Make.Linux_PII_FBLAS   # HPL makefile for OpenBLAS
│   ├── hpcg/
│   │   └── hpcg.py                    # Full-cluster HPCG
│   └── mpi/
│       └── osu_concurrent.py          # Concurrent OSU bandwidth
├── templates/
│   ├── HPL.dat.template               # Reference for HPL input format
│   └── hpcg.dat.template             # Reference for HPCG input format
└── baseline/
    ├── run_baseline.sh                # Raw VM pipeline (same CLI flags)
    ├── main.tf                        # Raw Azure VMs, no Juju
    └── setup_slurm.sh                 # Bash manual Slurm + MPI + NCCL install
```

## Prerequisites

### Tools

- **Juju CLI** — for bootstrapping controllers and managing models
- **OpenTofu** (or Terraform) — for infrastructure provisioning
- **jq** — for JSON parsing in timing utilities
- **bc** — for floating-point arithmetic in timing utilities
- **Python 3** — for the comparison reporter
- **SSH key pair** — for accessing raw VMs in the baseline pipeline

### Environment

```bash
export ARM_SUBSCRIPTION_ID=<your-azure-subscription-id>
juju add-credential azure
```

### For the baseline pipeline

```bash
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa
```

## Outputs

| File | Description |
|------|-------------|
| `juju_timing_report.json` | Per-phase timing + per-app readiness (Juju pipeline) |
| `baseline_timing_report.json` | Per-phase timing (baseline pipeline) |
| `comparison_report.md` | Side-by-side markdown comparison |
| `perflogs/` | Juju ReFrame performance logs |
| `baseline_perflogs/` | Baseline ReFrame performance logs |
| `output/` | Juju ReFrame raw test output |
| `baseline_output/` | Baseline ReFrame raw test output |

## Existing Azure Pipeline

This directory is separate from the existing `scripts/azure/` pipeline.
The existing pipeline runs a fixed 2-node cluster with 27 ReFrame checks
and is untouched by this work. This pipeline runs an expanded multi-node
check suite from both `../../checks` and `checks/` using a self-contained
ReFrame config.
