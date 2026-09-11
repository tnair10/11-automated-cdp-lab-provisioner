resource "aws_instance" "node" {
  for_each = var.nodes

  ami           = var.ami_id
  instance_type = each.value.instance_type
  key_name      = var.key_name

  subnet_id = var.subnet_id

  vpc_security_group_ids = [
    var.security_group_id
  ]

  iam_instance_profile = (
    var.instance_profile_name
  )

  associate_public_ip_address = true

  instance_initiated_shutdown_behavior = (
    var.auto_terminate_minutes > 0
    ? "terminate"
    : "stop"
  )

  dynamic "root_block_device" {
    for_each = (
      var.configure_root_volume
      ? [1]
      : []
    )

    content {
      volume_type           = "gp3"
      volume_size           = each.value.root_volume_gb
      delete_on_termination = true
    }
  }

  user_data = <<-EOF
    #!/bin/bash
    echo "${each.key}" > /etc/p11-node-name

    AUTO_TERMINATE_MINUTES="${var.auto_terminate_minutes}"

    if [ "$AUTO_TERMINATE_MINUTES" -gt 0 ]; then
      echo "$AUTO_TERMINATE_MINUTES" > /etc/p11-auto-terminate-minutes

      /usr/bin/systemd-run         --unit=p11-auto-terminate         --on-active="${var.auto_terminate_minutes}m"         /usr/sbin/shutdown -h now
    fi
  EOF

  tags = {
    Name = each.key
    Role = each.value.role
  }
}
