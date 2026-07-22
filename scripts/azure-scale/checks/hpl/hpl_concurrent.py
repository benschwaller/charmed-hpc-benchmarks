# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Concurrent split-cluster HPL benchmark.

Demonstrates that the Slurm scheduler can run four large HPL jobs
simultaneously on separate quarters of the cluster. Submits four sbatch
jobs, each using a quarter of the compute nodes, verifies they overlapped
in time via sacct, and reports per-job performance plus a concurrency gap.

Requires at least 4 compute nodes' worth of tasks (num_tasks >= 4 *
CORES_PER_NODE); otherwise the four quarters cannot run on distinct nodes
and the test is skipped.
"""

import math
import os

import reframe as rfm
import reframe.utility.sanity as sn
from hpl import MEM_PER_NODE_GB, NB, _factor_grid, _hpl_dat, build_hpl_cpu

CORES_PER_NODE = 120


@rfm.simple_test
class hpl_concurrent_check(rfm.RunOnlyRegressionTest):
    """HPL concurrent split-cluster benchmark."""

    descr = "HPL concurrent split-cluster benchmark"
    valid_systems = ["*:login"]
    valid_prog_environs = ["builtin"]
    tags = {"benchmark", "hpl", "concurrent"}
    # num_tasks here is the *total* task budget (nodes * cores) used to derive
    # the four quarter-cluster jobs; the ReFrame job itself runs locally on login.
    num_tasks = variable(int, value=480)
    local = True
    time_limit = "1h"

    # Reuse the CPU HPL build so the concurrent jobs run the same binary.
    hpl_binary = fixture(build_hpl_cpu, scope="environment")

    @run_before("run")
    def skip_if_too_small(self):
        """Skip unless there are at least 4 nodes to split into four quarters."""
        total_nodes = self.num_tasks // CORES_PER_NODE
        self.skip_if(
            total_nodes < 4,
            f"concurrent split-cluster test needs >= 4 nodes, got {total_nodes}",
        )

    @run_before("run")
    def set_executable(self):
        """Generate per-job HPL.dat + sbatch scripts and drive all four jobs."""
        xhpl = os.path.join(self.hpl_binary.stagedir, "bin", "Linux_PII_FBLAS", "xhpl")

        quarter_nodes = max(1, (self.num_tasks // CORES_PER_NODE) // 4)
        quarter_tasks = quarter_nodes * CORES_PER_NODE

        total_mem_gb = quarter_nodes * MEM_PER_NODE_GB
        n = int(math.sqrt((total_mem_gb * 1e9) / 8.0))
        n = (n // NB) * NB
        p, q = _factor_grid(quarter_tasks)

        # Shared HPL.dat for all jobs (run from per-job subdirectories).
        with open(os.path.join(self.stagedir, "HPL.dat"), "w") as f:
            f.write(_hpl_dat(n, NB, p, q))

        job_names = ["hpl_conc_a", "hpl_conc_b", "hpl_conc_c", "hpl_conc_d"]
        job_scripts = []
        for job_name in job_names:
            script_path = os.path.join(self.stagedir, f"{job_name}.sh")
            job_scripts.append(script_path)
            with open(script_path, "w") as f:
                f.write(
                    f"""#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --partition=hb120rs-v3
#SBATCH --nodes={quarter_nodes}
#SBATCH --ntasks-per-node={CORES_PER_NODE}
#SBATCH --time=30:00
#SBATCH --output={job_name}.out
#SBATCH --error={job_name}.err

cd {self.stagedir}
srun {xhpl}
"""
                )

        self.executable = "bash"
        self.executable_opts = [
            os.path.join(self.stagedir, "drive_concurrent.sh"),
        ]

        driver = os.path.join(self.stagedir, "drive_concurrent.sh")
        with open(driver, "w") as f:
            f.write(
                f"""#!/bin/bash
set -u
cd {self.stagedir}
t_submit=$(date +%s)
job_a=$(sbatch --parsable {job_scripts[0]})
job_b=$(sbatch --parsable {job_scripts[1]})
job_c=$(sbatch --parsable {job_scripts[2]})
job_d=$(sbatch --parsable {job_scripts[3]})
echo "SUBMITTED $job_a $job_b $job_c $job_d $t_submit"
echo "Waiting for all four jobs to complete..."
while squeue -j "$job_a,$job_b,$job_c,$job_d" -h | grep -q .; do sleep 5; done
t_complete=$(date +%s)
echo "TOTAL_WALL $(echo "$t_complete - $t_submit" | bc -l)"

gflops_a=$(grep -oP "^W[RC]\\S+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\S+\\s+\\K\\S+" hpl_conc_a.out 2>/dev/null | tail -1 || echo 0)
gflops_b=$(grep -oP "^W[RC]\\S+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\S+\\s+\\K\\S+" hpl_conc_b.out 2>/dev/null | tail -1 || echo 0)
gflops_c=$(grep -oP "^W[RC]\\S+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\S+\\s+\\K\\S+" hpl_conc_c.out 2>/dev/null | tail -1 || echo 0)
gflops_d=$(grep -oP "^W[RC]\\S+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\S+\\s+\\K\\S+" hpl_conc_d.out 2>/dev/null | tail -1 || echo 0)
echo "JOB_A_GFLOPS ${{gflops_a:-0}}"
echo "JOB_B_GFLOPS ${{gflops_b:-0}}"
echo "JOB_C_GFLOPS ${{gflops_c:-0}}"
echo "JOB_D_GFLOPS ${{gflops_d:-0}}"

start_a=$(sacct -j "$job_a" --format=start -n -P | head -1)
end_a=$(sacct -j "$job_a" --format=end -n -P | head -1)
start_b=$(sacct -j "$job_b" --format=start -n -P | head -1)
end_b=$(sacct -j "$job_b" --format=end -n -P | head -1)
start_c=$(sacct -j "$job_c" --format=start -n -P | head -1)
end_c=$(sacct -j "$job_c" --format=end -n -P | head -1)
start_d=$(sacct -j "$job_d" --format=start -n -P | head -1)
end_d=$(sacct -j "$job_d" --format=end -n -P | head -1)

ts_a=$(date -d "$start_a" +%s 2>/dev/null || echo 0)
te_a=$(date -d "$end_a" +%s 2>/dev/null || echo 0)
ts_b=$(date -d "$start_b" +%s 2>/dev/null || echo 0)
te_b=$(date -d "$end_b" +%s 2>/dev/null || echo 0)
ts_c=$(date -d "$start_c" +%s 2>/dev/null || echo 0)
te_c=$(date -d "$end_c" +%s 2>/dev/null || echo 0)
ts_d=$(date -d "$start_d" +%s 2>/dev/null || echo 0)
te_d=$(date -d "$end_d" +%s 2>/dev/null || echo 0)

max_start=$(echo "$ts_a $ts_b $ts_c $ts_d" | tr ' ' '\\n' | sort -n | tail -1)
min_end=$(echo "$te_a $te_b $te_c $te_d" | tr ' ' '\\n' | sort -n | head -1)
min_start=$(echo "$ts_a $ts_b $ts_c $ts_d" | tr ' ' '\\n' | sort -n | head -1)

if [ "$max_start" -lt "$min_end" ]; then
  echo "IS_CONCURRENT true"
  gap=$(( max_start - min_start ))
  echo "CONCURRENCY_GAP $gap"
else
  echo "IS_CONCURRENT false"
  echo "CONCURRENCY_GAP 999"
fi
echo "=== DONE ==="
"""
            )

    @sanity_function
    def assert_completed(self):
        """Validate that the driver ran to completion and jobs were concurrent."""
        return sn.all(
            [
                sn.assert_found(r"=== DONE ===", self.stdout),
                sn.assert_found(r"IS_CONCURRENT true", self.stdout),
            ]
        )

    @performance_function("Gflops")
    def job_a_gflops(self):
        """Extract job A Gflops."""
        return sn.extractsingle(r"JOB_A_GFLOPS (?P<val>[0-9.eE+-]+)", self.stdout, "val", float)

    @performance_function("Gflops")
    def job_b_gflops(self):
        """Extract job B Gflops."""
        return sn.extractsingle(r"JOB_B_GFLOPS (?P<val>[0-9.eE+-]+)", self.stdout, "val", float)

    @performance_function("Gflops")
    def job_c_gflops(self):
        """Extract job C Gflops."""
        return sn.extractsingle(r"JOB_C_GFLOPS (?P<val>[0-9.eE+-]+)", self.stdout, "val", float)

    @performance_function("Gflops")
    def job_d_gflops(self):
        """Extract job D Gflops."""
        return sn.extractsingle(r"JOB_D_GFLOPS (?P<val>[0-9.eE+-]+)", self.stdout, "val", float)

    @performance_function("s")
    def total_wall_seconds(self):
        """Extract total wall time in seconds."""
        return sn.extractsingle(r"TOTAL_WALL (?P<val>[0-9.eE+-]+)", self.stdout, "val", float)

    @performance_function("s")
    def concurrency_gap_seconds(self):
        """Extract concurrency gap in seconds."""
        return sn.extractsingle(r"CONCURRENCY_GAP (?P<val>[0-9.eE+-]+)", self.stdout, "val", float)
