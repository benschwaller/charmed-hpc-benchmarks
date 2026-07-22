#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Manual Slurm installation script for raw Azure VMs (no Juju/charms).
# Sets up Munge, Slurm, NFS, and MPI on a set of CPU VMs. This provides the
# raw-VM baseline that the Juju pipeline is compared against.
#
# Usage: setup_slurm.sh <login_ip> <compute_count> <ubuntu_series>
#   login_ip        - public IP of the login/controller VM
#   compute_count   - number of compute nodes
#   ubuntu_series   - 24.04 or 26.04

set -e

LOGIN_IP="$1"
COMPUTE_COUNT="$2"
UBUNTU_SERIES="${3:-24.04}"

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=30"

echo "[SETUP] Login IP: $LOGIN_IP"
echo "[SETUP] Compute nodes: $COMPUTE_COUNT"
echo "[SETUP] Ubuntu series: $UBUNTU_SERIES"

# --- Step 1: Install Munge on login node ---
echo "[SETUP] Installing Munge on login node..."
ssh $SSH_OPTS "ubuntu@$LOGIN_IP" << "EOF"
set -e
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y munge slurm-wlm
# Generate a munge key if one does not already exist.
if [ ! -f /etc/munge/munge.key ]; then
    sudo /usr/sbin/mungekey --create 2>/dev/null || sudo create-munge-key -f
fi
sudo systemctl enable --now munge
# Stage the key for distribution to compute nodes.
sudo cp /etc/munge/munge.key /tmp/munge.key
sudo chmod 644 /tmp/munge.key
EOF

# --- Step 2: Discover compute node private IPs from the login node ---
echo "[SETUP] Discovering compute node IPs..."
COMPUTE_IPS=()
for i in $(seq 0 $((COMPUTE_COUNT - 1))); do
    ip=$(ssh $SSH_OPTS "ubuntu@$LOGIN_IP" \
        "getent ahostsv4 baseline-compute-${i} 2>/dev/null | awk 'NR==1{print \$1}'" \
        2>/dev/null || echo "")
    if [ -z "$ip" ]; then
        echo "[SETUP] ERROR: could not resolve baseline-compute-${i} from the login node."
        echo "[SETUP] Ensure Azure-provided internal DNS is resolving VM hostnames."
        exit 1
    fi
    COMPUTE_IPS+=("$ip")
    echo "[SETUP] Found compute-${i} at ${ip}"
done

# --- Step 3: Install Munge + MPI on compute nodes (key from login) ---
echo "[SETUP] Installing Munge and MPI on compute nodes..."
scp $SSH_OPTS "ubuntu@$LOGIN_IP:/tmp/munge.key" /tmp/munge.key
for ip in "${COMPUTE_IPS[@]}"; do
    scp $SSH_OPTS /tmp/munge.key "ubuntu@$ip:/tmp/munge.key"
    ssh $SSH_OPTS "ubuntu@$ip" << "EOF"
set -e
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    munge slurm-wlm openmpi-bin libopenmpi-dev libopenblas-dev
sudo cp /tmp/munge.key /etc/munge/munge.key
sudo chown munge:munge /etc/munge/munge.key
sudo chmod 400 /etc/munge/munge.key
sudo systemctl enable --now munge
EOF
done

# --- Step 4: Configure NFS (login exports /nfs/home, compute mounts it) ---
echo "[SETUP] Configuring NFS..."
ssh $SSH_OPTS "ubuntu@$LOGIN_IP" << "EOF"
set -e
sudo apt-get install -y nfs-kernel-server
sudo mkdir -p /nfs/home
sudo chown ubuntu:ubuntu /nfs/home
echo "/nfs/home *(rw,sync,no_subtree_check,no_root_squash)" | sudo tee /etc/exports
sudo exportfs -ra
sudo systemctl enable --now nfs-kernel-server
EOF

for ip in "${COMPUTE_IPS[@]}"; do
    ssh $SSH_OPTS "ubuntu@$ip" << EOF
set -e
sudo apt-get install -y nfs-common
sudo mkdir -p /nfs/home
sudo mount ${LOGIN_IP}:/nfs/home /nfs/home
echo "${LOGIN_IP}:/nfs/home /nfs/home nfs defaults 0 0" | sudo tee -a /etc/fstab
EOF
done

# --- Step 5: Generate and distribute Slurm config ---
# Note: accounting via slurmdbd is intentionally omitted for the baseline;
# sacct-based concurrency checks rely on job accounting text records written
# by slurmctld, which works without a database backend.
echo "[SETUP] Generating Slurm configuration..."
ssh $SSH_OPTS "ubuntu@$LOGIN_IP" << EOF
set -e
sudo mkdir -p /etc/slurm /var/spool/slurmctld /var/spool/slurmd

sudo tee /etc/slurm/cgroup.conf << "CGROUPCONF"
CgroupPlugin=cgroup/v2
ConstrainCores=yes
ConstrainRAMSpace=yes
CGROUPCONF

sudo tee /etc/slurm/slurm.conf << "SLURMCONF"
ClusterName=baseline
SlurmctldHost=baseline-login
SlurmctldPort=6817
SlurmdPort=6818
AuthType=auth/munge
StateSaveLocation=/var/spool/slurmctld
SlurmdSpoolDir=/var/spool/slurmd
SlurmUser=slurm
SlurmdUser=root
SlurmdTimeout=300
SlurmctldTimeout=120
ReturnToService=2
SchedulerType=sched/backfill
SelectType=select/cons_tres
SelectTypeParameters=CR_Core
TaskPlugin=task/cgroup,task/affinity
ProctrackType=proctrack/cgroup
PropagateResourceLimits=ALL
SLURMCONF

# Append node and partition definitions (120 cores per HB120rs_v3).
for i in \$(seq 0 \$((${COMPUTE_COUNT} - 1))); do
    echo "NodeName=baseline-compute-\${i} CPUs=120 State=UNKNOWN" | sudo tee -a /etc/slurm/slurm.conf
done
echo "PartitionName=hb120rs-v3 Nodes=baseline-compute-[0-\$((${COMPUTE_COUNT} - 1))] Default=YES MaxTime=24:00:00 State=UP" | sudo tee -a /etc/slurm/slurm.conf

sudo systemctl enable --now slurmctld
EOF

# Distribute slurm.conf/cgroup.conf and start slurmd on compute nodes.
scp $SSH_OPTS "ubuntu@$LOGIN_IP:/etc/slurm/slurm.conf" /tmp/slurm.conf
scp $SSH_OPTS "ubuntu@$LOGIN_IP:/etc/slurm/cgroup.conf" /tmp/cgroup.conf
for ip in "${COMPUTE_IPS[@]}"; do
    scp $SSH_OPTS /tmp/slurm.conf "ubuntu@$ip:/tmp/slurm.conf"
    scp $SSH_OPTS /tmp/cgroup.conf "ubuntu@$ip:/tmp/cgroup.conf"
    ssh $SSH_OPTS "ubuntu@$ip" << "EOF"
set -e
sudo mkdir -p /etc/slurm /var/spool/slurmd
sudo cp /tmp/slurm.conf /etc/slurm/slurm.conf
sudo cp /tmp/cgroup.conf /etc/slurm/cgroup.conf
sudo systemctl enable --now slurmd
EOF
done

# --- Step 6: Install build tools + ReFrame venv on login node ---
echo "[SETUP] Installing build tools on login node..."
ssh $SSH_OPTS "ubuntu@$LOGIN_IP" << "EOF"
set -e
sudo apt-get install -y build-essential python3-venv git
python3 -m venv /nfs/home/reframe-venv
source /nfs/home/reframe-venv/bin/activate
pip install ReFrame-HPC
echo "ReFrame installed successfully"
EOF

echo "[SETUP] Slurm cluster setup complete!"
echo "[SETUP] Login node: $LOGIN_IP"
echo "[SETUP] Compute nodes: ${#COMPUTE_IPS[@]}"
