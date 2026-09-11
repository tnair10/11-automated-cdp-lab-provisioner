#!/usr/bin/env python3
import ipaddress, json, pathlib, subprocess, sys

EXPECTED_CREATES = 18
EXPECTED_INSTANCES = 3
AMI = "ami-03b03811941a02056"
KEY = "p11-cdp-lab"
ALLOWED_TYPES = {"m6a.xlarge", "m6i.xlarge"}
PROHIBITED = {"aws_nat_gateway","aws_db_instance","aws_rds_cluster","aws_eks_cluster","aws_lb","aws_elb"}

def fail(msg):
    print("FAIL:", msg)
    raise SystemExit(1)

if len(sys.argv) != 2:
    fail("usage: validate_aws_plan.py <saved-plan.tfplan>")

root = pathlib.Path(__file__).resolve().parents[1]
tfdir = root / "terraform" / "environments" / "aws"
r = subprocess.run(
    ["terraform", f"-chdir={tfdir}", "show", "-json", sys.argv[1]],
    capture_output=True, text=True
)
if r.returncode:
    print(r.stderr, end="")
    fail("unable to read saved plan")

plan = json.loads(r.stdout)
managed = [
    x for x in plan.get("resource_changes", [])
    if x.get("mode", "managed") == "managed"
]
bad = [
    f"{x.get('address')}:{x.get('change',{}).get('actions')}"
    for x in managed
    if x.get("change",{}).get("actions") not in (["create"], ["no-op"])
]
if bad:
    fail("non-create changes: " + ", ".join(bad))

creates = [x for x in managed if x.get("change",{}).get("actions") == ["create"]]
if len(creates) != EXPECTED_CREATES:
    fail(f"expected {EXPECTED_CREATES} creates, found {len(creates)}")

bad_types = sorted({x.get("type") for x in creates if x.get("type") in PROHIBITED})
if bad_types:
    fail("prohibited resources: " + ", ".join(bad_types))

instances = [x for x in creates if x.get("type") == "aws_instance"]
if len(instances) != EXPECTED_INSTANCES:
    fail(f"expected {EXPECTED_INSTANCES} EC2 instances, found {len(instances)}")

for x in instances:
    a = x.get("address")
    v = x.get("change",{}).get("after",{})
    if v.get("ami") != AMI:
        fail(f"{a}: unapproved AMI")
    if v.get("instance_type") not in ALLOWED_TYPES:
        fail(f"{a}: unapproved instance type")
    if v.get("key_name") != KEY:
        fail(f"{a}: unapproved SSH key")
    if v.get("associate_public_ip_address") is not True:
        fail(f"{a}: public IP is not enabled")
    if v.get("instance_initiated_shutdown_behavior") != "terminate":
        fail(f"{a}: shutdown behavior is not terminate")
    roots = v.get("root_block_device") or []
    if len(roots) != 1:
        fail(f"{a}: expected one root block device")
    d = roots[0]
    size = d.get("volume_size")
    if not isinstance(size,(int,float)) or not 0 < size <= 60:
        fail(f"{a}: invalid root volume size {size}")
    if d.get("volume_type") != "gp3" or d.get("delete_on_termination") is not True:
        fail(f"{a}: root EBS safety settings invalid")
    ud = v.get("user_data") or ""
    if 'AUTO_TERMINATE_MINUTES="55"' not in ud or '--on-active="55m"' not in ud:
        fail(f"{a}: 55-minute auto-termination missing")

ingress = [x for x in creates if x.get("type") == "aws_vpc_security_group_ingress_rule"]
admin_ports, internal = [], 0
for x in ingress:
    a = x.get("address")
    v = x.get("change",{}).get("after",{})
    port, cidr, proto = v.get("from_port"), v.get("cidr_ipv4"), v.get("ip_protocol")
    if port in (22,7180):
        admin_ports.append(port)
        if v.get("to_port") != port or proto != "tcp":
            fail(f"{a}: invalid admin rule")
        try:
            n = ipaddress.ip_network(cidr, strict=False)
        except (TypeError, ValueError):
            fail(f"{a}: invalid admin CIDR")
        if n.version != 4 or n.prefixlen != 32 or not n.network_address.is_global:
            fail(f"{a}: admin CIDR must be global IPv4 /32")
    if proto == "-1" and cidr == "10.42.0.0/16":
        internal += 1

if sorted(admin_ports) != [22,7180]:
    fail("expected SSH /32 and Cloudera Manager /32 rules")
if internal != 1:
    fail("expected one internal 10.42.0.0/16 rule")

print("========================================")
print(" PROJECT 11 SAVED PLAN SAFETY CHECK")
print("========================================")
print(f"Planned creates:          {len(creates)}")
print(f"Planned EC2 instances:    {len(instances)}")
print(f"AMI:                      {AMI}")
print("Instance types:           " + ", ".join(sorted(ALLOWED_TYPES)))
print("Root volume ceiling:      60 GB")
print("Auto-termination:         55 minutes")
print("Admin ingress:            global IPv4 /32 only")
print("Prohibited-resource scan: PASS")
print("Saved plan safety check:  PASS")
