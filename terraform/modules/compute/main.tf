resource "aws_instance" "node" {
  for_each = var.nodes

  ami           = var.ami_id
  instance_type = each.value.instance_type

  subnet_id = var.subnet_id

  vpc_security_group_ids = [
    var.security_group_id
  ]

  iam_instance_profile = (
    var.instance_profile_name
  )

  associate_public_ip_address = true

  dynamic "root_block_device" {
    for_each = (
      var.configure_root_volume
      ? [1]
      : []
    )

    content {
      volume_type = "gp3"
      volume_size = each.value.root_volume_gb
    }
  }

  user_data = <<-EOF
    #!/bin/bash
    echo "${each.key}" > /etc/p11-node-name
  EOF

  tags = {
    Name = each.key
    Role = each.value.role
  }
}
