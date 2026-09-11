resource "aws_vpc" "lab" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.project_name}-vpc"
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.lab.id
  cidr_block              = var.subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.project_name}-public"
  }
}

resource "aws_internet_gateway" "lab" {
  vpc_id = aws_vpc.lab.id

  tags = {
    Name = "${var.project_name}-igw"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.lab.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.lab.id
  }

  tags = {
    Name = "${var.project_name}-public-rt"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "cdp" {
  name        = "${var.project_name}-sg"
  description = "CDP lab security group"
  vpc_id      = aws_vpc.lab.id

  tags = {
    Name = "${var.project_name}-sg"
  }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.cdp.id

  cidr_ipv4   = var.admin_cidr
  from_port   = 22
  to_port     = 22
  ip_protocol = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "cm_ui" {
  security_group_id = aws_security_group.cdp.id

  cidr_ipv4   = var.admin_cidr
  from_port   = 7180
  to_port     = 7180
  ip_protocol = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "internal" {
  security_group_id = aws_security_group.cdp.id

  cidr_ipv4   = var.vpc_cidr
  ip_protocol = "-1"

  description = "Allow internal CDP traffic within the dedicated lab VPC"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.cdp.id

  cidr_ipv4   = "0.0.0.0/0"
  ip_protocol = "-1"
}
