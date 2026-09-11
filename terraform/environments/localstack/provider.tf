variable "localstack_endpoint" {
  type    = string
  default = "http://p11-localstack:4566"
}

provider "aws" {
  region = var.aws_region

  access_key = "test"
  secret_key = "test"

  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
  skip_requesting_account_id  = true

  s3_use_path_style = true

  endpoints {
    ec2       = var.localstack_endpoint
    iam       = var.localstack_endpoint
    s3        = var.localstack_endpoint
    s3control = var.localstack_endpoint
    sts       = var.localstack_endpoint
  }

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
      Backend   = "localstack"
    }
  }
}

data "aws_caller_identity" "current" {}
