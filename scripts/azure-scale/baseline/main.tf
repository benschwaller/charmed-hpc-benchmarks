# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~>4.17"
    }
  }
}

provider "azurerm" {
  features {}
}

# --- Variables ---

variable "compute_nodes" {
  type        = number
  default     = 2
  description = "Number of HB120rs_v3 compute nodes."
}

variable "enable_gpu" {
  type        = bool
  default     = true
  description = "Whether to deploy a GPU node."
}

variable "ubuntu_series" {
  type        = string
  default     = "24.04"
  description = "Ubuntu LTS series. 24.04 or 26.04."

  validation {
    condition     = contains(["24.04", "26.04"], var.ubuntu_series)
    error_message = "ubuntu_series must be either 24.04 or 26.04."
  }
}

locals {
  ubuntu_image = {
    "24.04" = "ubuntu-24_04-lts"
    "26.04" = "ubuntu-26_04-lts"
  }[var.ubuntu_series]

  login_vm_size  = "Standard_D4s_v5"
  compute_vm_size = "Standard_HB120rs_v3"
  gpu_vm_size     = "Standard_NC4as_T4_v3"
}

# --- Resource group & networking ---

resource "azurerm_resource_group" "baseline" {
  name     = "baseline-hpc-rg"
  location = "East US"
}

resource "azurerm_virtual_network" "baseline" {
  name                = "baseline-vnet"
  address_space       = ["10.0.0.0/16"]
  location            = azurerm_resource_group.baseline.location
  resource_group_name = azurerm_resource_group.baseline.name
}

resource "azurerm_subnet" "baseline" {
  name                 = "baseline-subnet"
  resource_group_name  = azurerm_resource_group.baseline.name
  virtual_network_name = azurerm_virtual_network.baseline.name
  address_prefixes     = ["10.0.1.0/24"]
}

resource "azurerm_network_security_group" "baseline" {
  name                = "baseline-nsg"
  location            = azurerm_resource_group.baseline.location
  resource_group_name = azurerm_resource_group.baseline.name

  security_rule {
    name                       = "SSH"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "Slurm"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "6817-6820"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

# --- Login node ---

resource "azurerm_public_ip" "login" {
  name                = "baseline-login-pip"
  location            = azurerm_resource_group.baseline.location
  resource_group_name = azurerm_resource_group.baseline.name
  allocation_method   = "Static"
}

resource "azurerm_network_interface" "login" {
  name                = "baseline-login-nic"
  location            = azurerm_resource_group.baseline.location
  resource_group_name = azurerm_resource_group.baseline.name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.baseline.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.login.id
  }
}

resource "azurerm_linux_virtual_machine" "login" {
  name                  = "baseline-login"
  location              = azurerm_resource_group.baseline.location
  resource_group_name   = azurerm_resource_group.baseline.name
  size                  = local.login_vm_size
  admin_username        = "ubuntu"
  network_interface_ids = [azurerm_network_interface.login.id]

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = local.ubuntu_image
    version   = "latest"
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
  }

  admin_ssh_key {
    username   = "ubuntu"
    public_key = fileexists("~/.ssh/id_rsa.pub") ? file("~/.ssh/id_rsa.pub") : var.ssh_public_key
  }
}

variable "ssh_public_key" {
  type        = string
  default     = ""
  description = "SSH public key for VM access. Falls back to ~/.ssh/id_rsa.pub."
}

# --- Compute nodes ---

resource "azurerm_network_interface" "compute" {
  count               = var.compute_nodes
  name                = "baseline-compute-nic-${count.index}"
  location            = azurerm_resource_group.baseline.location
  resource_group_name = azurerm_resource_group.baseline.name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.baseline.id
    private_ip_address_allocation = "Dynamic"
  }
}

resource "azurerm_linux_virtual_machine" "compute" {
  count                = var.compute_nodes
  name                 = "baseline-compute-${count.index}"
  location             = azurerm_resource_group.baseline.location
  resource_group_name = azurerm_resource_group.baseline.name
  size                 = local.compute_vm_size
  admin_username       = "ubuntu"
  network_interface_ids = [azurerm_network_interface.compute[count.index].id]

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = local.ubuntu_image
    version   = "latest"
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
  }

  admin_ssh_key {
    username   = "ubuntu"
    public_key = var.ssh_public_key != "" ? var.ssh_public_key : file("~/.ssh/id_rsa.pub")
  }
}

# --- GPU node (conditional) ---

resource "azurerm_network_interface" "gpu" {
  count               = var.enable_gpu ? 1 : 0
  name                = "baseline-gpu-nic"
  location            = azurerm_resource_group.baseline.location
  resource_group_name = azurerm_resource_group.baseline.name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.baseline.id
    private_ip_address_allocation = "Dynamic"
  }
}

resource "azurerm_linux_virtual_machine" "gpu" {
  count                = var.enable_gpu ? 1 : 0
  name                 = "baseline-gpu"
  location             = azurerm_resource_group.baseline.location
  resource_group_name = azurerm_resource_group.baseline.name
  size                 = local.gpu_vm_size
  admin_username       = "ubuntu"
  network_interface_ids = [azurerm_network_interface.gpu[0].id]

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = local.ubuntu_image
    version   = "latest"
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
  }

  admin_ssh_key {
    username   = "ubuntu"
    public_key = var.ssh_public_key != "" ? var.ssh_public_key : file("~/.ssh/id_rsa.pub")
  }
}

# --- Outputs ---

output "login_public_ip" {
  value = azurerm_public_ip.login.ip_address
}

output "compute_node_count" {
  value = var.compute_nodes
}

output "gpu_enabled" {
  value = var.enable_gpu
}

output "ubuntu_series" {
  value = var.ubuntu_series
}
