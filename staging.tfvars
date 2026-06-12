# terraform/staging/staging.tfvars
# Example variable values for the Staging environment.
# Do NOT commit sensitive values — use GitHub Secrets or Azure Key Vault.

environment         = "staging"
location            = "eastus2"
resource_group_name = "rg-myapp-staging"

# App Service
app_service_sku     = "B2"
app_service_count   = 1

# Networking
vnet_address_space  = "10.10.0.0/16"
subnet_app_prefix   = "10.10.1.0/24"
subnet_data_prefix  = "10.10.2.0/24"

# Tags
tags = {
  environment = "staging"
  managed_by  = "terraform"
  team        = "platform-engineering"
}
