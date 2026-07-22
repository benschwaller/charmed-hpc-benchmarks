# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

terraform {
  required_providers {
    juju = {
      source  = "juju/juju"
      version = ">= 0.14.0"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~>4.17"
    }
  }
}

provider "juju" {}

provider "azurerm" {
  features {}
}

# --- Variables ---

variable "hb120rs_v3_units" {
  type        = number
  default     = 2
  description = "Number of HB120rs_v3 compute nodes (CPU/RDMA partition). Scales to 64+."
}

variable "ubuntu_series" {
  type        = string
  default     = "24.04"
  description = "Ubuntu LTS series to deploy (24.04 or 26.04)."

  validation {
    condition     = contains(["24.04", "26.04"], var.ubuntu_series)
    error_message = "ubuntu_series must be either 24.04 or 26.04."
  }
}

# Compute partitions derived from CLI variables. Used by the slurmd module
# and the filesystem-client integration for_each.
locals {
  compute_partitions = {
    "hb120rs-v3" : {
      constraints = "arch=amd64 instance-type=Standard_HB120rs_v3",
      units       = var.hb120rs_v3_units,
    },
  }
}

# --- Azure networking ---

resource "azurerm_resource_group" "nfs-group" {
  name     = "azure-scale-nfs-group"
  location = "East US"
}

resource "azurerm_virtual_network" "nfs-vnet" {
  name                = "azure-scale-nfs-vnet"
  address_space       = ["10.0.0.0/16"]
  location            = azurerm_resource_group.nfs-group.location
  resource_group_name = azurerm_resource_group.nfs-group.name
  subnet              = []
}

resource "azurerm_network_security_group" "nfs-nsg" {
  name                = "azure-scale-nfs-nsg"
  location            = azurerm_resource_group.nfs-group.location
  resource_group_name = azurerm_resource_group.nfs-group.name
  security_rule {
    name                       = "Allow-SSH-Internet"
    description                = "Open SSH inbound ports"
    protocol                   = "Tcp"
    source_address_prefix      = "*"
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_range     = "22"
    access                     = "Allow"
    priority                   = 100
    direction                  = "Inbound"
  }
}

resource "azurerm_subnet" "nfs-subnet" {
  name                                          = "azure-scale-nfs-subnet"
  resource_group_name                           = azurerm_resource_group.nfs-group.name
  virtual_network_name                          = azurerm_virtual_network.nfs-vnet.name
  address_prefixes                              = ["10.0.1.0/24"]
  private_endpoint_network_policies             = "Enabled"
  private_link_service_network_policies_enabled = true
}

resource "azurerm_subnet_network_security_group_association" "nfs-nsg-to-subnet" {
  subnet_id                 = azurerm_subnet.nfs-subnet.id
  network_security_group_id = azurerm_network_security_group.nfs-nsg.id
}

# --- Juju model ---

resource "juju_model" "charmed-hpc" {
  name = "charmed-hpc"

  cloud {
    name   = "azure"
    region = "eastus"
  }

  config = {
    resource-group-name             = azurerm_resource_group.nfs-group.name
    network                         = azurerm_virtual_network.nfs-vnet.name
    storage-default-filesystem-source = "rootfs"
    default-series                  = var.ubuntu_series
  }
}

# --- NFS share (Azure storage + private endpoint, inlined because the
# upstream charmed-hpc-terraform//modules/nfs/azure wrapper is incompatible
# with the current filesystem-charms interface that requires model_uuid). ---

resource "azurerm_storage_account" "nfs" {
  name                     = "azure${substr(md5(azurerm_resource_group.nfs-group.id), 0, 8)}"
  resource_group_name      = azurerm_resource_group.nfs-group.name
  location                 = azurerm_resource_group.nfs-group.location
  account_tier             = "Premium"
  account_kind             = "FileStorage"
  account_replication_type = "LRS"

  # Azure NFS does not support HTTPS.
  https_traffic_only_enabled = false
}

resource "azurerm_storage_share" "nfs" {
  name               = "nfs-home"
  storage_account_id = azurerm_storage_account.nfs.id
  quota              = 100
  enabled_protocol   = "NFS"
}

resource "azurerm_private_dns_zone" "nfs" {
  name                = "privatelink.file.core.windows.net"
  resource_group_name = azurerm_resource_group.nfs-group.name
}

resource "azurerm_private_dns_zone_virtual_network_link" "nfs" {
  name                  = "nfs-dz-vnet-link"
  resource_group_name   = azurerm_resource_group.nfs-group.name
  private_dns_zone_name = azurerm_private_dns_zone.nfs.name
  virtual_network_id    = azurerm_virtual_network.nfs-vnet.id
}

resource "azurerm_private_endpoint" "nfs" {
  name                = "azure-scale-nfs-endpoint"
  location            = azurerm_resource_group.nfs-group.location
  resource_group_name = azurerm_resource_group.nfs-group.name
  subnet_id           = azurerm_subnet.nfs-subnet.id

  private_service_connection {
    name                           = "nfs-privateserviceconnection"
    private_connection_resource_id = azurerm_storage_account.nfs.id
    subresource_names              = ["file"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "nfs-dz-group"
    private_dns_zone_ids = [azurerm_private_dns_zone.nfs.id]
  }
}

module "nfs-server-proxy" {
  source    = "git::https://github.com/canonical/filesystem-charms//charms/nfs-server-proxy/terraform"
  app_name  = "nfs-server-proxy"
  model_uuid = juju_model.charmed-hpc.uuid
  config = {
    hostname = azurerm_storage_account.nfs.primary_file_host
    path     = "/${azurerm_storage_account.nfs.name}/${azurerm_storage_share.nfs.name}"
  }
  depends_on = [azurerm_private_endpoint.nfs]
}

module "filesystem-client" {
  source    = "git::https://github.com/canonical/filesystem-charms//charms/filesystem-client/terraform"
  app_name  = "filesystem-client"
  model_uuid = juju_model.charmed-hpc.uuid
  config = {
    mountpoint = "/nfs/home"
  }
}

resource "juju_integration" "nfs-server-proxy-to-filesystem-client" {
  model_uuid = juju_model.charmed-hpc.uuid

  application {
    name     = "nfs-server-proxy"
    endpoint = module.nfs-server-proxy.provides.filesystem
  }

  application {
    name     = "filesystem-client"
    endpoint = module.filesystem-client.requires.filesystem
  }
}

# --- MySQL (backing database for Slurm accounting) ---

module "mysql" {
  # Pin ref=main: the repo's default branch was changed to "readme" (no code).
  source = "git::https://github.com/canonical/mysql-operator//terraform?ref=main"

  model    = juju_model.charmed-hpc.uuid
  app_name = "mysql"
  channel  = "8.0/stable"
  units    = 1
}

# --- Slurm cluster (inlined because the upstream
# charmed-hpc-terraform//modules/slurm wrapper passes model_name to
# slurm-charms submodules that now require model_uuid). ---

module "slurmctld" {
  source     = "git::https://github.com/canonical/slurm-charms//charms/slurmctld/terraform"
  app_name   = "slurmctld"
  model_uuid = juju_model.charmed-hpc.uuid
}

module "slurmdbd" {
  source     = "git::https://github.com/canonical/slurm-charms//charms/slurmdbd/terraform"
  app_name   = "slurmdbd"
  model_uuid = juju_model.charmed-hpc.uuid
}

module "slurmrestd" {
  source     = "git::https://github.com/canonical/slurm-charms//charms/slurmrestd/terraform"
  app_name   = "slurmrestd"
  model_uuid = juju_model.charmed-hpc.uuid
}

module "sackd" {
  source     = "git::https://github.com/canonical/slurm-charms//charms/sackd/terraform"
  app_name   = "login"
  model_uuid = juju_model.charmed-hpc.uuid
  units      = 1
}

module "slurmd" {
  source      = "git::https://github.com/canonical/slurm-charms//charms/slurmd/terraform"
  for_each    = local.compute_partitions
  app_name    = each.key
  model_uuid  = juju_model.charmed-hpc.uuid
  units       = each.value.units
  constraints = each.value.constraints
}

resource "juju_integration" "sackd-to-slurmctld" {
  model_uuid = juju_model.charmed-hpc.uuid

  application {
    name     = "login"
    endpoint = module.sackd.provides.slurmctld
  }

  application {
    name     = "slurmctld"
    endpoint = module.slurmctld.requires.sackd
  }
}

resource "juju_integration" "slurmdbd-to-slurmctld" {
  model_uuid = juju_model.charmed-hpc.uuid

  application {
    name     = "slurmdbd"
    endpoint = module.slurmdbd.provides.slurmctld
  }

  application {
    name     = "slurmctld"
    endpoint = module.slurmctld.requires.slurmdbd
  }
}

resource "juju_integration" "slurmrestd-to-slurmctld" {
  model_uuid = juju_model.charmed-hpc.uuid

  application {
    name     = "slurmrestd"
    endpoint = module.slurmrestd.provides.slurmctld
  }

  application {
    name     = "slurmctld"
    endpoint = module.slurmctld.requires.slurmrestd
  }
}

resource "juju_integration" "slurmdbd-to-mysql" {
  model_uuid = juju_model.charmed-hpc.uuid

  application {
    name     = "slurmdbd"
    endpoint = module.slurmdbd.requires.database
  }

  application {
    name     = module.mysql.app_name
    endpoint = module.mysql.provides.database
  }
}

resource "juju_integration" "slurmd-to-slurmctld" {
  model_uuid = juju_model.charmed-hpc.uuid
  for_each   = local.compute_partitions

  application {
    name     = each.key
    endpoint = module.slurmd[each.key].provides.slurmctld
  }

  application {
    name     = "slurmctld"
    endpoint = module.slurmctld.requires.slurmd
  }
}

# --- NFS integrations ---

resource "juju_integration" "login-to-filesystem-client" {
  model_uuid = juju_model.charmed-hpc.uuid

  application {
    name     = "login"
    endpoint = "juju-info"
  }

  application {
    name     = "filesystem-client"
    endpoint = module.filesystem-client.requires.juju_info
  }
}

resource "juju_integration" "compute-to-filesystem-client" {
  model_uuid = juju_model.charmed-hpc.uuid
  for_each   = local.compute_partitions

  application {
    name     = each.key
    endpoint = "juju-info"
  }

  application {
    name     = "filesystem-client"
    endpoint = module.filesystem-client.requires.juju_info
  }
}

# --- Outputs ---

output "model_name" {
  value = juju_model.charmed-hpc.name
}

output "compute_node_count" {
  value = var.hb120rs_v3_units
}

output "ubuntu_series" {
  value = var.ubuntu_series
}
