# Pinned so a fresh `terraform init` on any machine resolves the same providers.
# The Railway provider is community-maintained (Railway ships no official one).
terraform {
  required_version = ">= 1.6.0"

  required_providers {
    railway = {
      source  = "terraform-community-providers/railway"
      version = "~> 0.6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
