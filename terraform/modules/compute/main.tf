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

  # Cost guard: guest shutdown must STOP the instance, never terminate it.
  instance_initiated_shutdown_behavior = "stop"

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
    set -eu

    echo "${each.key}" > /etc/p11-node-name
    echo "${var.auto_terminate_minutes}" > /etc/p11-auto-stop-minutes

    cat >/etc/systemd/system/p11-auto-stop.service <<'UNIT'
    [Unit]
    Description=Project 11 automatic EC2 stop

    [Service]
    Type=oneshot
    ExecStart=/usr/sbin/shutdown -h now
    UNIT

    cat >/etc/systemd/system/p11-auto-stop.timer <<'UNIT'
    [Unit]
    Description=Project 11 automatic EC2 stop timer

    [Timer]
    OnBootSec=${var.auto_terminate_minutes}min
    Unit=p11-auto-stop.service
    AccuracySec=30s
    Persistent=false

    [Install]
    WantedBy=timers.target
    UNIT

    systemctl daemon-reload
    systemctl enable --now p11-auto-stop.timer
  EOF

  tags = {
    Name = each.key
    Role = each.value.role
  }
}
