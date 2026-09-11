variable "project_name" {
  type = string
}

variable "ami_id" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "security_group_id" {
  type = string
}

variable "instance_profile_name" {
  type = string
}

variable "configure_root_volume" {
  description = "Configure the EC2 root EBS volume"
  type        = bool
  default     = true
}

variable "nodes" {
  type = map(object({
    role           = string
    instance_type  = string
    root_volume_gb = number
  }))
}

variable "key_name" {
  description = "Existing EC2 SSH key pair name"
  type        = string
  default     = null
  nullable    = true
}

variable "auto_terminate_minutes" {
  description = "Minutes after boot before the instance shuts down; 0 disables automatic termination"
  type        = number
  default     = 0

  validation {
    condition = var.auto_terminate_minutes == 0 || (
      var.auto_terminate_minutes >= 1 &&
      var.auto_terminate_minutes <= 60
    )
    error_message = "auto_terminate_minutes must be 0 (disabled) or between 1 and 60."
  }
}
