# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charmed-HPC infrastructure overhead check.

Measures the full resource footprint of the Juju/charm infrastructure
on each node partition — not just jujud, but all Juju-related processes
including snapd, juju-run, and charm hook helpers.

Reports per-process and aggregate metrics as performance variables.
On the baseline (raw VM) pipeline, the Juju-specific metrics will be zero,
making the overhead directly measurable as the delta between pipelines.
"""

import reframe as rfm
import reframe.utility.sanity as sn

REFERENCE = {
    "azure:login": {
        "jujud_cpu_percent": (0, None, None, "%"),
        "jujud_rss_mb": (0, None, None, "MB"),
        "jujud_threads": (0, None, None, "count"),
        "snapd_cpu_percent": (0, None, None, "%"),
        "snapd_rss_mb": (0, None, None, "MB"),
        "total_juju_cpu_percent": (0, None, None, "%"),
        "total_juju_rss_mb": (0, None, None, "MB"),
        "juju_disk_mb": (0, None, None, "MB"),
        "juju_log_disk_mb": (0, None, None, "MB"),
    },
    "azure:hb120rs-v3": {
        "jujud_cpu_percent": (0, None, None, "%"),
        "jujud_rss_mb": (0, None, None, "MB"),
        "jujud_threads": (0, None, None, "count"),
        "snapd_cpu_percent": (0, None, None, "%"),
        "snapd_rss_mb": (0, None, None, "MB"),
        "total_juju_cpu_percent": (0, None, None, "%"),
        "total_juju_rss_mb": (0, None, None, "MB"),
        "juju_disk_mb": (0, None, None, "MB"),
        "juju_log_disk_mb": (0, None, None, "MB"),
    },
    "azure:nc4as-t4-v3": {
        "jujud_cpu_percent": (0, None, None, "%"),
        "jujud_rss_mb": (0, None, None, "MB"),
        "jujud_threads": (0, None, None, "count"),
        "snapd_cpu_percent": (0, None, None, "%"),
        "snapd_rss_mb": (0, None, None, "MB"),
        "total_juju_cpu_percent": (0, None, None, "%"),
        "total_juju_rss_mb": (0, None, None, "MB"),
        "juju_disk_mb": (0, None, None, "MB"),
        "juju_log_disk_mb": (0, None, None, "MB"),
    },
}


@rfm.simple_test
class juju_agent_overhead(rfm.RunOnlyRegressionTest):
    """Charmed-HPC infrastructure overhead (jujud, snapd, all Juju processes)."""

    valid_systems = ["*:login", "*:hb120rs-v3", "*:nc4as-t4-v3"]
    valid_prog_environs = ["builtin"]
    reference = REFERENCE
    tags = {"infrastructure", "overhead"}
    descr = "Charmed-HPC infrastructure overhead (jujud, snapd, aggregate)"

    @run_before("run")
    def set_executable(self):
        """Set job script to collect all Juju-related resource metrics."""
        self.executable = "bash"
        self.executable_opts = ["-l", "-c"]
        # Collect metrics for jujud, snapd, and all juju-related processes.
        # Uses ps to get per-process CPU/RAM/threads, and du for disk usage.
        # Sections are tagged for regex parsing.
        self.executable_opts.append(
            '"'
            # --- jujud process ---
            'echo "=== JUJUD ==="; '
            'ps -o %cpu,rss,nlwp -C jujud --no-headers 2>/dev/null || echo "0 0 0"; '
            # --- snapd process ---
            'echo "=== SNAPD ==="; '
            'ps -o %cpu,rss,nlwp -C snapd --no-headers 2>/dev/null || echo "0 0 0"; '
            # --- aggregate all juju-related processes ---
            'echo "=== AGGREGATE ==="; '
            "ps -o %cpu,rss --no-headers -C jujud,juju-run,juju-exec,snapd,juju-db 2>/dev/null "
            '| awk "{cpu+=\\$1; rss+=\\$2} END {if (NR==0) print "0 0"; else print cpu, rss}"; '
            # --- Juju state disk usage ---
            'echo "=== DISK_STATE ==="; '
            'du -sm /var/lib/juju 2>/dev/null | cut -f1 || echo "0"; '
            # --- Juju log disk usage ---
            'echo "=== DISK_LOGS ==="; '
            'du -sm /var/log/juju 2>/dev/null | cut -f1 || echo "0"; '
            'echo "=== DONE ==="'
            '"'
        )

    @sanity_function
    def assert_completed(self):
        """Assert metrics collection completed."""
        return sn.assert_found(r"=== DONE ===", self.stdout)

    # --- jujud metrics ---

    @performance_function("%")
    def jujud_cpu_percent(self):
        """Extract jujud CPU percentage."""
        return sn.extractsingle(r"=== JUJUD ===\s*(?P<cpu>\S+)", self.stdout, "cpu", float)

    @performance_function("MB")
    def jujud_rss_mb(self):
        """Extract jujud RSS memory in megabytes."""
        rss_kb = sn.extractsingle(r"=== JUJUD ===\s*\S+\s+(?P<rss>\S+)", self.stdout, "rss", float)
        return rss_kb / 1024.0

    @performance_function("count")
    def jujud_threads(self):
        """Extract jujud thread count."""
        return sn.extractsingle(
            r"=== JUJUD ===\s*\S+\s+\S+\s+(?P<threads>\S+)", self.stdout, "threads", float
        )

    # --- snapd metrics ---

    @performance_function("%")
    def snapd_cpu_percent(self):
        """Extract snapd CPU percentage."""
        return sn.extractsingle(r"=== SNAPD ===\s*(?P<cpu>\S+)", self.stdout, "cpu", float)

    @performance_function("MB")
    def snapd_rss_mb(self):
        """Extract snapd RSS memory in megabytes."""
        rss_kb = sn.extractsingle(r"=== SNAPD ===\s*\S+\s+(?P<rss>\S+)", self.stdout, "rss", float)
        return rss_kb / 1024.0

    # --- aggregate Juju infrastructure metrics ---

    @performance_function("%")
    def total_juju_cpu_percent(self):
        """Extract total CPU usage of all Juju-related processes."""
        return sn.extractsingle(r"=== AGGREGATE ===\s*(?P<cpu>\S+)", self.stdout, "cpu", float)

    @performance_function("MB")
    def total_juju_rss_mb(self):
        """Extract total RSS memory of all Juju-related processes."""
        rss_kb = sn.extractsingle(
            r"=== AGGREGATE ===\s*\S+\s+(?P<rss>\S+)", self.stdout, "rss", float
        )
        return rss_kb / 1024.0

    # --- disk metrics ---

    @performance_function("MB")
    def juju_disk_mb(self):
        """Extract Juju state disk usage in megabytes."""
        return sn.extractsingle(r"=== DISK_STATE ===\s*(?P<disk>\S+)", self.stdout, "disk", float)

    @performance_function("MB")
    def juju_log_disk_mb(self):
        """Extract Juju log disk usage in megabytes."""
        return sn.extractsingle(r"=== DISK_LOGS ===\s*(?P<disk>\S+)", self.stdout, "disk", float)
