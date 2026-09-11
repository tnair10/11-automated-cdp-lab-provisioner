variable "project_name" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "subnet_cidr" {
  type = string
}

variable "admin_cidr" {
  type = string
}

variable "availability_zone" {
  description = "Optional availability zone for the subnet"
  type        = string
  default     = null
  nullable    = true
}
