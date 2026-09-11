output "account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "backend" {
  value = "aws"
}

output "region" {
  value = var.region
}

output "planned_node_count" {
  value = length(var.nodes)
}

output "instance_ids" {
  value = module.compute.instance_ids
}

output "instance_states" {
  value = module.compute.instance_states
}

output "evidence_bucket" {
  value = aws_s3_bucket.evidence.bucket
}

output "vpc_id" {
  value = module.network.vpc_id
}

output "subnet_id" {
  value = module.network.subnet_id
}

output "security_group_id" {
  value = module.network.security_group_id
}

output "safety_summary" {
  value = {
    backend             = "aws"
    node_count          = length(var.nodes)
    max_runtime_minutes = var.max_runtime_minutes
    auto_destroy        = true
    plan_only_stage     = true
  }
}
