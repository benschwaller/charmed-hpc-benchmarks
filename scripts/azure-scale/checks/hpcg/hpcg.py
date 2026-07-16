# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""HPCG (High Performance Conjugate Gradients) benchmark.

Builds HPCG from source and runs it on the CPU partition (hb120rs-v3).
Scales to N nodes with automatic problem sizing based on num_tasks.
HPCG stresses memory bandwidth and latency - complementary to HPL's
raw FLOPS measurement.
"""

import os

import reframe as rfm
import reframe.utility.sanity as sn

REFERENCE = {
    "azure:hb120rs-v3": {
        "hpcg_gflops": (0, None, None, "GFLOP/s"),
        "hpcg_rating": (0, None, None, "score"),
    },
}

CORES_PER_NODE = 120
MEM_PER_NODE_GB = 400


class build_hpcg(rfm.CompileOnlyRegressionTest):
    """Build HPCG from source."""

    descr = "Build HPCG from source"
    build_system = "Make"

    @run_before("compile")
    def set_build_opts(self):
        """Set HPCG build options."""
        # Clone HPCG and configure for MPI + OpenMP
        self.prebuild_cmds = [
            "git clone https://github.com/hpcg-benchmark/hpcg.git . || true",
            "cd src && cp Make.Linux_PII_FBLAS_hpcg Make.%s" % "Linux_PII_FBLAS_hpcg",
        ]
        self.build_system.options = ["ARCH=Linux_PII_FBLAS_hpcg"]
        self.build_system.max_concurrency = 0

    @sanity_function
    def assert_built(self):
        """Assert HPCG binary was built."""
        return sn.assert_found(r"xhpcg", self.stdout)


@rfm.simple_test
class hpcg_check(rfm.RunOnlyRegressionTest):
    """HPCG benchmark (full cluster)."""

    descr = "HPCG benchmark (full cluster)"
    valid_systems = ["*:hb120rs-v3"]
    valid_prog_environs = ["builtin", "mpi-gnu"]
    reference = REFERENCE
    tags = {"benchmark", "hpcg"}
    num_tasks = variable(int, value=240)
    num_tasks_per_node = variable(int, value=CORES_PER_NODE)
    time_limit = "30m"

    executable = "xhpcg"
    executable_opts = ["--rt=60"]

    @run_before("run")
    def prepare_hpcg_dat(self):
        """Prepare HPCG.dat input file."""
        ntasks = self.num_tasks

        # Local problem size per rank (nx, ny, nz).
        # Use ~104 per dimension (standard HPCG sizing).
        # This keeps the global problem manageable while stressing memory.
        local_nx = 104
        local_ny = 104
        local_nz = 104

        # HPCG.dat format:
        # Line 1: number of MPI ranks
        # Line 2-4: local nx, ny, nz
        # Line 5: runtime in seconds
        hpcg_dat = f"""{ntasks}
{local_nx}
{local_ny}
{local_nz}
60
"""
        with open(os.path.join(self.stagedir, "HPCG.dat"), "w") as f:
            f.write(hpcg_dat)

    @sanity_function
    def assert_completed(self):
        """Assert HPCG run completed."""
        return sn.assert_found(r"Final Summary", self.stdout)

    @performance_function("GFLOP/s")
    def hpcg_gflops(self):
        """Extract GFLOP/s from HPCG output."""
        return sn.extractsingle(r"GFLOP/s.*?=\s*(?P<val>\S+)", self.stdout, "val", float)

    @performance_function("score")
    def hpcg_rating(self):
        """Extract HPCG performance rating."""
        # HPCG outputs a performance rating score
        return sn.extractsingle(
            r"HPCG result is VALID.*?rating of\s*(?P<val>\S+)", self.stdout, "val", float
        )
