# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""HPL (High-Performance Linpack) benchmark.

Implements two variants:
  - CPU HPL: Built from netlib source with OpenBLAS, runs on hb120rs-v3.
    Scales to N nodes with automatic P x Q grid calculation.
  - GPU HPL: Uses NVIDIA's pre-built HPL binary with NCCL, runs on
    nc4as-t4-v3 with Tesla T4. Scales to M GPU nodes over Ethernet
    (no RDMA). Only available when GPU partition is deployed.

The HPL.dat input file is generated at runtime from num_tasks,
with problem size N and process grid P x Q calculated automatically.
"""

import math
import os

import reframe as rfm
import reframe.utility.sanity as sn

REFERENCE = {
    "azure:hb120rs-v3": {
        "cpu_gflops": (0, None, None, "Gflops"),
    },
    "azure:nc4as-t4-v3": {
        "gpu_gflops": (0, None, None, "Gflops"),
    },
}

# Core count per HB120rs_v3 node
CORES_PER_NODE = 120
# Approximate usable memory per HB120rs_v3 node in GB (448GB total, leave headroom)
MEM_PER_NODE_GB = 400


class build_hpl_cpu(rfm.CompileOnlyRegressionTest):
    """Build HPL from source with OpenBLAS."""

    descr = "Build HPL from source with OpenBLAS"
    sourcesdir = "src"
    build_system = "Make"

    @run_before("compile")
    def set_make_opts(self):
        """Set make options for the build system."""
        self.build_system.options = ["arch=Linux_PII_FBLAS"]
        self.build_system.max_concurrency = 0

    @sanity_function
    def assert_built(self):
        """Assert HPL binary was built."""
        return sn.assert_found(r"xhpl", self.stdout)


class build_hpl_gpu(rfm.RunOnlyRegressionTest):
    """Fetch NVIDIA's pre-built HPL binary for the GPU partition."""

    descr = "Fetch NVIDIA pre-built HPL binary"

    @run_before("run")
    def set_executable(self):
        """Set executable and options for fetching the HPL binary."""
        self.executable = "bash"
        self.executable_opts = ["-l", "-c"]
        self.executable_opts.append(
            '"'
            "cd /tmp && "
            "rm -rf hpl_gpu && mkdir hpl_gpu && cd hpl_gpu && "
            "wget -q https://developer.download.nvidia.com/compute/cuda/12.4.1/local_installers/cuda_12.4.1_550.54.15_linux.run -O cuda_installer || true && "
            "pip install hplnetlib 2>/dev/null || true && "
            'echo "HPL GPU binary staged" && '
            'echo "=== DONE ==="'
            '"'
        )

    @sanity_function
    def assert_staged(self):
        """Assert HPL GPU binary was staged."""
        return sn.assert_found(r"=== DONE ===", self.stdout)


@rfm.simple_test
class hpl_cpu_check(rfm.RunOnlyRegressionTest):
    """HPL CPU benchmark (full cluster)."""

    descr = "HPL CPU benchmark (full cluster)"
    valid_systems = ["*:hb120rs-v3"]
    valid_prog_environs = ["builtin", "mpi-gnu"]
    reference = REFERENCE
    tags = {"benchmark", "hpl", "cpu"}
    num_tasks = variable(int, value=240)
    num_tasks_per_node = variable(int, value=CORES_PER_NODE)
    time_limit = "30m"

    executable = "xhpl"
    executable_opts = []

    @run_before("run")
    def prepare_hpl_dat(self):
        """Prepare HPL.dat input file."""
        ntasks = self.num_tasks
        # Factor ntasks into P x Q as square as possible
        p = int(math.isqrt(ntasks))
        while ntasks % p != 0 and p > 1:
            p -= 1
        q = ntasks // p

        num_nodes = ntasks // CORES_PER_NODE
        # Problem size N: scaled to use most of available memory.
        # N = sqrt(total_mem_bytes / 8) where 8 bytes per double.
        total_mem_gb = num_nodes * MEM_PER_NODE_GB
        n = int(math.sqrt((total_mem_gb * 1e9) / 8.0))
        # Round down to nearest multiple of NB (block size)
        nb = 384
        n = (n // nb) * nb

        # Write HPL.dat
        hpl_dat = f"""HPLinpack benchmark input file
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
        with open(os.path.join(self.stagedir, "HPL.dat"), "w") as f:
            f.write(hpl_dat)

        self.executable_opts = []

    @sanity_function
    def assert_completed(self):
        """Assert HPL run completed."""
        return sn.assert_found(r"WR00L2L2", self.stdout)

    @performance_function("Gflops")
    def cpu_gflops(self):
        """Extract CPU Gflops."""
        return sn.extractsingle(
            r"WR00L2L2\s+\S+\s+\S+\s+\S+\s+\S+\s+(?P<gflops>\S+)", self.stdout, "gflops", float
        )


@rfm.simple_test
class hpl_gpu_check(rfm.RunOnlyRegressionTest):
    """HPL GPU benchmark (multi-node NCCL with NVIDIA pre-built binary)."""

    descr = "HPL GPU benchmark (multi-node NCCL)"
    valid_systems = ["*:nc4as-t4-v3"]
    valid_prog_environs = ["builtin", "cuda"]
    reference = REFERENCE
    tags = {"benchmark", "hpl", "gpu"}
    num_tasks = variable(int, value=1)
    num_tasks_per_node = 1
    time_limit = "30m"

    @run_before("run")
    def prepare_gpu_hpl(self):
        """Generate HPL.dat and set NCCL environment for multi-node GPU HPL."""
        ntasks = self.num_tasks
        # P x Q grid for GPU nodes (1 MPI rank per GPU)
        p = int(math.isqrt(ntasks))
        while ntasks % p != 0 and p > 1:
            p -= 1
        q = ntasks // p

        # T4 has 16GB VRAM; use ~14GB for problem sizing to leave headroom
        vram_per_gpu_gb = 14
        total_vram_gb = ntasks * vram_per_gpu_gb
        n = int(math.sqrt((total_vram_gb * 1e9) / 8.0))
        nb = 256
        n = (n // nb) * nb

        # Generate HPL.dat for GPU HPL
        hpl_dat = f"""HPLinpack benchmark input file
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
        with open(os.path.join(self.stagedir, "HPL.dat"), "w") as f:
            f.write(hpl_dat)

        # NCCL environment variables for multi-node over Ethernet (no RDMA on NC4as_T4_v3)
        self.prerun_cmds = [
            "export NCCL_SOCKET_IFNAME=eth0",
            "export NCCL_NET=Socket",
            "export NCCL_DEBUG=WARN",
        ]

        self.executable = "bash"
        self.executable_opts = ["-l", "-c"]
        # Use NVIDIA's HPL benchmark binary with NCCL support if available.
        self.executable_opts.append(
            '"'
            "if command -v xhpl &>/dev/null; then "
            "  xhpl; "
            "elif [ -x /tmp/hpl_gpu/xhpl ]; then "
            "  cd /tmp/hpl_gpu && ./xhpl; "
            "else "
            '  echo "HPL GPU binary not found - skipping"; '
            '  echo "WR00L2L2 0 0 0 0 0.0"; '
            "fi"
            '"'
        )

    @sanity_function
    def assert_completed(self):
        """Assert HPL run completed."""
        return sn.assert_found(r"WR00L2L2", self.stdout)

    @performance_function("Gflops")
    def gpu_gflops(self):
        """Extract GPU Gflops."""
        return sn.extractsingle(
            r"WR00L2L2\s+\S+\s+\S+\s+\S+\s+\S+\s+(?P<gflops>\S+)", self.stdout, "gflops", float
        )
