# Azure-Scale Charmed-HPC Benchmarking

Benchmarking infrastructure for measuring Charmed-HPC cluster instantiation
time, Juju/charm overhead, and multi-node CPU HPC workload performance at
scale on Microsoft Azure.

## Overview

This directory contains a self-contained benchmarking suite that answers two
questions:

1. **How long does a Charmed-HPC cluster take to instantiate, and what is
   the overhead of the Juju/charm infrastructure?**
2. **How does that compare to a raw VM deployment with manually-installed
   Slurm (no Juju)?**

To answer these, the suite deploys a Slurm cluster on Azure via Juju, runs a
multi-node CPU ReFrame benchmark suite, tears it down, then deploys raw Azure
VMs with manual Slurm, runs the same benchmarks, and produces a side-by-side
comparison report.

> **CPU-only.** This suite targets the RDMA-enabled `Standard_HB120rs_v3` CPU
> partition. There is no GPU partition or GPU benchmark here; single-node GPU
> tests live in the top-level `scripts/azure` pipeline.

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
./run_all.sh --nodes 64 --ubuntu 24.04 --repeats 5

# Or run each phase separately:
./run_azure_scale.sh --nodes 64 --ubuntu 24.04 --repeats 5
./baseline/run_baseline.sh --nodes 64 --ubuntu 24.04 --repeats 5
python3 compare.py --juju juju_timing_report.json --baseline baseline_timing_report.json
```

## CLI Parameters

| Parameter | Default | Values | Description |
|-----------|---------|--------|-------------|
| `--nodes` | 2 | Any positive integer (64+ for large scale) | Number of HB120rs_v3 CPU compute nodes |
| `--ubuntu` | 24.04 | `24.04` / `26.04` | Ubuntu LTS series |
| `--repeats` | 5 | Any positive integer (min 5 recommended) | Repetitions per full-cluster benchmark |
| `--repo` | `canonical/charmed-hpc-benchmarks` | Any git URL | Repo cloned on the login node to source the checks |
| `--ref` | `main` | Any branch/tag/commit | Git ref checked out on the login node |

> **Testing a feature branch.** The login node clones the suite from git. To
> run checks that are not yet merged, pass `--repo <your-fork-url> --ref
> <your-branch>`. This is required because the login node does not have access
> to your local working tree.

## ReFrame Checks

The pipeline runs checks from two directories:
- `../../checks` — the existing top-level charmed-hpc-benchmarks checks
- `checks/` — checks specific to this scaling suite

Point-to-point and single-node checks (OSU pt2pt, IMB pingpong, gpu_burn, fio,
slurmdbd, slurmrestd) are **excluded** via ReFrame's `-n` name filter — this
suite focuses on multi-node / cluster-scaling benchmarks and infrastructure
overhead.

### Full-cluster tests (each repeated N× via `--repeats`)

| Check | Source | Tasks used | What it measures |
|-------|--------|-----------|------------------|
| `charmed_osu_collective_check` | `../../checks` | 1 rank/node | OSU MPI collective (allreduce, alltoall) |
| `imb_allreduce_check` | `../../checks` | 1 rank/node | Intel MPI allreduce latency |
| `hpl_cpu_check` | `checks/` | 1 rank/core | Peak FLOPS, full cluster |
| `hpcg_check` | `checks/` | 1 rank/core | Memory bandwidth / latency stress |
| `juju_agent_overhead` | `checks/` | 1 per partition | jujud/snapd CPU/RAM, aggregate Juju overhead, disk usage |
| `slurm_dispatch_latency` | `checks/` | 1 (login → compute) | srun/sbatch/sinfo/scontrol latency |

### Concurrent split-cluster tests (single run, requires `--nodes >= 4` for HPL, `--nodes >= 8` for OSU)

| Check | Source | Layout | What it demonstrates |
|-------|--------|--------|----------------------|
| `hpl_concurrent_check` | `checks/` | Four HPL jobs on N/4 nodes each, simultaneously | Scheduler concurrency + full performance under load |
| `osu_concurrent_check` | `checks/` | Four osu_bw jobs on separate node pairs, simultaneously | RDMA network handles concurrent traffic |

Concurrent checks self-skip when fewer nodes are available than required.
They run once regardless of `--repeats` (they submit their own sbatch jobs
internally).

### `num_tasks` conventions

The orchestrator computes task counts from `--nodes` and passes them via
`--setvar`:

- **Per-core checks** (`hpl_cpu_check`, `hpcg_check`, and the concurrent
  checks' internal task budget): `nodes × 120`.
- **Collective checks** (`charmed_osu_collective_check`, `imb_allreduce_check`,
  which pin `num_tasks_per_node = 1`): `nodes` (one rank per node).

### Automatic problem sizing

HPL and HPCG generate their input files at runtime.

- **HPL** (`checks/hpl/hpl.py`): the P×Q grid is factored from `num_tasks` to
  be as square as possible; problem size `N = sqrt(nodes × 400GB × 1e9 / 8)`
  rounded to a multiple of `NB=384`. The result-line regex anchors on the
  stable HPL column layout rather than a hardcoded run tag.
- **HPCG** (`checks/hpcg/hpcg.py`): fixed per-rank local dimensions
  (104³); the global problem grows with rank count. The `hpcg.dat` uses the
  correct `nx ny nz` single-line format.

Both `hpl_cpu_check` and `hpcg_check` build their binary via a ReFrame build
fixture (`build_hpl_cpu`, `build_hpcg`) and execute it from the fixture's
stage directory — the binaries are not assumed to be on `PATH`.

## Workstreams

### 1. Per-Phase Deployment Timing + Application Readiness Timeline

**Files:** `timing_utils.sh`, `run_azure_scale.sh`

Instruments every phase of the deployment pipeline with structured timing.
During the wait-for-active loop, per-application readiness is tracked — the
first time each application flips to `active` is recorded. All numeric values
are normalized to valid JSON before being written.

**Phases timed:**

| Phase | Description |
|-------|-------------|
| `bootstrap` | `juju bootstrap azure` controller creation |
| `tofu_apply` | `tofu init && tofu apply` (Azure provisioning + charm deployment) |
| `wait_active` | Polling loop until all applications report `active` |
| `node_configured` | `node-configured` action on all CPU compute units (parallelized) |
| `install_deps` | Placeholder phase; build tooling is installed in the ReFrame phase |
| `reframe_suite` | ReFrame installation + benchmark suite execution |
| `copy_results` | `scp` of perflogs and output directories |
| `teardown_tofu` | `tofu destroy` (with retry logic) |
| `teardown_juju` | `juju destroy-controller` |

**Output:** `juju_timing_report.json` / `baseline_timing_report.json`:

```json
{
  "run_id": "2026-07-15T10:30:00Z",
  "deployment_type": "juju",
  "phases": {
    "bootstrap": {"start": ..., "end": ..., "duration_seconds": 85.3},
    "...": {}
  },
  "application_readiness": {
    "mysql": {"seconds_from_wait_start": 45.2}
  },
  "totals": {
    "spinup_seconds": 0.0,
    "teardown_seconds": 0.0,
    "total_seconds": 0.0
  }
}
```

### 2. Infrastructure Overhead Check

**File:** `checks/infrastructure/juju_overhead.py`

Measures the resource footprint of the Juju/charm infrastructure. Processes
are matched by command-line pattern (jujud runs as `jujud-machine-N`), and the
collection logic is written to a script file to avoid fragile nested quoting.
On the baseline pipeline the jujud/aggregate metrics are zero; snapd is
reported separately because it also exists on non-Juju hosts.

| Metric | Unit | Description |
|--------|------|-------------|
| `jujud_cpu_percent` / `jujud_rss_mb` / `jujud_threads` | %, MB, count | jujud agent footprint |
| `snapd_cpu_percent` / `snapd_rss_mb` | %, MB | snapd (present on baseline too) |
| `total_juju_cpu_percent` / `total_juju_rss_mb` | %, MB | Aggregate of all Juju-related processes |
| `juju_disk_mb` / `juju_log_disk_mb` | MB | `/var/lib/juju` and `/var/log/juju` usage |

### 3. Slurm Scheduling Latency Check

**File:** `checks/infrastructure/slurm_latency.py`

Reference values are permissive upper bounds intended to catch gross
regressions (e.g. a wedged controller), not tight performance targets.

| Metric | Unit | Description |
|--------|------|-------------|
| `srun_latency` | s | `srun hostname` dispatch time |
| `sinfo_latency` | s | `sinfo` query time |
| `scontrol_latency` | s | `scontrol show nodes` time |
| `sbatch_latency` | s | Time from `sbatch` to job start |

### 4. HPL + HPCG Benchmarks

**Files:** `checks/hpl/hpl.py`, `checks/hpl/hpl_concurrent.py`,
`checks/hpl/src/Make.Linux_PII_FBLAS`, `checks/hpcg/hpcg.py`

- **HPL (CPU)** — built from netlib source against OpenBLAS on `hb120rs-v3`,
  scaling to N nodes with an auto-calculated P×Q grid.
- **HPCG** — built from source with MPI on `hb120rs-v3`, scaling to N nodes.
- **Concurrent split-cluster** — four HPL jobs and four OSU bandwidth jobs
  submitted simultaneously, each using a quarter of the cluster / separate
  node pairs, with overlap confirmed via `sacct`.

| Metric | Description |
|--------|-------------|
| `job_a_gflops` / `job_b_gflops` / `job_c_gflops` / `job_d_gflops` | Per-quarter-cluster HPL performance |
| `total_wall_seconds` | Wall-clock for all four to complete |
| `concurrency_gap_seconds` | Max gap between job starts (near-zero when concurrent) |

### 5. Raw VM Baseline Comparison

**Files:** `baseline/run_baseline.sh`, `baseline/main.tf`,
`baseline/setup_slurm.sh`

Deploys the same Azure CPU VMs **without Juju/charms**, manually installs
Slurm via bash, and runs the same ReFrame suite.

**`baseline/setup_slurm.sh`** installs:
1. Munge + Slurm packages on login node (shared key for Slurm auth)
2. Munge + Slurm + MPI packages on compute nodes (key distributed from login)
3. NFS — export `/nfs/home` from login, mount on compute nodes
4. Slurm configuration — slurmctld on login, slurmd on compute nodes (cgroup v2,
   no slurmdbd; accounting text records are sufficient for `sacct` concurrency
   checks)
5. ReFrame — venv on the shared filesystem

### 6. Comparison Reporter

**File:** `compare.py`

Reads both timing report JSON files and optionally perflogs, producing a
markdown comparison report with phase-by-phase timing, per-app readiness, and
benchmark results. Handles legitimately-zero values correctly.

**Output:** `comparison_report.md`

## Ubuntu Version Support

| Version | Codename | Status |
|---------|----------|--------|
| 24.04 | Noble Numbat | Default; all charms support it |
| 26.04 | (latest LTS) | **Verify charmed-hpc charm compatibility first** |

The Juju plan sets the model's `default-series` to the Ubuntu series version
(e.g., `24.04`). Individual
charms may still pin their own supported base, so `--ubuntu 26.04` only takes
effect where the charms actually support it. The baseline plan selects the
matching Azure image (`ubuntu-24_04-lts` / `ubuntu-26_04-lts`, SKU `server`).
`UBUNTU_SERIES` is also exported for the ReFrame config's description string.

## Scaling to 64+ Nodes

### What scales automatically

- **Wait-for-active loop** — polls `juju status --format=json` generically
- **Per-app readiness tracking** — JSON parsing is app-agnostic
- **`node-configured` action** — parallelized via `xargs -P 0`
- **HPL/HPCG problem sizing** — `num_tasks` set via `--setvar`; P×Q grid and
  problem size auto-calculated from node count and per-node memory

### Azure quota requirements

64× `Standard_HB120rs_v3` = 7,680 vCPUs. Default Azure quota is usually
10–100 vCPUs for HB-series. Request a quota increase via the Azure portal.

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
│   └── azure_scale_config.py          # ReFrame config (CPU-only)
├── checks/
│   ├── infrastructure/
│   │   ├── juju_overhead.py           # jujud/snapd CPU/RAM/disk/threads
│   │   └── slurm_latency.py           # srun/sbatch/sinfo/scontrol latency
│   ├── hpl/
│   │   ├── hpl.py                     # CPU HPL (OpenBLAS) build + run
│   │   ├── hpl_concurrent.py          # Split-cluster concurrent HPL
│   │   └── src/
│   │       └── Make.Linux_PII_FBLAS   # HPL makefile for OpenBLAS
│   ├── hpcg/
│   │   └── hpcg.py                    # Full-cluster HPCG
│   └── mpi/
│       └── osu_concurrent.py          # Concurrent OSU bandwidth
├── templates/
│   ├── HPL.dat.template               # Reference for HPL input format
│   └── hpcg.dat.template              # Reference for HPCG input format
└── baseline/
    ├── run_baseline.sh                # Raw VM pipeline (same CLI flags)
    ├── main.tf                        # Raw Azure CPU VMs, no Juju
    └── setup_slurm.sh                 # Bash manual Slurm + MPI install
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

This directory is separate from the existing `scripts/azure/` pipeline. The
existing pipeline runs a fixed 2-node cluster with the full single-node check
suite (including GPU) and is untouched by this work. This pipeline runs an
expanded multi-node CPU check suite from both `../../checks` and `checks/`
using a self-contained ReFrame config.
