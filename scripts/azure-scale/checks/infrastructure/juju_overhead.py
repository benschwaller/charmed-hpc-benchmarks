# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Juju agent runtime overhead check.

Measures the resource footprint of the Juju agent (jujud) on each
node partition. Reports CPU percentage, RSS memory, disk usage, and
thread count as performance metrics.
"""

import reframe as rfm
import reframe.utility.sanity as sn

REFERENCE = {
    "azure:login": {
        "jujud_cpu_percent": (0, None, None, "%"),
        "jujud_rss_mb": (0, None, None, "MB"),
        "juju_disk_mb": (0, None, None, "MB"),
        "jujud_threads": (0, None, None, "count"),
    },
    "azure:hb120rs-v3": {
        "jujud_cpu_percent": (0, None, None, "%"),
        "jujud_rss_mb": (0, None, None, "MB"),
        "juju_disk_mb": (0, None, None, "MB"),
        "jujud_threads": (0, None, None, "count"),
    },
    "azure:nc4as-t4-v3": {
        "jujud_cpu_percent": (0, None, None, "%"),
        "jujud_rss_mb": (0, None, None, "MB"),
        "juju_disk_mb": (0, None, None, "MB"),
        "jujud_threads": (0, None, None, "count"),
    },
}


@rfm.simple_test
class juju_agent_overhead(rfm.RunOnlyRegressionTest):
    """Juju agent (jujud) resource overhead."""

    valid_systems = ["*:login", "*:hb120rs-v3", "*:nc4as-t4-v3"]
    valid_prog_environs = ["builtin"]
    reference = REFERENCE
    tags = {"infrastructure", "overhead"}
    descr = "Juju agent (jujud) resource overhead"

    @run_before("run")
    def set_executable(self):
        """Set job script to collect jujud resource metrics."""
        self.executable = "bash"
        self.executable_opts = ["-l", "-c"]
        self.executable_opts.append(
            '"'
            'echo "=== CPU ==="; '
            'ps -o %cpu,rss,nlwp -C jujud --no-headers 2>/dev/null || echo "0 0 0"; '
            'echo "=== DISK ==="; '
            'du -sm /var/lib/juju 2>/dev/null | cut -f1 || echo "0"; '
            'echo "=== DONE ==="'
            '"'
        )

    @sanity_function
    def assert_jujud_found(self):
        """Assert jujud metrics collection completed."""
        return sn.assert_found(r"=== DONE ===", self.stdout)

    @performance_function("%")
    def jujud_cpu_percent(self):
        """Extract jujud CPU percentage."""
        return sn.extractsingle(r"=== CPU ===\s*(?P<cpu>\S+)", self.stdout, "cpu", float)

    @performance_function("MB")
    def jujud_rss_mb(self):
        """Extract jujud RSS memory in megabytes."""
        rss_kb = sn.extractsingle(r"=== CPU ===\s*\S+\s+(?P<rss>\S+)", self.stdout, "rss", float)
        return rss_kb / 1024.0

    @performance_function("count")
    def jujud_threads(self):
        """Extract jujud thread count."""
        return sn.extractsingle(
            r"=== CPU ===\s*\S+\s+\S+\s+(?P<threads>\S+)", self.stdout, "threads", float
        )

    @performance_function("MB")
    def juju_disk_mb(self):
        """Extract Juju on-disk usage in megabytes."""
        return sn.extractsingle(r"=== DISK ===\s*(?P<disk>\S+)", self.stdout, "disk", float)
