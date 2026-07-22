# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Concurrent OSU MPI bandwidth benchmark.

Demonstrates that the RDMA network can carry simultaneous point-to-point
transfers on separate node pairs without significant degradation. Submits
four osu_bw jobs simultaneously on different node pairs, verifies they
overlapped via sacct, and reports per-job bandwidth plus the degradation
against a solo reference bandwidth.

Requires at least 8 compute nodes so the four jobs land on disjoint pairs;
otherwise the test is skipped.
"""

import os

import reframe as rfm
import reframe.utility.sanity as sn
from hpctestlib.microbenchmarks.mpi.osu import build_osu_benchmarks

CORES_PER_NODE = 120
# Approximate solo single-pair bandwidth (MB/s) for HB120rs_v3 RDMA, used only
# to express concurrent degradation as a percentage. The authoritative solo
# figure comes from the standalone OSU pt2pt check.
SOLO_BW_MBPS = 22000


class build_osu_concurrent(build_osu_benchmarks):
    """Build OSU benchmarks for the concurrent bandwidth test."""

    build_type = parameter(["cpu"])


@rfm.simple_test
class osu_concurrent_check(rfm.RunOnlyRegressionTest):
    """Concurrent OSU MPI bandwidth check on separate node pairs."""

    descr = "Concurrent OSU MPI bandwidth on separate node pairs"
    valid_systems = ["*:login"]
    valid_prog_environs = ["builtin"]
    tags = {"benchmark", "mpi", "concurrent"}
    # Total task budget (nodes * cores); used only to gate the >= 8 node rule.
    num_tasks = variable(int, value=480)
    time_limit = "15m"

    osu_binaries = fixture(build_osu_concurrent, scope="environment")

    @run_before("run")
    def skip_if_too_small(self):
        """Skip unless there are at least 8 compute nodes (4 disjoint pairs)."""
        total_nodes = self.num_tasks // CORES_PER_NODE
        self.skip_if(
            total_nodes < 8,
            f"concurrent OSU test needs >= 8 nodes (4 disjoint pairs), got {total_nodes}",
        )

    @run_before("run")
    def set_executable(self):
        """Generate sbatch scripts and drive four concurrent osu_bw jobs."""
        osu_bw = os.path.join(self.osu_binaries.stagedir, "mpi", "pt2pt", "osu_bw")

        job_names = ["osu_conc_a", "osu_conc_b", "osu_conc_c", "osu_conc_d"]
        for job_name in job_names:
            with open(os.path.join(self.stagedir, f"{job_name}.sh"), "w") as f:
                f.write(
                    f"""#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --partition=hb120rs-v3
#SBATCH --nodes=2
#SBATCH --ntasks=2
#SBATCH --ntasks-per-node=1
#SBATCH --time=5:00
#SBATCH --output={job_name}.out
#SBATCH --error={job_name}.err

cd {self.stagedir}
srun {osu_bw} -m 4194304:4194304
"""
                )

        driver = os.path.join(self.stagedir, "drive_osu_concurrent.sh")
        with open(driver, "w") as f:
            f.write(
                f"""#!/bin/bash
set -u
cd {self.stagedir}
t_submit=$(date +%s)
job_a=$(sbatch --parsable osu_conc_a.sh)
job_b=$(sbatch --parsable osu_conc_b.sh)
job_c=$(sbatch --parsable osu_conc_c.sh)
job_d=$(sbatch --parsable osu_conc_d.sh)
echo "SUBMITTED $job_a $job_b $job_c $job_d"
while squeue -j "$job_a,$job_b,$job_c,$job_d" -h | grep -q .; do sleep 2; done
t_complete=$(date +%s)
echo "TOTAL_WALL $(echo "$t_complete - $t_submit" | bc -l)"

bw_a=$(grep "^4194304" osu_conc_a.out 2>/dev/null | awk '{{print $2}}' | tail -1)
bw_b=$(grep "^4194304" osu_conc_b.out 2>/dev/null | awk '{{print $2}}' | tail -1)
bw_c=$(grep "^4194304" osu_conc_c.out 2>/dev/null | awk '{{print $2}}' | tail -1)
bw_d=$(grep "^4194304" osu_conc_d.out 2>/dev/null | awk '{{print $2}}' | tail -1)
bw_a=${{bw_a:-0}}
bw_b=${{bw_b:-0}}
bw_c=${{bw_c:-0}}
bw_d=${{bw_d:-0}}
echo "CONCURRENT_BW_A $bw_a"
echo "CONCURRENT_BW_B $bw_b"
echo "CONCURRENT_BW_C $bw_c"
echo "CONCURRENT_BW_D $bw_d"

min_bw=$(echo "$bw_a $bw_b $bw_c $bw_d" | tr ' ' '\\n' | sort -n | head -1)
deg=$(echo "scale=2; (1 - $min_bw / {SOLO_BW_MBPS}) * 100" | bc -l 2>/dev/null || echo 0)
echo "DEGRADATION_PERCENT $deg"

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

if [ "$max_start" -lt "$min_end" ]; then
  echo "IS_CONCURRENT true"
else
  echo "IS_CONCURRENT false"
fi
echo "=== DONE ==="
"""
            )

        self.executable = "bash"
        self.executable_opts = [driver]

    @sanity_function
    def assert_completed(self):
        """Validate the driver completed and all jobs ran concurrently."""
        return sn.all(
            [
                sn.assert_found(r"=== DONE ===", self.stdout),
                sn.assert_found(r"IS_CONCURRENT true", self.stdout),
            ]
        )

    @performance_function("MB/s")
    def concurrent_bw_a(self):
        """Extract concurrent bandwidth A in MB/s."""
        return sn.extractsingle(r"CONCURRENT_BW_A (?P<val>[0-9.eE+-]+)", self.stdout, "val", float)

    @performance_function("MB/s")
    def concurrent_bw_b(self):
        """Extract concurrent bandwidth B in MB/s."""
        return sn.extractsingle(r"CONCURRENT_BW_B (?P<val>[0-9.eE+-]+)", self.stdout, "val", float)

    @performance_function("MB/s")
    def concurrent_bw_c(self):
        """Extract concurrent bandwidth C in MB/s."""
        return sn.extractsingle(r"CONCURRENT_BW_C (?P<val>[0-9.eE+-]+)", self.stdout, "val", float)

    @performance_function("MB/s")
    def concurrent_bw_d(self):
        """Extract concurrent bandwidth D in MB/s."""
        return sn.extractsingle(r"CONCURRENT_BW_D (?P<val>[0-9.eE+-]+)", self.stdout, "val", float)

    @performance_function("%")
    def degradation_percent(self):
        """Extract bandwidth degradation percentage."""
        return sn.extractsingle(
            r"DEGRADATION_PERCENT (?P<val>[0-9.eE+-]+)", self.stdout, "val", float
        )

    @performance_function("s")
    def total_wall_seconds(self):
        """Extract total wall time in seconds."""
        return sn.extractsingle(r"TOTAL_WALL (?P<val>[0-9.eE+-]+)", self.stdout, "val", float)
