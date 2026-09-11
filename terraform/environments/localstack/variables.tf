variable "project_name" {
  type    = string
  default = "p11-cdp-lab"
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "admin_cidr" {
  type    = string
  default = "10.0.0.0/8"

  validation {
    condition = (
      var.admin_cidr != "0.0.0.0/0" &&
      var.admin_cidr != "::/0"
    )

    error_message = "COST/SECURITY GUARD: admin access must not be open to the Internet."
  }
}

variable "ami_id" {
  description = "Mock LocalStack EC2 AMI"
  type        = string
  default     = "ami-ff0fea8310f3"
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

    error_message = "COST GUARD: maximum 3 lab nodes."
  }

  validation {
    condition = alltrue([
      for node in values(var.nodes) :
      contains(
        [
          "m6a.xlarge",
          "m6i.xlarge",
        ],
        node.instance_type
      )
    ])

    error_message = "COST GUARD: unapproved instance type."
  }

  validation {
    condition = alltrue([
      for node in values(var.nodes) :
      node.root_volume_gb <= 60
    ])

    error_message = "COST GUARD: root volume exceeds 60 GB."
  }
}
