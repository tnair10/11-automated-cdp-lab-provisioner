module "network" {
  source = "../../modules/network"

  project_name = var.project_name

  vpc_cidr    = "10.42.0.0/16"
  subnet_cidr = "10.42.10.0/24"

  admin_cidr = var.admin_cidr
}


resource "aws_s3_bucket" "evidence" {
  bucket = "${var.project_name}-evidence-local"

  tags = {
    Purpose = "provisioning-evidence"
  }
}


resource "aws_s3_bucket_versioning" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  versioning_configuration {
    status = "Enabled"
  }
}


resource "aws_iam_role" "cdp_node" {
  name = "${var.project_name}-node-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Effect = "Allow"

        Principal = {
          Service = "ec2.amazonaws.com"
        }

        Action = "sts:AssumeRole"
      }
    ]
  })
}


resource "aws_iam_role_policy" "evidence" {
  name = "${var.project_name}-evidence-policy"

  role = aws_iam_role.cdp_node.id

  policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Effect = "Allow"

        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket",
        ]

        Resource = [
          aws_s3_bucket.evidence.arn,
          "${aws_s3_bucket.evidence.arn}/*",
        ]
      }
    ]
  })
}


resource "aws_iam_instance_profile" "cdp_node" {
  name = "${var.project_name}-node-profile"
  role = aws_iam_role.cdp_node.name
}


module "compute" {
  source = "../../modules/compute"

  project_name = var.project_name

  ami_id = var.ami_id

  subnet_id = module.network.subnet_id

  security_group_id = (
    module.network.security_group_id
  )

  instance_profile_name = (
    aws_iam_instance_profile.cdp_node.name
  )

  nodes = var.nodes

  # LocalStack Stage 2 is API/CRUD simulation only.
  # Real AWS keeps root-volume configuration enabled.
  configure_root_volume = false
}
