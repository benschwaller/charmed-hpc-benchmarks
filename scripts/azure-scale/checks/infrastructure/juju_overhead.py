# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charmed-HPC infrastructure overhead check.

Measures the resource footprint of the Juju/charm infrastructure on each
node partition: the jujud machine agent, snapd (the charm runtime), the
aggregate of all Juju-related processes, and Juju's on-disk state and logs.

On the raw-VM baseline pipeline these metrics are zero (no jujud, no Juju
state directories), so the Juju overhead is the delta between the two
pipelines. Note that snapd exists on baseline VMs too; it is reported
separately from the jujud-specific figures for that reason.
"""

import os

import reframe as rfm
import reframe.utility.sanity as sn

_METRICS = {
    "jujud_cpu_percent": (0, None, None, "%"),
    "jujud_rss_mb": (0, None, None, "MB"),
    "jujud_threads": (0, None, None, "count"),
    "snapd_cpu_percent": (0, None, None, "%"),
    "snapd_rss_mb": (0, None, None, "MB"),
    "total_juju_cpu_percent": (0, None, None, "%"),
    "total_juju_rss_mb": (0, None, None, "MB"),
    "juju_disk_mb": (0, None, None, "MB"),
    "juju_log_disk_mb": (0, None, None, "MB"),
}

REFERENCE = {
    "azure:login": dict(_METRICS),
    "azure:hb120rs-v3": dict(_METRICS),
}

# Collection script. Written to the stage dir and executed, avoiding fragile
# nested quoting. jujud runs as "jujud-machine-N" (or "jujud-unit-...") so we
# match by command-line pattern rather than exact process name.
_COLLECT_SCRIPT = r"""#!/bin/bash
# Sum %CPU and RSS(KB) for processes whose command line matches a pattern.
sum_pattern() {
    ps -eo pcpu,rss,comm,args --no-headers \
        | awk -v pat="$1" '$0 ~ pat {cpu+=$1; rss+=$2} END {printf "%s %s\n", cpu+0, rss+0}'
}

# jujud: the machine/unit agent process.
read jujud_cpu jujud_rss < <(sum_pattern 'jujud')
jujud_threads=$(ps -eL -o comm,args --no-headers | awk '$0 ~ /jujud/' | wc -l)
echo "=== JUJUD ==="
echo "$jujud_cpu $jujud_rss $jujud_threads"

# snapd: charm runtime (also present on non-Juju hosts).
read snapd_cpu snapd_rss < <(sum_pattern '(^| )snapd')
echo "=== SNAPD ==="
echo "$snapd_cpu $snapd_rss"

# Aggregate of all Juju-related processes.
read juju_cpu juju_rss < <(sum_pattern 'juju')
echo "=== AGGREGATE ==="
echo "$juju_cpu $juju_rss"

echo "=== DISK_STATE ==="
disk_state=$(du -sm /var/lib/juju 2>/dev/null | cut -f1); echo "${disk_state:-0}"
echo "=== DISK_LOGS ==="
disk_logs=$(du -sm /var/log/juju 2>/dev/null | cut -f1); echo "${disk_logs:-0}"
echo "=== DONE ==="
"""


@rfm.simple_test
class juju_agent_overhead(rfm.RunOnlyRegressionTest):
    """Charmed-HPC infrastructure overhead (jujud, snapd, aggregate, disk)."""

    valid_systems = ["*:login", "*:hb120rs-v3"]
    valid_prog_environs = ["builtin"]
    reference = REFERENCE
    tags = {"infrastructure", "overhead"}
    descr = "Charmed-HPC infrastructure overhead (jujud, snapd, aggregate)"

    @run_before("run")
    def set_executable(self):
        """Write the collection script to the stage dir and run it."""
        script = os.path.join(self.stagedir, "collect_overhead.sh")
        with open(script, "w") as f:
            f.write(_COLLECT_SCRIPT)
        self.executable = "bash"
        self.executable_opts = [script]

    @sanity_function
    def assert_completed(self):
        """Assert metrics collection completed."""
        return sn.assert_found(r"=== DONE ===", self.stdout)

    @performance_function("%")
    def jujud_cpu_percent(self):
        """Extract jujud CPU percentage."""
        return sn.extractsingle(r"=== JUJUD ===\s*\n(?P<cpu>[0-9.]+)", self.stdout, "cpu", float)

    @performance_function("MB")
    def jujud_rss_mb(self):
        """Extract jujud RSS memory in megabytes."""
        rss_kb = sn.extractsingle(
            r"=== JUJUD ===\s*\n[0-9.]+\s+(?P<rss>[0-9.]+)", self.stdout, "rss", float
        )
        return rss_kb / 1024.0

    @performance_function("count")
    def jujud_threads(self):
        """Extract jujud thread count."""
        return sn.extractsingle(
            r"=== JUJUD ===\s*\n[0-9.]+\s+[0-9.]+\s+(?P<threads>[0-9]+)",
            self.stdout,
            "threads",
            float,
        )

    @performance_function("%")
    def snapd_cpu_percent(self):
        """Extract snapd CPU percentage."""
        return sn.extractsingle(r"=== SNAPD ===\s*\n(?P<cpu>[0-9.]+)", self.stdout, "cpu", float)

    @performance_function("MB")
    def snapd_rss_mb(self):
        """Extract snapd RSS memory in megabytes."""
        rss_kb = sn.extractsingle(
            r"=== SNAPD ===\s*\n[0-9.]+\s+(?P<rss>[0-9.]+)", self.stdout, "rss", float
        )
        return rss_kb / 1024.0

    @performance_function("%")
    def total_juju_cpu_percent(self):
        """Extract total CPU usage of all Juju-related processes."""
        return sn.extractsingle(
            r"=== AGGREGATE ===\s*\n(?P<cpu>[0-9.]+)", self.stdout, "cpu", float
        )

    @performance_function("MB")
    def total_juju_rss_mb(self):
        """Extract total RSS memory of all Juju-related processes."""
        rss_kb = sn.extractsingle(
            r"=== AGGREGATE ===\s*\n[0-9.]+\s+(?P<rss>[0-9.]+)", self.stdout, "rss", float
        )
        return rss_kb / 1024.0

    @performance_function("MB")
    def juju_disk_mb(self):
        """Extract Juju state disk usage in megabytes."""
        return sn.extractsingle(
            r"=== DISK_STATE ===\s*\n(?P<disk>[0-9.]+)", self.stdout, "disk", float
        )

    @performance_function("MB")
    def juju_log_disk_mb(self):
        """Extract Juju log disk usage in megabytes."""
        return sn.extractsingle(
            r"=== DISK_LOGS ===\s*\n(?P<disk>[0-9.]+)", self.stdout, "disk", float
        )
