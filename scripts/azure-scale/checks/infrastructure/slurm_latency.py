# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Slurm scheduling latency check.

Measures the overhead of querying Slurm and dispatching jobs. Runs on the
login node and submits to the compute partition. Reports srun dispatch
latency, sinfo/scontrol query times, and sbatch-to-start latency.

The reference values are permissive upper bounds intended to catch gross
regressions (e.g. a wedged controller), not tight performance targets.
"""

import os

import reframe as rfm
import reframe.utility.sanity as sn

REFERENCE = {
    "azure:login": {
        "srun_latency": (2.0, None, 1.0, "s"),
        "sinfo_latency": (0.5, None, 2.0, "s"),
        "scontrol_latency": (0.5, None, 2.0, "s"),
        "sbatch_latency": (3.0, None, 1.0, "s"),
    },
}

# Collection script written to the stage dir to avoid fragile nested quoting.
_SCRIPT = r"""#!/bin/bash
PARTITION="$1"

echo "=== SRUN ==="
{ time -p srun -p "$PARTITION" --time=1 hostname ; } 2>&1

echo "=== SINFO ==="
{ time -p sinfo ; } 2>&1

echo "=== SCONTROL ==="
{ time -p scontrol show nodes >/dev/null ; } 2>&1

echo "=== SBATCH ==="
sbatch_script=$(mktemp)
cat > "$sbatch_script" <<EOF
#!/bin/bash
#SBATCH -p $PARTITION
#SBATCH --time=1
#SBATCH -o /dev/null
hostname
EOF
t_start=$(date +%s.%N)
jobid=$(sbatch --parsable "$sbatch_script" 2>/dev/null)
if [ -n "$jobid" ]; then
    while true; do
        state=$(squeue -j "$jobid" -h -o %T 2>/dev/null)
        [ "$state" = "RUNNING" ] && break
        [ -z "$state" ] && break
        sleep 0.1
    done
    t_running=$(date +%s.%N)
    echo "SBATCH_LATENCY $(echo "$t_running - $t_start" | bc -l)"
    scancel "$jobid" 2>/dev/null
else
    echo "SBATCH_LATENCY 999"
fi
rm -f "$sbatch_script"
echo "=== DONE ==="
"""


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
        """Write the measurement script to the stage dir and run it."""
        script = os.path.join(self.stagedir, "measure_latency.sh")
        with open(script, "w") as f:
            f.write(_SCRIPT)
        self.executable = "bash"
        self.executable_opts = [script, self.partition]

    @sanity_function
    def assert_completed(self):
        """Assert completion."""
        return sn.assert_found(r"=== DONE ===", self.stdout)

    @performance_function("s")
    def srun_latency(self):
        """Extract srun dispatch latency."""
        return sn.extractsingle(
            r"=== SRUN ===\n[\s\S]*?real\s+(?P<time>[0-9.]+)", self.stdout, "time", float
        )

    @performance_function("s")
    def sinfo_latency(self):
        """Extract sinfo query latency."""
        return sn.extractsingle(
            r"=== SINFO ===\n[\s\S]*?real\s+(?P<time>[0-9.]+)", self.stdout, "time", float
        )

    @performance_function("s")
    def scontrol_latency(self):
        """Extract scontrol query latency."""
        return sn.extractsingle(
            r"=== SCONTROL ===\n[\s\S]*?real\s+(?P<time>[0-9.]+)", self.stdout, "time", float
        )

    @performance_function("s")
    def sbatch_latency(self):
        """Extract sbatch-to-start latency."""
        return sn.extractsingle(r"SBATCH_LATENCY (?P<time>[0-9.]+)", self.stdout, "time", float)
