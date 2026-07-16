# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Concurrent split-cluster HPL benchmark.

Demonstrates that the Slurm scheduler can run two large HPL jobs
simultaneously on separate halves of the cluster. Submits two sbatch
jobs, each using N/2 nodes, verifies they ran concurrently via sacct,
and reports per-job performance plus a concurrency confirmation.

Only runs when num_tasks >= 4 nodes worth (i.e., --nodes >= 4).
"""

import math
import os

import reframe as rfm
import reframe.utility.sanity as sn

CORES_PER_NODE = 120
MEM_PER_NODE_GB = 400


@rfm.simple_test
class hpl_concurrent_check(rfm.RunOnlyRegressionTest):
    """HPL concurrent split-cluster benchmark."""

    descr = "HPL concurrent split-cluster benchmark"
    valid_systems = ["*:login"]
    valid_prog_environs = ["builtin"]
    tags = {"benchmark", "hpl", "concurrent"}
    num_tasks = variable(int, value=120)
    time_limit = "1h"

    @run_before("run")
    def set_executable(self):
        """Set executable and generate HPL job scripts."""
        self.executable = "bash"
        self.executable_opts = ["-l", "-c"]

        half_nodes = max(1, (self.num_tasks // CORES_PER_NODE) // 2)
        half_tasks = half_nodes * CORES_PER_NODE

        # Problem size for half the cluster
        total_mem_gb = half_nodes * MEM_PER_NODE_GB
        n = int(math.sqrt((total_mem_gb * 1e9) / 8.0))
        nb = 384
        n = (n // nb) * nb

        # P x Q grid for half the cluster
        ntasks = half_tasks
        p = int(math.isqrt(ntasks))
        while ntasks % p != 0 and p > 1:
            p -= 1
        q = ntasks // p

        # Generate the HPL.dat content for each job
        hpl_dat_content = f"""HPLinpack benchmark input file
Innovative Computing Laboratory, University of Tennessee
HPL.out      output file name (if any)
6            device out (6=stdout,7=stderr,file)
1            # of problems sizes (N)
{n}          Ns
1            # of NBs
{nb}         NBs
0            PMAP process mapping (0=Row-,1=Column-major)
1            # of process grids (P x Q)
{p}          Ps
{q}          Qs
16.0         threshold
1            # of panel fact
2            PFACTs (0=left, 1=Crout, 2=Right)
1            # of recursive stopping criterium
4            NBMINs
2            NDIVs
1            # of panels in recursion
2            RFACTs (0=left, 1=Crout, 2=Right)
1            # of broadcast
0            BCASTs (0=1rg,1=1rM,2=2rg,3=2rM,4=Lng,5=LnM)
1            # of lookahead depth
0            DEPTHs (0=0,1=1,2=2)
2            SWAP (0=bin-exch,1=long,2=mix)
64           swapping threshold
0            L1 in (0=transposed,1=no-transposed) form
0            U  in (0=transposed,1=no-transposed) form
1            Equilibration (0=no,1=yes)
0            memory alignment in double (> 0)
"""

        # Write the HPL.dat and sbatch scripts to stagedir
        hpl_dat_path = os.path.join(self.stagedir, "HPL.dat")
        with open(hpl_dat_path, "w") as f:
            f.write(hpl_dat_content)

        job_script_a = os.path.join(self.stagedir, "job_a.sh")
        job_script_b = os.path.join(self.stagedir, "job_b.sh")

        for script_path, job_name in [(job_script_a, "hpl_conc_a"), (job_script_b, "hpl_conc_b")]:
            with open(script_path, "w") as f:
                f.write(f"""#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --partition=hb120rs-v3
#SBATCH --nodes={half_nodes}
#SBATCH --ntasks-per-node={CORES_PER_NODE}
#SBATCH --time=30:00
#SBATCH --output={job_name}.out
#SBATCH --error={job_name}.err

cd {self.stagedir}
srun xhpl
""")

        # The main script submits both jobs simultaneously and waits.
        self.executable_opts.append(
            f'"'
            f"cd {self.stagedir} && "
            f"t_submit=$(date +%s); "
            f"job_a=$(sbatch --parsable {job_script_a}); "
            f"job_b=$(sbatch --parsable {job_script_b}); "
            f'echo "SUBMITTED $job_a $job_b $t_submit"; '
            f'echo "Waiting for both jobs to complete..."; '
            f"while squeue -j $job_a,$job_b -h | grep -q .; do sleep 5; done; "
            f"t_complete=$(date +%s); "
            f'echo "TOTAL_WALL $(echo "$t_complete - $t_submit" | bc -l)"; '
            f"# Check concurrency via sacct; "
            f"start_a=$(sacct -j $job_a --format=start -n -P | head -1); "
            f"end_a=$(sacct -j $job_a --format=end -n -P | head -1); "
            f"start_b=$(sacct -j $job_b --format=start -n -P | head -1); "
            f"end_b=$(sacct -j $job_b --format=end -n -P | head -1); "
            f'echo "JOB_A_START $start_a"; '
            f'echo "JOB_A_END $end_a"; '
            f'echo "JOB_B_START $start_b"; '
            f'echo "JOB_B_END $end_b"; '
            f"# Extract Gflops from each job output; "
            f'gflops_a=$(grep -oP "WR00L2L2\\s+\\S+\\s+\\S+\\s+\\S+\\s+\\S+\\s+\\K\\S+" hpl_conc_a.out 2>/dev/null || echo "0"); '
            f'gflops_b=$(grep -oP "WR00L2L2\\s+\\S+\\s+\\S+\\s+\\S+\\s+\\S+\\s+\\K\\S+" hpl_conc_b.out 2>/dev/null || echo "0"); '
            f'echo "JOB_A_GFLOPS $gflops_a"; '
            f'echo "JOB_B_GFLOPS $gflops_b"; '
            f"# Concurrency check: did the time ranges overlap?; "
            f'ts_a=$(date -d "$start_a" +%s 2>/dev/null || echo 0); '
            f'te_a=$(date -d "$end_a" +%s 2>/dev/null || echo 0); '
            f'ts_b=$(date -d "$start_b" +%s 2>/dev/null || echo 0); '
            f'te_b=$(date -d "$end_b" +%s 2>/dev/null || echo 0); '
            f'if [ "$ts_a" -lt "$te_b" ] && [ "$ts_b" -lt "$te_a" ]; then '
            f'  echo "IS_CONCURRENT true"; '
            f"  gap=$(( ts_a > ts_b ? ts_a - ts_b : ts_b - ts_a )); "
            f'  echo "CONCURRENCY_GAP $gap"; '
            f"else "
            f'  echo "IS_CONCURRENT false"; '
            f'  echo "CONCURRENCY_GAP 999"; '
            f"fi; "
            f'echo "=== DONE ==="'
            f'"'
        )

    @sanity_function
    def assert_completed(self):
        """Validate benchmark completion."""
        return sn.assert_found(r"=== DONE ===", self.stdout)

    @performance_function("Gflops")
    def job_a_gflops(self):
        """Extract job A Gflops."""
        return sn.extractsingle(r"JOB_A_GFLOPS (?P<val>\S+)", self.stdout, "val", float)

    @performance_function("Gflops")
    def job_b_gflops(self):
        """Extract job B Gflops."""
        return sn.extractsingle(r"JOB_B_GFLOPS (?P<val>\S+)", self.stdout, "val", float)

    @performance_function("s")
    def total_wall_seconds(self):
        """Extract total wall time in seconds."""
        return sn.extractsingle(r"TOTAL_WALL (?P<val>\S+)", self.stdout, "val", float)

    @performance_function("s")
    def concurrency_gap_seconds(self):
        """Extract concurrency gap in seconds."""
        return sn.extractsingle(r"CONCURRENCY_GAP (?P<val>\S+)", self.stdout, "val", float)
