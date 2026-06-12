# terraform/staging/backend.tf
# Remote state stored in Azure Blob Storage

terraform {
  required_version = ">= 1.7.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.100"
    }
  }

  backend "azurerm" {
    # Values injected at init time via -backend-config flags in CI
    # resource_group_name  = var.TF_BACKEND_RG
    # storage_account_name = var.TF_BACKEND_SA
    # container_name       = var.TF_BACKEND_CONTAINER
    # key                  = "staging.terraform.tfstate"
  }
}

provider "azurerm" {
  features {}
  # ARM_CLIENT_ID, ARM_TENANT_ID, ARM_SUBSCRIPTION_ID, ARM_USE_OIDC
  # are injected via environment variables from GitHub Actions secrets
}
