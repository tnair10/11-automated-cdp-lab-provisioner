provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project           = var.project_name
      ManagedBy         = "terraform"
      Backend           = "aws"
      AutoDestroy       = "true"
      MaxRuntimeMinutes = tostring(var.max_runtime_minutes)
    }
  }
}

data "aws_caller_identity" "current" {}
