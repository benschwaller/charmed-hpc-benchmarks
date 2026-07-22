# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""ReFrame site configuration for scalable Azure CPU benchmarking.

The suite targets the RDMA-enabled Standard_HB120rs_v3 CPU partition. The
Ubuntu series is recorded from the UBUNTU_SERIES environment variable
(default: "24.04") purely for descriptive purposes; it does not change the
ReFrame partition structure.
"""

import os

UBUNTU_SERIES = os.environ.get("UBUNTU_SERIES", "24.04")

partitions = [
    {
        "name": "login",
        "descr": "Cluster login nodes",
        "launcher": "local",
        "environs": ["builtin"],
        "scheduler": "local",
    },
    {
        "name": "hb120rs-v3",
        "descr": "Standard_HB120rs_v3 instances - RDMA-enabled partition",
        "launcher": "mpirun",
        "environs": ["builtin", "mpi-gnu"],
        "access": ["--partition=hb120rs-v3"],
        "scheduler": "slurm",
        "time_limit": "2h",
        "max_jobs": 100,
    },
]

site_configuration = {
    "systems": [
        {
            "name": "azure",
            "descr": f"Microsoft Azure scalable CPU benchmarking cluster (Ubuntu {UBUNTU_SERIES})",
            "hostnames": ["juju"],
            "modules_system": "nomod",
            "partitions": partitions,
        },
    ],
    "environments": [
        {
            "name": "mpi-gnu",
            "cc": "mpicc",
            "cxx": "mpicxx",
            "ftn": "mpif90",
            "target_systems": ["azure"],
            "features": ["mpi"],
        },
        {
            "name": "builtin",
            "cc": "cc",
            "cxx": "CC",
            "ftn": "ftn",
            "target_systems": ["azure"],
        },
    ],
}
