output "instance_ids" {
  value = {
    for name, instance in aws_instance.node :
    name => instance.id
  }
}

output "private_ips" {
  value = {
    for name, instance in aws_instance.node :
    name => instance.private_ip
  }
}

output "instance_states" {
  value = {
    for name, instance in aws_instance.node :
    name => instance.instance_state
  }
}
