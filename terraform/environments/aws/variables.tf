variable "project_name" {
  type    = string
  default = "p11-cdp-lab"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "subnet_cidr" {
  type    = string
  default = "10.42.1.0/24"
}

variable "admin_cidr" {
  description = "CIDR permitted to access SSH and Cloudera Manager"
  type        = string
  default     = "203.0.113.10/32"

  validation {
    condition = (
      var.admin_cidr != "0.0.0.0/0" &&
      var.admin_cidr != "::/0"
    )

    error_message = "admin_cidr must not allow access from the entire Internet."
  }
}

variable "ami_id" {
  description = "x86_64 Linux AMI used for real CDP hosts"
  type        = string

  # Deliberately unusable placeholder during Stage 5 plan-only development.
  default = "ami-00000000000000000"

  validation {
    condition = can(
      regex(
        "^ami-[0-9a-f]{17}$",
        var.ami_id
      )
    )

    error_message = "ami_id must use the standard 17-character AWS AMI ID format."
  }
}

variable "key_name" {
  description = "Existing AWS EC2 key-pair name"
  type        = string

  # Deliberately unusable until real-host provisioning is approved.
  default = "REPLACE_ME"
}

variable "max_runtime_minutes" {
  type    = number
  default = 60

  validation {
    condition = (
      var.max_runtime_minutes > 0 &&
      var.max_runtime_minutes <= 60
    )

    error_message = "Maximum runtime cannot exceed 60 minutes."
  }
}

variable "nodes" {
  type = map(object({
    role           = string
    instance_type  = string
    root_volume_gb = number
  }))

  default = {
    cm-master-01 = {
      role           = "management"
      instance_type  = "m6a.xlarge"
      root_volume_gb = 40
    }

    worker-01 = {
      role           = "worker"
      instance_type  = "m6a.xlarge"
      root_volume_gb = 40
    }

    worker-02 = {
      role           = "worker"
      instance_type  = "m6a.xlarge"
      root_volume_gb = 40
    }
  }

  validation {
    condition = length(var.nodes) <= 3

    error_message = "Real AWS lab is limited to 3 instances."
  }

  validation {
    condition = alltrue([
      for node in values(var.nodes) :
      contains(
        [
          "m6a.xlarge",
          "m6i.xlarge"
        ],
        node.instance_type
      )
    ])

    error_message = "Only approved instance types may be used."
  }

  validation {
    condition = alltrue([
      for node in values(var.nodes) :
      node.root_volume_gb <= 60
    ])

    error_message = "Root volumes cannot exceed 60 GB."
  }
}

variable "availability_zone" {
  description = "Availability zone used for the Project 11 subnet"
  type        = string
  default     = "us-east-1a"

  validation {
    condition = contains(
      ["us-east-1a", "us-east-1b", "us-east-1c", "us-east-1d", "us-east-1f"],
      var.availability_zone
    )
    error_message = "availability_zone must be an AZ where m6a.xlarge was verified as available."
  }
}
