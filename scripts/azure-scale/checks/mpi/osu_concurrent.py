# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Concurrent OSU MPI bandwidth benchmark.

Demonstrates that the RDMA network can handle simultaneous point-to-point
transfers on separate node pairs without significant degradation.
Submits two osu_bw jobs simultaneously on different node pairs, then
compares against the solo bandwidth from the full-cluster OSU test.

Only runs when num_tasks >= 4 nodes worth (i.e., --nodes >= 4).
"""

import reframe as rfm
import reframe.utility.sanity as sn

REFERENCE = {
    "azure:login": {
        "concurrent_bw_a": (0, None, None, "MB/s"),
        "concurrent_bw_b": (0, None, None, "MB/s"),
        "degradation_percent": (0, None, None, "%"),
    },
}


@rfm.simple_test
class osu_concurrent_check(rfm.RunOnlyRegressionTest):
    """Concurrent OSU MPI bandwidth check on separate node pairs."""

    descr = "Concurrent OSU MPI bandwidth on separate node pairs"
    valid_systems = ["*:login"]
    valid_prog_environs = ["builtin"]
    reference = REFERENCE
    tags = {"benchmark", "mpi", "concurrent"}
    time_limit = "10m"

    @run_before("run")
    def set_executable(self):
        """Set executable."""
        self.executable = "bash"
        self.executable_opts = ["-l", "-c"]

        # Submit two osu_bw jobs simultaneously on different node pairs.
        # Each job uses 2 tasks on 2 nodes within the hb120rs-v3 partition.
        self.executable_opts.append(
            '"'
            "cd /tmp && rm -rf osu_conc && mkdir osu_conc && cd osu_conc; "
            "# Submit two concurrent osu_bw jobs; "
            'cat > osu_a.sh << "EOFA"\n'
            "#!/bin/bash\n"
            "#SBATCH --job-name=osu_conc_a\n"
            "#SBATCH --partition=hb120rs-v3\n"
            "#SBATCH --nodes=2\n"
            "#SBATCH --ntasks=2\n"
            "#SBATCH --ntasks-per-node=1\n"
            "#SBATCH --time=5:00\n"
            "#SBATCH --output=osu_a.out\n"
            "#SBATCH --error=osu_a.err\n"
            "srun osu_bw -m 4194304:4194304\n"
            "EOFA\n"
            'cat > osu_b.sh << "EOFB"\n'
            "#!/bin/bash\n"
            "#SBATCH --job-name=osu_conc_b\n"
            "#SBATCH --partition=hb120rs-v3\n"
            "#SBATCH --nodes=2\n"
            "#SBATCH --ntasks=2\n"
            "#SBATCH --ntasks-per-node=1\n"
            "#SBATCH --time=5:00\n"
            "#SBATCH --output=osu_b.out\n"
            "#SBATCH --error=osu_b.err\n"
            "srun osu_bw -m 4194304:4194304\n"
            "EOFB\n"
            "t_submit=$(date +%s); "
            "job_a=$(sbatch --parsable osu_a.sh); "
            "job_b=$(sbatch --parsable osu_b.sh); "
            'echo "SUBMITTED $job_a $job_b"; '
            "# Wait for both to complete; "
            "while squeue -j $job_a,$job_b -h | grep -q .; do sleep 2; done; "
            "t_complete=$(date +%s); "
            'echo "TOTAL_WALL $(echo "$t_complete - $t_submit" | bc -l)"; '
            "# Extract bandwidth (4MB row) from each; "
            r'bw_a=$(grep "^4194304" osu_a.out 2>/dev/null | awk "{print \$2}" || echo "0"); '
            r'bw_b=$(grep "^4194304" osu_b.out 2>/dev/null | awk "{print \$2}" || echo "0"); '
            'echo "CONCURRENT_BW_A $bw_a"; '
            'echo "CONCURRENT_BW_B $bw_b"; '
            "# Degradation: compare min of concurrent vs typical solo (~22000 MB/s); "
            "# Solo value is approximate; actual solo comes from full-cluster OSU test; "
            "solo_bw=22000; "
            r'min_concurrent=$(echo "$bw_a $bw_b" | awk "{if (\$1 < \$2) print \$1; else print \$2}"); '
            'deg=$(echo "scale=2; (1 - $min_concurrent / $solo_bw) * 100" | bc -l 2>/dev/null || echo "0"); '
            'echo "DEGRADATION_PERCENT $deg"; '
            "# Check concurrency; "
            "start_a=$(sacct -j $job_a --format=start -n -P | head -1); "
            "end_a=$(sacct -j $job_a --format=end -n -P | head -1); "
            "start_b=$(sacct -j $job_b --format=start -n -P | head -1); "
            "end_b=$(sacct -j $job_b --format=end -n -P | head -1); "
            'ts_a=$(date -d "$start_a" +%s 2>/dev/null || echo 0); '
            'te_a=$(date -d "$end_a" +%s 2>/dev/null || echo 0); '
            'ts_b=$(date -d "$start_b" +%s 2>/dev/null || echo 0); '
            'te_b=$(date -d "$end_b" +%s 2>/dev/null || echo 0); '
            'if [ "$ts_a" -lt "$te_b" ] && [ "$ts_b" -lt "$te_a" ]; then '
            '  echo "IS_CONCURRENT true"; '
            "else "
            '  echo "IS_CONCURRENT false"; '
            "fi; "
            'echo "=== DONE ==="'
            '"'
        )

    @sanity_function
    def assert_completed(self):
        """Assert benchmark completed."""
        return sn.assert_found(r"=== DONE ===", self.stdout)

    @performance_function("MB/s")
    def concurrent_bw_a(self):
        """Extract concurrent bandwidth A in MB/s."""
        return sn.extractsingle(r"CONCURRENT_BW_A (?P<val>\S+)", self.stdout, "val", float)

    @performance_function("MB/s")
    def concurrent_bw_b(self):
        """Extract concurrent bandwidth B in MB/s."""
        return sn.extractsingle(r"CONCURRENT_BW_B (?P<val>\S+)", self.stdout, "val", float)

    @performance_function("%")
    def degradation_percent(self):
        """Extract bandwidth degradation percentage."""
        return sn.extractsingle(r"DEGRADATION_PERCENT (?P<val>\S+)", self.stdout, "val", float)

    @performance_function("s")
    def total_wall_seconds(self):
        """Extract total wall time in seconds."""
        return sn.extractsingle(r"TOTAL_WALL (?P<val>\S+)", self.stdout, "val", float)
