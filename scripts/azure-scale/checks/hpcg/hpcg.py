# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""HPCG (High Performance Conjugate Gradients) benchmark.

Builds HPCG from source with MPI and runs it across the CPU partition
(hb120rs-v3). Scales to N nodes; the per-rank local problem size is fixed
and the global problem grows with the rank count. HPCG stresses memory
bandwidth and latency, complementing HPL's raw FLOPS measurement.
"""

import os

import reframe as rfm
import reframe.utility.sanity as sn

REFERENCE = {
    "azure:hb120rs-v3": {
        "hpcg_gflops": (0, None, None, "GFLOP/s"),
    },
}

CORES_PER_NODE = 120

# Per-rank local dimensions. Must be multiples of 8 for HPCG. 104 keeps the
# per-rank working set large enough to stress memory without exhausting RAM.
LOCAL_NX = 104
LOCAL_NY = 104
LOCAL_NZ = 104
# HPCG measured run time in seconds (>= 60 required for an official run).
RUNTIME_SECONDS = 60


class build_hpcg(rfm.CompileOnlyRegressionTest):
    """Build HPCG from source with MPI."""

    descr = "Build HPCG from source"
    sourcesdir = "https://github.com/hpcg-benchmark/hpcg.git"
    build_system = "Make"

    @run_before("compile")
    def set_build_opts(self):
        """Configure the HPCG build for the Linux MPI arch."""
        # HPCG ships setup/Make.<arch> templates. Use the generic MPI+GCC one.
        self.build_system.options = ["arch=MPI_GCC_OMP"]
        self.build_system.max_concurrency = 0

    @sanity_function
    def assert_built(self):
        """Assert the xhpcg binary was produced."""
        return sn.assert_true(os.path.exists(os.path.join(self.stagedir, "bin", "xhpcg")))


@rfm.simple_test
class hpcg_check(rfm.RunOnlyRegressionTest):
    """HPCG benchmark across the full CPU partition."""

    descr = "HPCG benchmark (full cluster)"
    valid_systems = ["*:hb120rs-v3"]
    valid_prog_environs = ["mpi-gnu"]
    reference = REFERENCE
    tags = {"benchmark", "hpcg"}
    num_tasks = variable(int, value=240)
    num_tasks_per_node = variable(int, value=CORES_PER_NODE)
    time_limit = "30m"

    hpcg_binary = fixture(build_hpcg, scope="environment")

    @run_before("run")
    def prepare_run(self):
        """Generate hpcg.dat and point the executable at the built binary."""
        # HPCG input file format (hpcg.dat):
        #   line 1: title/comment
        #   line 2: comment
        #   line 3: "nx ny nz" local problem dimensions (single line)
        #   line 4: run time in seconds
        hpcg_dat = (
            "HPCG benchmark input file\n"
            "Charmed-HPC azure-scale\n"
            f"{LOCAL_NX} {LOCAL_NY} {LOCAL_NZ}\n"
            f"{RUNTIME_SECONDS}\n"
        )
        with open(os.path.join(self.stagedir, "hpcg.dat"), "w") as f:
            f.write(hpcg_dat)

        self.executable = os.path.join(self.hpcg_binary.stagedir, "bin", "xhpcg")
        self.executable_opts = []

    @sanity_function
    def assert_completed(self):
        """Assert HPCG completed and reported a valid result."""
        return sn.assert_found(r"Final Summary::HPCG result is VALID", self.stdout)

    @performance_function("GFLOP/s")
    def hpcg_gflops(self):
        """Extract the total GFLOP/s rating from the HPCG summary.

        HPCG writes a summary file and prints lines such as:
            Final Summary::HPCG result is VALID with a GFLOP/s rating of=12.34
        """
        return sn.extractsingle(
            r"HPCG result is VALID with a GFLOP/s rating of\s*=\s*(?P<val>[0-9.eE+-]+)",
            self.stdout,
            "val",
            float,
        )
