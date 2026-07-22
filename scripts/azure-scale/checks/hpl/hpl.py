# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""HPL (High-Performance Linpack) CPU benchmark.

Builds HPL from netlib source against OpenBLAS and runs it across the CPU
partition (hb120rs-v3). Scales to N nodes with an automatic P x Q grid and
problem size derived from num_tasks and available memory per node.

The HPL.dat input file is generated at runtime. The output line tag is
derived from the PMAP/BCAST/DEPTH values written into HPL.dat so the sanity
and performance regexes always match the generated configuration.
"""

import math
import os

import reframe as rfm
import reframe.utility.sanity as sn

REFERENCE = {
    "azure:hb120rs-v3": {
        "cpu_gflops": (0, None, None, "Gflops"),
    },
}

# Core count per HB120rs_v3 node.
CORES_PER_NODE = 120
# Approximate usable memory per HB120rs_v3 node in GB (448GB total, leave headroom).
MEM_PER_NODE_GB = 400
# HPL block size.
NB = 384

# HPL prints one result line per run whose first column ("T/V") is a compact
# tag encoding the run configuration (e.g. "WR00R2R2"). Rather than hardcode a
# specific tag (which changes with PFACT/RFACT/BCAST/DEPTH), anchor on the
# stable "W" prefix and the fixed column layout:
#   T/V   N   NB   P   Q   Time   Gflops
# and capture the final Gflops column.
HPL_RESULT_RE = r"^W[RC]\S+\s+\d+\s+\d+\s+\d+\s+\d+\s+[0-9.eE+-]+\s+(?P<gflops>[0-9.eE+-]+)"


def _factor_grid(ntasks):
    """Factor ntasks into a P x Q grid as square as possible."""
    p = int(math.isqrt(ntasks))
    while p > 1 and ntasks % p != 0:
        p -= 1
    q = ntasks // p
    return p, q


def _hpl_dat(n, nb, p, q):
    """Render an HPL.dat with a single problem size and process grid."""
    return f"""HPLinpack benchmark input file
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


class build_hpl_cpu(rfm.CompileOnlyRegressionTest):
    """Build HPL from netlib source against OpenBLAS.

    The committed 'src' directory ships the OpenBLAS arch makefile
    (Make.Linux_PII_FBLAS). The netlib HPL 2.3 source is downloaded and
    extracted into the stage directory, the arch makefile is placed at its
    root (where the netlib build system expects Make.<arch>), and TOPdir is
    passed explicitly so the build does not depend on $HOME/hpl.
    """

    descr = "Build HPL from source with OpenBLAS"
    sourcesdir = "src"
    build_system = "Make"

    #: HPL release to build.
    hpl_version = variable(str, value="2.3")

    @run_before("compile")
    def set_make_opts(self):
        """Fetch HPL source, install the arch makefile, and configure make."""
        tarball = f"hpl-{self.hpl_version}.tar.gz"
        url = f"https://www.netlib.org/benchmark/hpl/{tarball}"
        self.prebuild_cmds = [
            # Download and flatten the netlib HPL tree into the stage dir so
            # that Make.Linux_PII_FBLAS (staged from 'src') sits at its root.
            f"curl -fsSL {url} -o {tarball}",
            f"tar xzf {tarball}",
            f"cp -r hpl-{self.hpl_version}/* .",
            f"rm -rf hpl-{self.hpl_version} {tarball}",
            # Bake the absolute stage-dir path into TOPdir. netlib's leaf
            # sub-makes cd into subdirectories, so TOPdir must be an absolute
            # constant rather than $(CURDIR)/$(pwd).
            'sed -i "s#^TOPdir .*#TOPdir       = $(pwd)#" Make.Linux_PII_FBLAS',
        ]
        self.build_system.options = ["arch=Linux_PII_FBLAS"]
        self.build_system.max_concurrency = 0

    @sanity_function
    def assert_built(self):
        """Assert the xhpl binary was produced."""
        return sn.assert_true(
            os.path.exists(os.path.join(self.stagedir, "bin", "Linux_PII_FBLAS", "xhpl"))
        )


@rfm.simple_test
class hpl_cpu_check(rfm.RunOnlyRegressionTest):
    """HPL CPU benchmark across the full CPU partition."""

    descr = "HPL CPU benchmark (full cluster)"
    valid_systems = ["*:hb120rs-v3"]
    valid_prog_environs = ["mpi-gnu"]
    reference = REFERENCE
    tags = {"benchmark", "hpl", "cpu"}
    num_tasks = variable(int, value=240)
    num_tasks_per_node = variable(int, value=CORES_PER_NODE)
    time_limit = "30m"

    hpl_binary = fixture(build_hpl_cpu, scope="environment")

    @run_before("run")
    def prepare_run(self):
        """Generate HPL.dat and point the executable at the built binary."""
        ntasks = self.num_tasks
        p, q = _factor_grid(ntasks)

        num_nodes = max(1, ntasks // CORES_PER_NODE)
        total_mem_gb = num_nodes * MEM_PER_NODE_GB
        n = int(math.sqrt((total_mem_gb * 1e9) / 8.0))
        n = (n // NB) * NB

        with open(os.path.join(self.stagedir, "HPL.dat"), "w") as f:
            f.write(_hpl_dat(n, NB, p, q))

        self.executable = os.path.join(self.hpl_binary.stagedir, "bin", "Linux_PII_FBLAS", "xhpl")
        self.executable_opts = []

    @sanity_function
    def assert_completed(self):
        """Assert HPL produced a result line."""
        return sn.assert_found(HPL_RESULT_RE, self.stdout)

    @performance_function("Gflops")
    def cpu_gflops(self):
        """Extract Gflops from the HPL result line."""
        return sn.extractsingle(HPL_RESULT_RE, self.stdout, "gflops", float)
