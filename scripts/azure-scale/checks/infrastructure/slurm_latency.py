# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Slurm scheduling latency check.

Measures the overhead of submitting and starting jobs through Slurm.
Runs on the login node and submits to compute partitions via srun/sbatch.
Reports srun dispatch latency, sinfo query time, scontrol query time,
and sbatch-to-start latency.
"""

import reframe as rfm
import reframe.utility.sanity as sn

REFERENCE = {
    "azure:login": {
        "srun_latency": (2.0, None, 0.5, "s"),
        "sinfo_latency": (0.5, None, 0.5, "s"),
        "scontrol_latency": (0.5, None, 0.5, "s"),
        "sbatch_latency": (3.0, None, 0.5, "s"),
    },
}


@rfm.simple_test
class slurm_dispatch_latency(rfm.RunOnlyRegressionTest):
    """Slurm job dispatch and query latency check."""

    valid_systems = ["*:login"]
    valid_prog_environs = ["builtin"]
    reference = REFERENCE
    tags = {"infrastructure", "scheduling"}
    descr = "Slurm job dispatch and query latency"

    partition = variable(str, value="hb120rs-v3")

    @run_before("run")
    def set_executable(self):
        """Set executable."""
        self.executable = "bash"
        self.executable_opts = ["-l", "-c"]
        # Run all latency measurements in sequence, each wrapped in `time -p`.
        # Output is tagged with section headers for parsing.
        self.executable_opts.append(
            f'"'
            f'echo "=== SRUN ==="; '
            f"time -p srun -p {self.partition} --time=1 hostname 2>&1; "
            f'echo "=== SINFO ==="; '
            f"time -p sinfo 2>&1; "
            f'echo "=== SCONTROL ==="; '
            f"time -p scontrol show nodes 2>&1 >/dev/null; "
            f'echo "=== SBATCH ==="; '
            f"sbatch_script=$(mktemp); "
            f'echo "#!/bin/bash" > $sbatch_script; '
            f'echo "#SBATCH -p {self.partition}" >> $sbatch_script; '
            f'echo "#SBATCH --time=1" >> $sbatch_script; '
            f'echo "#SBATCH -o /dev/null" >> $sbatch_script; '
            f'echo "hostname" >> $sbatch_script; '
            f"t_start=$(date +%s.%N); "
            f'jobid=$(sbatch $sbatch_script 2>/dev/null | awk "{{print $4}}"); '
            f'if [ -n "$jobid" ]; then '
            f'  while [ "$(squeue -j $jobid -h -o %T 2>/dev/null)" != "RUNNING" ] && '
            f'        [ "$(squeue -j $jobid -h -o %T 2>/dev/null)" != "" ]; do sleep 0.1; done; '
            f"  t_running=$(date +%s.%N); "
            f'  echo "SBATCH_LATENCY $(echo "$t_running - $t_start" | bc -l)"; '
            f"  scancel $jobid 2>/dev/null; "
            f"else "
            f'  echo "SBATCH_LATENCY 999"; '
            f"fi; "
            f"rm -f $sbatch_script; "
            f'echo "=== DONE ==="'
            f'"'
        )

    @sanity_function
    def assert_completed(self):
        """Assert completion."""
        return sn.assert_found(r"=== DONE ===", self.stdout)

    @performance_function("s")
    def srun_latency(self):
        """Extract srun dispatch latency."""
        return sn.extractsingle(
            r"=== SRUN ===\n[\s\S]*?real (?P<time>\S+)", self.stdout, "time", float
        )

    @performance_function("s")
    def sinfo_latency(self):
        """Extract sinfo query latency."""
        return sn.extractsingle(
            r"=== SINFO ===\n[\s\S]*?real (?P<time>\S+)", self.stdout, "time", float
        )

    @performance_function("s")
    def scontrol_latency(self):
        """Extract scontrol query latency."""
        return sn.extractsingle(
            r"=== SCONTROL ===\n[\s\S]*?real (?P<time>\S+)", self.stdout, "time", float
        )

    @performance_function("s")
    def sbatch_latency(self):
        """Extract sbatch-to-start latency."""
        return sn.extractsingle(r"SBATCH_LATENCY (?P<time>\S+)", self.stdout, "time", float)
