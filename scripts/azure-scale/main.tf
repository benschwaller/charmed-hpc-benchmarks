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

variable "enable_gpu" {
  type        = bool
  default     = true
  description = "Whether to deploy the nc4as-t4-v3 GPU partition."
}

variable "nc4as_t4_v3_units" {
  type        = number
  default     = 1
  description = "Number of NC4as_T4_v3 GPU compute nodes. Scales for multi-node GPU HPL with NCCL."
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

# --- NFS share ---

module "nfs-share" {
  source = "git::https://github.com/canonical/charmed-hpc-terraform//modules/azure-managed-nfs"

  name                = "azure-scale-nfs-share"
  resource_group_name = azurerm_resource_group.nfs-group.name
  subnet_info = {
    name                 = azurerm_subnet.nfs-subnet.name
    virtual_network_name = azurerm_subnet.nfs-subnet.virtual_network_name
  }
  model_name = juju_model.charmed-hpc.name
  quota      = 100
  mountpoint = "/nfs/home"
  depends_on = [
    azurerm_resource_group.nfs-group
  ]
}

# --- MySQL (backing database for Slurm accounting) ---

module "mysql" {
  source = "git::https://github.com/canonical/mysql-operator//terraform"

  juju_model_name = juju_model.charmed-hpc.name
  app_name        = "mysql"
  channel         = "8.0/stable"
  units           = 1
}

# --- Slurm cluster ---

module "slurm" {
  source = "git::https://github.com/canonical/charmed-hpc-terraform//modules/slurm"

  model_name = juju_model.charmed-hpc.name
  database_backend = {
    name     = module.mysql.application_name,
    endpoint = module.mysql.provides.database
  }

  controller = {
    app_name = "slurmctld"
  }

  database = {
    app_name = "slurmdbd"
  }

  rest_api = {
    app_name = "slurmrestd"
  }

  kiosk = {
    app_name = "login",
    units    = 1,
  }

  compute_partitions = {
    "hb120rs-v3" : {
      constraints = "arch=amd64 instance-type=Standard_HB120rs_v3",
      units       = var.hb120rs_v3_units,
    },
    "nc4as-t4-v3" : {
      constraints = "arch=amd64 instance-type=Standard_NC4as_T4_v3",
      units       = var.enable_gpu ? var.nc4as_t4_v3_units : 0,
    }
  }
  depends_on = [
    juju_model.charmed-hpc
  ]
}

# --- NFS integrations ---

resource "juju_integration" "login-to-filesystem-client" {
  model = juju_model.charmed-hpc.name

  application {
    name     = module.slurm.kiosk.app_name
    endpoint = "juju-info"
  }

  application {
    name     = module.nfs-share.app_name
    endpoint = "juju-info"
  }
}

resource "juju_integration" "compute-to-filesystem-client" {
  model    = juju_model.charmed-hpc.name
  for_each = module.slurm.compute_partitions

  application {
    name     = each.key
    endpoint = "juju-info"
  }

  application {
    name     = module.nfs-share.app_name
    endpoint = "juju-info"
  }
}

# --- Outputs ---

output "model_name" {
  value = juju_model.charmed-hpc.name
}

output "compute_node_count" {
  value = var.hb120rs_v3_units
}

output "gpu_enabled" {
  value = var.enable_gpu
}

output "gpu_node_count" {
  value = var.enable_gpu ? var.nc4as_t4_v3_units : 0
}

output "ubuntu_series" {
  value = var.ubuntu_series
}
