output "account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "is_localstack" {
  value = (
    data.aws_caller_identity.current.account_id
    == "000000000000"
  )
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

output "evidence_bucket" {
  value = aws_s3_bucket.evidence.bucket
}

output "instance_ids" {
  value = module.compute.instance_ids
}

output "instance_states" {
  value = module.compute.instance_states
}

output "estate_summary" {
  value = {
    backend      = "localstack"
    node_count   = length(var.nodes)
    management   = 1
    workers      = 2
    real_aws     = false
    aws_cost_usd = 0
  }
}
