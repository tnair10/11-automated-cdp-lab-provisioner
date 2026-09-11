module "network" {
  source = "../../modules/network"

  project_name = var.project_name
  vpc_cidr     = var.vpc_cidr
  subnet_cidr  = var.subnet_cidr
  admin_cidr   = var.admin_cidr
}

resource "aws_s3_bucket" "evidence" {
  bucket = "${var.project_name}-evidence-${data.aws_caller_identity.current.account_id}-${var.region}"
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
          "s3:ListBucket"
        ]

        Resource = [
          aws_s3_bucket.evidence.arn,
          "${aws_s3_bucket.evidence.arn}/*"
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

  project_name          = var.project_name
  ami_id                = var.ami_id
  key_name              = var.key_name
  subnet_id             = module.network.subnet_id
  security_group_id     = module.network.security_group_id
  instance_profile_name = aws_iam_instance_profile.cdp_node.name
  nodes                 = var.nodes

  configure_root_volume  = true
  auto_terminate_minutes = max(1, var.max_runtime_minutes - 5)
}
