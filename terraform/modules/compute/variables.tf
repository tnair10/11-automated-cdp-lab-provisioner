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
