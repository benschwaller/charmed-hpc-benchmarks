# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""ReFrame site configuration for scalable Azure benchmarking.

This configuration is GPU-aware: the nc4as-t4-v3 partition is only
included when the ENABLE_GPU environment variable is set to "true"
(default). Set ENABLE_GPU=false for CPU-only deployments.

The ubuntu series is controlled by the UBUNTU_SERIES environment
variable (default: "24.04"). This affects package naming expectations
in check prerun commands but does not change the ReFrame partition
structure.
"""

import os

ENABLE_GPU = os.environ.get("ENABLE_GPU", "true").lower() == "true"
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

if ENABLE_GPU:
    partitions.append(
        {
            "name": "nc4as-t4-v3",
            "descr": "Standard_NC4as_T4_v3 instances - Tesla T4 GPU-equipped partition (multi-node NCCL)",
            "launcher": "mpirun",
            "environs": ["builtin", "cuda"],
            "access": ["--partition=nc4as-t4-v3", "--gres=gpu:1"],
            "scheduler": "slurm",
            "time_limit": "2h",
            "max_jobs": 100,
        }
    )

site_configuration = {
    "systems": [
        {
            "name": "azure",
            "descr": f"Microsoft Azure scalable benchmarking cluster (Ubuntu {UBUNTU_SERIES})",
            "hostnames": ["juju"],
            "modules_system": "nomod",
            "partitions": partitions,
        },
    ],
    "environments": [
        {
            "name": "cuda",
            "cc": "nvcc",
            "cxx": "nvcc",
            "target_systems": ["azure"],
            "features": ["cuda"],
        },
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
