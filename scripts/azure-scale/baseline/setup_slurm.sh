#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Manual Slurm installation script for raw Azure VMs (no Juju/charms).
# Sets up Slurm, Munge, NFS, MPI, and optional CUDA/NCCL on a set of VMs.
#
# Usage: setup_slurm.sh <login_ip> <compute_count> <gpu_enabled> <gpu_nodes> <ubuntu_series>
#   login_ip        - IP of the login/controller VM
#   compute_count   - Number of compute nodes
#   gpu_enabled     - true/false
#   gpu_nodes       - Number of GPU nodes (for multi-node GPU HPL)
#   ubuntu_series   - 24.04 or 26.04

set -e

LOGIN_IP="$1"
COMPUTE_COUNT="$2"
GPU_ENABLED="$3"
GPU_NODES="${4:-1}"
UBUNTU_SERIES="${5:-24.04}"

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=30"

echo "[SETUP] Login IP: $LOGIN_IP"
echo "[SETUP] Compute nodes: $COMPUTE_COUNT"
echo "[SETUP] GPU enabled: $GPU_ENABLED"
echo "[SETUP] GPU nodes: $GPU_NODES"
echo "[SETUP] Ubuntu series: $UBUNTU_SERIES"

# --- Step 1: Install Munge on login node ---
echo "[SETUP] Installing Munge on login node..."
ssh $SSH_OPTS ubuntu@$LOGIN_IP << "EOF"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y munge slurm-wlm
# Generate munge key
sudo mungekey || sudo /usr/sbin/mungekey || sudo create-munge-key
sudo systemctl enable munge
sudo systemctl start munge
# Copy munge key for distribution
sudo cp /etc/munge/munge.key /tmp/munge.key
sudo chmod 644 /tmp/munge.key
EOF

# --- Step 2: Discover compute node IPs ---
echo "[SETUP] Discovering compute node IPs..."
COMPUTE_IPS=()
for i in $(seq 0 $((COMPUTE_COUNT - 1))); do
    # Azure VMs are named baseline-compute-0, baseline-compute-1, etc.
    # We resolve their IPs from the login node
    ip=$(ssh $SSH_OPTS ubuntu@$LOGIN_IP "getent hosts baseline-compute-${i} | awk '{print \$1}'" 2>/dev/null || echo "")
    if [ -z "$ip" ]; then
        echo "[SETUP] WARNING: Could not resolve baseline-compute-${i}, trying alternate method..."
        ip=$(ssh $SSH_OPTS ubuntu@$LOGIN_IP "ping -c 1 baseline-compute-${i} 2>/dev/null | head -1 | grep -oP '\(\K[0-9.]+'" 2>/dev/null || echo "")
    fi
    if [ -z "$ip" ]; then
        echo "[SETUP] WARNING: Could not resolve compute node ${i}, skipping"
        continue
    fi
    COMPUTE_IPS+=("$ip")
    echo "[SETUP] Found compute-${i} at ${ip}"
done

# --- Step 3: Install Munge on compute nodes ---
echo "[SETUP] Installing Munge on compute nodes..."
for ip in "${COMPUTE_IPS[@]}"; do
    # Copy munge key from login to compute
    scp $SSH_OPTS ubuntu@$LOGIN_IP:/tmp/munge.key /tmp/munge.key
    scp $SSH_OPTS /tmp/munge.key ubuntu@$ip:/tmp/munge.key
    ssh $SSH_OPTS ubuntu@$ip << "EOF"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y munge slurm-wlm openmpi-bin libopenmpi-dev libopenblas-dev
sudo cp /tmp/munge.key /etc/munge/munge.key
sudo chown munge:munge /etc/munge/munge.key
sudo chmod 400 /etc/munge/munge.key
sudo systemctl enable munge
sudo systemctl start munge
EOF
done

# --- Step 4: Configure NFS (login exports /home, compute mounts) ---
echo "[SETUP] Configuring NFS..."
ssh $SSH_OPTS ubuntu@$LOGIN_IP << "EOF"
sudo apt-get install -y nfs-kernel-server
sudo mkdir -p /nfs/home
sudo chown ubuntu:ubuntu /nfs/home
echo "/nfs/home *(rw,sync,no_subtree_check,no_root_squash)" | sudo tee /etc/exports
sudo exportfs -ra
sudo systemctl enable nfs-kernel-server
sudo systemctl restart nfs-kernel-server
EOF

for ip in "${COMPUTE_IPS[@]}"; do
    ssh $SSH_OPTS ubuntu@$ip << EOF
sudo apt-get install -y nfs-common
sudo mkdir -p /nfs/home
sudo mount ${LOGIN_IP}:/nfs/home /nfs/home
echo "${LOGIN_IP}:/nfs/home /nfs/home nfs defaults 0 0" | sudo tee -a /etc/fstab
EOF
done

# --- Step 5: Generate and distribute Slurm config ---
echo "[SETUP] Generating Slurm configuration..."
ssh $SSH_OPTS ubuntu@$LOGIN_IP << EOF
COMPUTE_COUNT=${COMPUTE_COUNT}
sudo mkdir -p /etc/slurm

# Generate slurm.conf
sudo tee /etc/slurm/slurm.conf << "SLURMCONF"
ClusterName=baseline
SlurmctldHost=baseline-login
SlurmctldPort=6817
SlurmdPort=6818
AuthType=auth/munge
StateSaveLocation=/var/spool/slurmctld
SlurmdSpoolDir=/var/spool/slurmd
SlurmdUser=root
SlurmdTimeout=300
SlurmctldTimeout=120
ReturnToService=2
SchedulerType=sched/backfill
SelectType=select/cons_tres
SelectTypeParameters=CR_Core
TaskPlugin=task/cgroup
ProctrackType=proctrack/linuxproc
PropagateResourceLimits=ALL
AccountingStorageType=accounting/slurmdbd
AccountingStorageHost=baseline-login
JobAcctGatherType=jobacct_gather/linux
SLURMCONF

# Add compute nodes (120 cores per HB120rs_v3)
for i in \$(seq 0 \$((COMPUTE_COUNT - 1))); do
    echo "NodeName=baseline-compute-\${i} CPUs=120 State=UNKNOWN" | sudo tee -a /etc/slurm/slurm.conf
done

# Add partition
echo "PartitionName=hb120rs-v3 Nodes=baseline-compute-[0-\$((COMPUTE_COUNT - 1))] Default=YES MaxTime=24:00:00 State=UP" | sudo tee -a /etc/slurm/slurm.conf

# Start slurmctld
sudo systemctl enable slurmctld
sudo systemctl start slurmctld
EOF

# Distribute slurm.conf and start slurmd on compute nodes
for ip in "${COMPUTE_IPS[@]}"; do
    scp $SSH_OPTS ubuntu@$LOGIN_IP:/etc/slurm/slurm.conf /tmp/slurm.conf
    scp $SSH_OPTS /tmp/slurm.conf ubuntu@$ip:/tmp/slurm.conf
    ssh $SSH_OPTS ubuntu@$ip << "EOF"
sudo mkdir -p /etc/slurm
sudo cp /tmp/slurm.conf /etc/slurm/slurm.conf
sudo mkdir -p /var/spool/slurmd
sudo systemctl enable slurmd
sudo systemctl start slurmd
EOF
done

# --- Step 6: Optional GPU setup ---
if [ "$GPU_ENABLED" = "true" ]; then
    echo "[SETUP] Setting up GPU node..."
    GPU_IP=$(ssh $SSH_OPTS ubuntu@$LOGIN_IP "getent hosts baseline-gpu | awk '{print \$1}'" 2>/dev/null || echo "")
    if [ -n "$GPU_IP" ]; then
        # Copy munge key and slurm config to GPU node
        scp $SSH_OPTS ubuntu@$LOGIN_IP:/tmp/munge.key /tmp/munge.key
        scp $SSH_OPTS ubuntu@$LOGIN_IP:/etc/slurm/slurm.conf /tmp/slurm.conf
        scp $SSH_OPTS /tmp/munge.key ubuntu@$GPU_IP:/tmp/munge.key
        scp $SSH_OPTS /tmp/slurm.conf ubuntu@$GPU_IP:/tmp/slurm.conf

        # Add GPU node to slurm.conf on login
        ssh $SSH_OPTS ubuntu@$LOGIN_IP << EOF
echo "NodeName=baseline-gpu CPUs=4 Gres=gpu:1 State=UNKNOWN" | sudo tee -a /etc/slurm/slurm.conf
echo "PartitionName=nc4as-t4-v3 Nodes=baseline-gpu Default=NO MaxTime=24:00:00 State=UP" | sudo tee -a /etc/slurm/slurm.conf
sudo systemctl restart slurmctld
EOF

        ssh $SSH_OPTS ubuntu@$GPU_IP << "EOF"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y munge slurm-wlm libcublas12 libnccl2 libnccl-dev
sudo cp /tmp/munge.key /etc/munge/munge.key
sudo chown munge:munge /etc/munge/munge.key
sudo chmod 400 /etc/munge/munge.key
sudo systemctl enable munge
sudo systemctl start munge
sudo mkdir -p /etc/slurm /var/spool/slurmd
sudo cp /tmp/slurm.conf /etc/slurm/slurm.conf
sudo systemctl enable slurmd
sudo systemctl start slurmd
EOF

        echo "[SETUP] GPU node configured at $GPU_IP"
    else
        echo "[SETUP] WARNING: Could not resolve GPU node, skipping GPU setup"
    fi
fi

# --- Step 7: Install build tools on login node ---
echo "[SETUP] Installing build tools on login node..."
ssh $SSH_OPTS ubuntu@$LOGIN_IP << "EOF"
sudo apt-get install -y build-essential python3-venv git nvidia-cuda-toolkit-gcc
# Install ReFrame
python3 -m venv /nfs/home/reframe-venv
source /nfs/home/reframe-venv/bin/activate
pip install ReFrame-HPC
echo "ReFrame installed successfully"
EOF

echo "[SETUP] Slurm cluster setup complete!"
echo "[SETUP] Login node: $LOGIN_IP"
echo "[SETUP] Compute nodes: ${#COMPUTE_IPS[@]}"
if [ "$GPU_ENABLED" = "true" ]; then
    echo "[SETUP] GPU node: configured"
fi
