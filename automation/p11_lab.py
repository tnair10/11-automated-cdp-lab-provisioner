#!/usr/bin/env python3
"""Project 11: consolidated distributed Hadoop lab orchestrator.

Public/open-source stack:
  - Apache Hadoop 3.4.1 (HDFS + YARN + MapReduce)
  - Apache Hive 4.1.0 (PostgreSQL metastore, HiveServer2, MR execution)
  - Apache Spark 3.5.9 on YARN

The script assumes the existing Project 11 Terraform stack has already created
three EC2 instances tagged Project=p11-cdp-lab with Name tags:
  cm-master-01, worker-01, worker-02

For the public Hadoop implementation, cm-master-01 is treated logically as
master-01. The Terraform/AWS tag can be renamed during final cleanup without
blocking the functional lab.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "results" / "p11-live"

AWS_PROFILE = os.environ.get("P11_AWS_PROFILE", "p11-lab")
AWS_REGION = os.environ.get("P11_AWS_REGION", "us-east-1")
PROJECT_TAG = os.environ.get("P11_PROJECT_TAG", "p11-cdp-lab")
SSH_USER = os.environ.get("P11_SSH_USER", "ec2-user")
SSH_KEY = str(Path(os.environ.get("P11_SSH_KEY", "~/.ssh/p11-cdp-lab")).expanduser())
DOMAIN = os.environ.get("P11_LAB_DOMAIN", "lab.example.com")

AWS_TO_LOGICAL = {
    "cm-master-01": "master-01",
    "worker-01": "worker-01",
    "worker-02": "worker-02",
}
LOGICAL_ORDER = ("master-01", "worker-01", "worker-02")

HADOOP_VERSION = "3.4.1"
HIVE_VERSION = "4.1.0"
SPARK_VERSION = "3.5.9"

HADOOP_ARCHIVE = f"hadoop-{HADOOP_VERSION}-lean.tar.gz"
HIVE_ARCHIVE = f"apache-hive-{HIVE_VERSION}-bin.tar.gz"
SPARK_ARCHIVE = f"spark-{SPARK_VERSION}-bin-hadoop3.tgz"

HADOOP_URL = (
    f"https://archive.apache.org/dist/hadoop/common/hadoop-{HADOOP_VERSION}/"
    f"{HADOOP_ARCHIVE}"
)
HADOOP_SUM = HADOOP_URL + ".sha512"
HIVE_URL = f"https://archive.apache.org/dist/hive/hive-{HIVE_VERSION}/{HIVE_ARCHIVE}"
HIVE_SUM = HIVE_URL + ".sha256"
SPARK_URL = f"https://archive.apache.org/dist/spark/spark-{SPARK_VERSION}/{SPARK_ARCHIVE}"
SPARK_SUM = SPARK_URL + ".sha512"

OPT_ROOT = "/opt/p11"
HADOOP_HOME = f"{OPT_ROOT}/hadoop"
HIVE_HOME = f"{OPT_ROOT}/hive"
SPARK_HOME = f"{OPT_ROOT}/spark"


class P11Error(RuntimeError):
    pass


def run(cmd, *, check=True, capture=False, input_text=None, env=None):
    cmd = [str(x) for x in cmd]
    print("+", " ".join(shlex.quote(x) for x in cmd))
    cp = subprocess.run(
        cmd,
        check=False,
        text=True,
        input=input_text,
        capture_output=capture,
        env=env,
    )
    if check and cp.returncode != 0:
        if capture:
            if cp.stdout:
                print(cp.stdout, file=sys.stderr)
            if cp.stderr:
                print(cp.stderr, file=sys.stderr)
        raise P11Error(f"command failed rc={cp.returncode}: {cmd[0]}")
    return cp


def aws_json(args):
    cp = run(
        [
            "aws",
            *args,
            "--profile",
            AWS_PROFILE,
            "--region",
            AWS_REGION,
            "--output",
            "json",
        ],
        capture=True,
    )
    return json.loads(cp.stdout)


def discover_nodes() -> List[Dict[str, str]]:
    data = aws_json(
        [
            "ec2",
            "describe-instances",
            "--filters",
            f"Name=tag:Project,Values={PROJECT_TAG}",
            "Name=instance-state-name,Values=pending,running",
        ]
    )
    by_logical = {}
    for reservation in data.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
            aws_name = tags.get("Name")
            logical = AWS_TO_LOGICAL.get(aws_name)
            if not logical:
                continue
            by_logical[logical] = {
                "aws_name": aws_name,
                "name": logical,
                "fqdn": f"{logical}.{DOMAIN}",
                "instance_id": inst["InstanceId"],
                "public_ip": inst.get("PublicIpAddress"),
                "private_ip": inst.get("PrivateIpAddress"),
                "instance_type": inst.get("InstanceType"),
                "az": inst.get("Placement", {}).get("AvailabilityZone"),
                "state": inst.get("State", {}).get("Name"),
            }

    missing = [n for n in LOGICAL_ORDER if n not in by_logical]
    if missing:
        raise P11Error("missing running Project 11 nodes: " + ", ".join(missing))

    nodes = [by_logical[n] for n in LOGICAL_ORDER]
    for n in nodes:
        if not n["public_ip"] or not n["private_ip"]:
            raise P11Error(f"{n['name']}: public/private IP missing")
    return nodes


def node_map(nodes):
    return {n["name"]: n for n in nodes}


def ssh_base(node):
    return [
        "ssh",
        "-n",
        "-i",
        SSH_KEY,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        f"{SSH_USER}@{node['public_ip']}",
    ]


def remote(node, command, *, check=True, capture=False):
    return run(ssh_base(node) + [command], check=check, capture=capture)


def remote_root(node, script, *, check=True, capture=False):
    cmd = ssh_base(node)
    cmd.remove("-n")
    return run(
        cmd + ["sudo", "bash", "-s"],
        check=check,
        capture=capture,
        input_text=script,
    )


def wait_ssh(node, timeout=420):
    deadline = time.time() + timeout
    while time.time() < deadline:
        cp = remote(node, "true", check=False, capture=True)
        if cp.returncode == 0:
            return
        time.sleep(5)
    raise P11Error(f"{node['name']}: SSH not ready after {timeout}s")


def parallel(nodes, fn, title):
    print(f"\n===== {title} =====")
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as ex:
        futs = {ex.submit(fn, n): n for n in nodes}
        for fut in concurrent.futures.as_completed(futs):
            n = futs[fut]
            try:
                fut.result()
                print(f"[{n['name']}] PASS")
            except Exception as e:
                errors.append((n["name"], str(e)))
                print(f"[{n['name']}] FAIL: {e}", file=sys.stderr)
    if errors:
        raise P11Error(f"{title} failed: {errors}")


def write_json(name, data):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / name
    path.write_text(json.dumps(data, indent=2) + "\n")
    print("evidence:", path)
    return path


def inventory():
    nodes = discover_nodes()
    write_json("inventory.json", {"nodes": nodes})
    print(json.dumps({"nodes": nodes}, indent=2))
    return nodes


def hosts_block(nodes):
    lines = ["# BEGIN P11 MANAGED HOSTS"]
    for n in nodes:
        lines.append(f"{n['private_ip']} {n['fqdn']} {n['name']}")
    lines.append("# END P11 MANAGED HOSTS")
    return "\n".join(lines)


def prepare_node_script(node, nodes):
    block = hosts_block(nodes)
    return f'''set -euo pipefail

# Base packages only; no Cloudera/CDP material.
dnf install -y \
  java-17-openjdk-devel chrony curl wget tar gzip xz unzip rsync \
  procps-ng which bind-utils jq python3

systemctl enable --now chronyd
systemctl disable --now firewalld 2>/dev/null || true

hostnamectl set-hostname {shlex.quote(node['fqdn'])}

python3 - <<'PYHOSTS'
from pathlib import Path
p=Path('/etc/hosts')
s=p.read_text()
start='# BEGIN P11 MANAGED HOSTS'
end='# END P11 MANAGED HOSTS'
block={block!r}
if start in s and end in s:
    left=s.split(start,1)[0].rstrip()
    right=s.split(end,1)[1].lstrip()
    s=left+'\\n'+block+'\\n'+right
else:
    s=s.rstrip()+'\\n\\n'+block+'\\n'
p.write_text(s)
PYHOSTS

cat >/etc/sysctl.d/99-p11-hadoop.conf <<'EOF'
vm.swappiness = 1
net.core.somaxconn = 16000
EOF
sysctl --system >/dev/null

cat >/etc/systemd/system/p11-disable-thp.service <<'EOF'
[Unit]
Description=Disable Transparent Huge Pages for Project 11
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'test ! -e /sys/kernel/mm/transparent_hugepage/enabled || echo never > /sys/kernel/mm/transparent_hugepage/enabled'
ExecStart=/bin/sh -c 'test ! -e /sys/kernel/mm/transparent_hugepage/defrag || echo never > /sys/kernel/mm/transparent_hugepage/defrag'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now p11-disable-thp.service

getent group hadoop >/dev/null || groupadd --system hadoop
id hadoop >/dev/null 2>&1 || useradd --system --create-home --gid hadoop --shell /bin/bash hadoop

mkdir -p {OPT_ROOT} /data/hadoop/tmp /var/log/p11-hadoop
chown -R hadoop:hadoop {OPT_ROOT} /data/hadoop /var/log/p11-hadoop
chmod 0755 {OPT_ROOT}

JAVA_BIN="$(rpm -ql java-17-openjdk-headless | grep -E '/bin/java$' | head -1)"
JAVA_HOME="$(dirname "$(dirname "$JAVA_BIN")")"
cat >/etc/profile.d/p11-platform.sh <<EOF
export JAVA_HOME=$JAVA_HOME
export HADOOP_HOME={HADOOP_HOME}
export HADOOP_CONF_DIR={HADOOP_HOME}/etc/hadoop
export HIVE_HOME={HIVE_HOME}
export SPARK_HOME={SPARK_HOME}
export PATH=\\$PATH:{HADOOP_HOME}/bin:{HADOOP_HOME}/sbin:{HIVE_HOME}/bin:{SPARK_HOME}/bin
EOF
chmod 0644 /etc/profile.d/p11-platform.sh

echo P11_PREP_OK
'''


def prepare():
    nodes = inventory()
    parallel(nodes, wait_ssh, "SSH READY")
    parallel(nodes, lambda n: remote_root(n, prepare_node_script(n, nodes)), "HOST PREP")

    evidence = {}
    for n in nodes:
        cp = remote(
            n,
            "printf 'hostname='; hostname -f; "
            "printf 'java='; java -version 2>&1 | head -1; "
            "printf 'chronyd='; systemctl is-active chronyd; "
            "printf 'swappiness='; sysctl -n vm.swappiness; "
            "printf 'somaxconn='; sysctl -n net.core.somaxconn; "
            "printf 'selinux='; getenforce 2>/dev/null || true",
            capture=True,
        )
        evidence[n["name"]] = cp.stdout
        print(f"\n--- {n['name']} ---\n{cp.stdout}", end="")
    write_json("host-prep.json", evidence)
    return nodes


def download_function_shell():
    return r"""
download_verify_sha512() {
  local url="$1" sumurl="$2" dest="$3"
  local expected actual

  curl -fsSL --retry 4 --retry-delay 3 "$sumurl" -o "$dest.sha512"
  expected="$(grep -Eo '[0-9A-Fa-f]{128}' "$dest.sha512" | head -1)"
  test -n "$expected"

  if [ -s "$dest" ]; then
    actual="$(sha512sum "$dest" | awk '{print $1}')"
    if [ "$actual" = "$expected" ]; then
      rm -f "$dest.tmp"
      return 0
    fi
    rm -f "$dest"
  fi

  if [ -s "$dest.tmp" ]; then
    actual="$(sha512sum "$dest.tmp" | awk '{print $1}')"
    if [ "$actual" = "$expected" ]; then
      mv "$dest.tmp" "$dest"
      return 0
    fi
    rm -f "$dest.tmp"
  fi

  curl -fL --retry 4 --retry-delay 3 -o "$dest.tmp" "$url"
  actual="$(sha512sum "$dest.tmp" | awk '{print $1}')"
  test "$actual" = "$expected"
  mv "$dest.tmp" "$dest"
}

download_verify_sha256() {
  local url="$1" sumurl="$2" dest="$3"
  local expected actual

  curl -fsSL --retry 4 --retry-delay 3 "$sumurl" -o "$dest.sha256"
  expected="$(grep -Eo '[0-9A-Fa-f]{64}' "$dest.sha256" | head -1)"
  test -n "$expected"

  if [ -s "$dest" ]; then
    actual="$(sha256sum "$dest" | awk '{print $1}')"
    if [ "$actual" = "$expected" ]; then
      rm -f "$dest.tmp"
      return 0
    fi
    rm -f "$dest"
  fi

  if [ -s "$dest.tmp" ]; then
    actual="$(sha256sum "$dest.tmp" | awk '{print $1}')"
    if [ "$actual" = "$expected" ]; then
      mv "$dest.tmp" "$dest"
      return 0
    fi
    rm -f "$dest.tmp"
  fi

  curl -fL --retry 4 --retry-delay 3 -o "$dest.tmp" "$url"
  actual="$(sha256sum "$dest.tmp" | awk '{print $1}')"
  test "$actual" = "$expected"
  mv "$dest.tmp" "$dest"
}
"""

def install_hadoop_script():
    funcs = download_function_shell()
    return f'''set -euo pipefail
{funcs}
mkdir -p {OPT_ROOT}/downloads
chown -R hadoop:hadoop {OPT_ROOT}

download_verify_sha512 \
  {shlex.quote(HADOOP_URL)} \
  {shlex.quote(HADOOP_SUM)} \
  {OPT_ROOT}/downloads/{HADOOP_ARCHIVE}

if [ ! -d {OPT_ROOT}/hadoop-{HADOOP_VERSION} ]; then
  tar -xzf {OPT_ROOT}/downloads/{HADOOP_ARCHIVE} -C {OPT_ROOT}
fi
ln -sfn {OPT_ROOT}/hadoop-{HADOOP_VERSION} {HADOOP_HOME}
chown -R hadoop:hadoop {OPT_ROOT}/hadoop-{HADOOP_VERSION}
echo P11_HADOOP_INSTALLED
'''


def install_master_extras_script():
    funcs = download_function_shell()
    return f'''set -euo pipefail
{funcs}
mkdir -p {OPT_ROOT}/downloads

dnf install -y postgresql-server postgresql postgresql-jdbc openssl

download_verify_sha256 \
  {shlex.quote(HIVE_URL)} \
  {shlex.quote(HIVE_SUM)} \
  {OPT_ROOT}/downloads/{HIVE_ARCHIVE}

if [ ! -d {OPT_ROOT}/apache-hive-{HIVE_VERSION}-bin ]; then
  tar -xzf {OPT_ROOT}/downloads/{HIVE_ARCHIVE} -C {OPT_ROOT}
fi
ln -sfn {OPT_ROOT}/apache-hive-{HIVE_VERSION}-bin {HIVE_HOME}

download_verify_sha512 \
  {shlex.quote(SPARK_URL)} \
  {shlex.quote(SPARK_SUM)} \
  {OPT_ROOT}/downloads/{SPARK_ARCHIVE}

if [ ! -d {OPT_ROOT}/spark-{SPARK_VERSION}-bin-hadoop3 ]; then
  tar -xzf {OPT_ROOT}/downloads/{SPARK_ARCHIVE} -C {OPT_ROOT}
fi
ln -sfn {OPT_ROOT}/spark-{SPARK_VERSION}-bin-hadoop3 {SPARK_HOME}

chown -R hadoop:hadoop \
  {OPT_ROOT}/apache-hive-{HIVE_VERSION}-bin \
  {OPT_ROOT}/spark-{SPARK_VERSION}-bin-hadoop3

echo P11_MASTER_EXTRAS_INSTALLED
'''


def install():
    nodes = discover_nodes()
    parallel(nodes, lambda n: remote_root(n, install_hadoop_script()), "INSTALL HADOOP")
    master = node_map(nodes)["master-01"]
    remote_root(master, install_master_extras_script())
    return nodes



def secure_identity_script():
    return r"""set -euo pipefail

P11_USER=p11admin
P11_GROUP=p11admin
P11_UID=1100
P11_GID=1100
P11_HOME=/home/p11admin

if getent group "$P11_GROUP" >/dev/null; then
  existing_gid="$(getent group "$P11_GROUP" | cut -d: -f3)"
  test "$existing_gid" = "$P11_GID"
else
  groupadd --gid "$P11_GID" "$P11_GROUP"
fi

if getent passwd "$P11_USER" >/dev/null; then
  existing_uid="$(id -u "$P11_USER")"
  existing_gid="$(id -g "$P11_USER")"
  existing_home="$(getent passwd "$P11_USER" | cut -d: -f6)"
  existing_shell="$(getent passwd "$P11_USER" | cut -d: -f7)"

  test "$existing_uid" = "$P11_UID"
  test "$existing_gid" = "$P11_GID"
  test "$existing_home" = "$P11_HOME"
  test "$existing_shell" = "/bin/bash"
else
  useradd \
    --uid "$P11_UID" \
    --gid "$P11_GROUP" \
    --create-home \
    --home-dir "$P11_HOME" \
    --shell /bin/bash \
    "$P11_USER"
fi

install \
  -d \
  -o "$P11_USER" \
  -g "$P11_GROUP" \
  -m 0750 \
  "$P11_HOME"

if id -nG "$P11_USER" | tr " " "\n" | grep -qx hadoop; then
  echo "ERROR: p11admin must not be a member of hadoop group" >&2
  exit 1
fi

echo P11_SECURE_IDENTITY_CONFIGURED
"""


def validate_secure_identity(nodes=None):
    if nodes is None:
        nodes = discover_nodes()

    evidence = {
        "expected": {
            "user": "p11admin",
            "uid": 1100,
            "group": "p11admin",
            "gid": 1100,
            "home": "/home/p11admin",
            "shell": "/bin/bash",
            "hadoop_group_member": False,
        },
        "nodes": {},
        "overall": "PASS",
    }

    expected = "1100:1100:/home/p11admin:/bin/bash:1100"

    for node in nodes:
        identity = remote(
            node,
            """uid=$(id -u p11admin) && \
gid=$(id -g p11admin) && \
home=$(getent passwd p11admin | cut -d: -f6) && \
shell=$(getent passwd p11admin | cut -d: -f7) && \
group_gid=$(getent group p11admin | cut -d: -f3) && \
printf '%s:%s:%s:%s:%s\\n' "$uid" "$gid" "$home" "$shell" "$group_gid" """,
            capture=True,
        ).stdout.strip()

        if identity != expected:
            raise P11Error(
                f'{node["name"]}: unexpected p11admin identity: {identity!r}'
            )

        groups = remote(
            node,
            "id -nG p11admin",
            capture=True,
        ).stdout.strip().split()

        if "hadoop" in groups:
            raise P11Error(
                f'{node["name"]}: p11admin must not belong to hadoop group'
            )

        evidence["nodes"][node["name"]] = {
            "identity": identity,
            "groups": groups,
            "status": "PASS",
        }

    secure_dir = RESULTS_DIR / "secure"
    secure_dir.mkdir(parents=True, exist_ok=True)

    evidence_path = secure_dir / "identity.json"
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n")

    print(f"evidence: {evidence_path}")
    print(json.dumps(evidence, indent=2))

    return evidence


def secure_identity():
    nodes = discover_nodes()

    parallel(
        nodes,
        lambda n: remote_root(n, secure_identity_script()),
        "SECURE IDENTITY",
    )

    validate_secure_identity(nodes)

    print("P11 SECURE IDENTITY: PASS")

    return nodes



def secure_lce_worker_script():
    return f"""set -euo pipefail

test "$(id -u p11admin)" = "1100"
test "$(id -g p11admin)" = "1100"

# Secure every parent relevant to container-executor.
chown root:hadoop \
  {OPT_ROOT} \
  {OPT_ROOT}/hadoop-{HADOOP_VERSION} \
  {OPT_ROOT}/hadoop-{HADOOP_VERSION}/bin \
  {OPT_ROOT}/hadoop-{HADOOP_VERSION}/etc \
  {OPT_ROOT}/hadoop-{HADOOP_VERSION}/etc/hadoop

chmod 0755 \
  {OPT_ROOT} \
  {OPT_ROOT}/hadoop-{HADOOP_VERSION} \
  {OPT_ROOT}/hadoop-{HADOOP_VERSION}/bin \
  {OPT_ROOT}/hadoop-{HADOOP_VERSION}/etc \
  {OPT_ROOT}/hadoop-{HADOOP_VERSION}/etc/hadoop

chown -h root:hadoop {HADOOP_HOME}

# Harden the privileged native executor.
chown root:hadoop {HADOOP_HOME}/bin/container-executor
chmod 6050 {HADOOP_HOME}/bin/container-executor

cat >{HADOOP_HOME}/etc/hadoop/container-executor.cfg <<'EOF'
yarn.nodemanager.linux-container-executor.group=hadoop
banned.users=hdfs,yarn,mapred,bin,hadoop
min.user.id=1000
allowed.system.users=
EOF

chown root:hadoop \
  {HADOOP_HOME}/etc/hadoop/container-executor.cfg

chmod 0400 \
  {HADOOP_HOME}/etc/hadoop/container-executor.cfg

# Add LinuxContainerExecutor properties without duplicating them.
python3 - <<'P11PY'
from pathlib import Path
import xml.etree.ElementTree as ET

path = Path("{HADOOP_HOME}/etc/hadoop/yarn-site.xml")
tree = ET.parse(path)
root = tree.getroot()

wanted = {{
    "yarn.nodemanager.container-executor.class":
        "org.apache.hadoop.yarn.server.nodemanager.LinuxContainerExecutor",
    "yarn.nodemanager.linux-container-executor.group":
        "hadoop",
    "yarn.nodemanager.linux-container-executor.nonsecure-mode.limit-users":
        "false",
}}

existing = {{}}

for prop in root.findall("property"):
    name = prop.find("name")
    value = prop.find("value")
    if name is not None and value is not None:
        existing[name.text] = prop

for name, value in wanted.items():
    if name in existing:
        existing[name].find("value").text = value
    else:
        prop = ET.SubElement(root, "property")
        n = ET.SubElement(prop, "name")
        n.text = name
        v = ET.SubElement(prop, "value")
        v.text = value

try:
    ET.indent(tree, space="  ")
except AttributeError:
    pass

tree.write(
    path,
    encoding="unicode",
    xml_declaration=True,
)
P11PY

chown hadoop:hadoop \
  {HADOOP_HOME}/etc/hadoop/yarn-site.xml

chmod 0644 \
  {HADOOP_HOME}/etc/hadoop/yarn-site.xml

# The setuid executor itself must remain root-owned.
chown root:hadoop {HADOOP_HOME}/bin/container-executor
chmod 6050 {HADOOP_HOME}/bin/container-executor

chown root:hadoop \
  {HADOOP_HOME}/etc/hadoop/container-executor.cfg

chmod 0400 \
  {HADOOP_HOME}/etc/hadoop/container-executor.cfg

# Fail here if Hadoop rejects ownership/mode/configuration.
sudo -u hadoop \
  {HADOOP_HOME}/bin/container-executor \
  --checksetup

systemctl restart p11-nodemanager

for i in $(seq 1 30); do
  if systemctl is-active --quiet p11-nodemanager; then
    break
  fi
  sleep 2
done

systemctl is-active --quiet p11-nodemanager

echo P11_LCE_CONFIGURED
"""


def validate_secure_lce(nodes=None):
    if nodes is None:
        nodes = discover_nodes()

    by = node_map(nodes)
    master = by["master-01"]
    workers = [by["worker-01"], by["worker-02"]]

    evidence = {
        "workers": {},
        "yarn_nodes": None,
        "mapreduce_as_p11admin": None,
        "overall": "PASS",
    }

    for node in workers:
        binary = remote(
            node,
            f"""stat -Lc '%U:%G:%a' \
{HADOOP_HOME}/bin/container-executor""",
            capture=True,
        ).stdout.strip()

        cfg = remote(
            node,
            f"""stat -Lc '%U:%G:%a' \
{HADOOP_HOME}/etc/hadoop/container-executor.cfg""",
            capture=True,
        ).stdout.strip()

        parents = remote(
            node,
            f"""stat -Lc '%U:%G:%a:%n' \
{OPT_ROOT} \
{OPT_ROOT}/hadoop-{HADOOP_VERSION} \
{OPT_ROOT}/hadoop-{HADOOP_VERSION}/bin \
{OPT_ROOT}/hadoop-{HADOOP_VERSION}/etc \
{OPT_ROOT}/hadoop-{HADOOP_VERSION}/etc/hadoop""",
            capture=True,
        ).stdout.strip()

        checksetup = remote(
            node,
            f"""sudo -u hadoop \
{HADOOP_HOME}/bin/container-executor \
--checksetup && echo CHECKSETUP_PASS""",
            capture=True,
        ).stdout.strip()

        yarn_cfg = remote(
            node,
            f"""grep -A2 -B1 \
'yarn.nodemanager.container-executor.class' \
{HADOOP_HOME}/etc/hadoop/yarn-site.xml; \
grep -A2 -B1 \
'yarn.nodemanager.linux-container-executor.group' \
{HADOOP_HOME}/etc/hadoop/yarn-site.xml""",
            capture=True,
        ).stdout.strip()

        nm_state = remote(
            node,
            "systemctl is-active p11-nodemanager",
            capture=True,
        ).stdout.strip()

        if binary != "root:hadoop:6050":
            raise P11Error(
                f'{node["name"]}: unexpected container-executor mode: {binary}'
            )

        if cfg != "root:hadoop:400":
            raise P11Error(
                f'{node["name"]}: unexpected container-executor.cfg mode: {cfg}'
            )

        if "CHECKSETUP_PASS" not in checksetup:
            raise P11Error(
                f'{node["name"]}: container-executor --checksetup failed'
            )

        if nm_state != "active":
            raise P11Error(
                f'{node["name"]}: NodeManager is not active'
            )

        evidence["workers"][node["name"]] = {
            "container_executor": binary,
            "container_executor_cfg": cfg,
            "parents": parents,
            "checksetup": "PASS",
            "nodemanager": "active",
            "yarn_configuration": yarn_cfg,
        }

    # Wait until both restarted NodeManagers have re-registered.
    yarn_nodes = remote(
        master,
        f"""sudo -u hadoop bash -lc '
. /etc/p11-platform.env
# The same LCE validator runs before and after the Kerberos
# cutover. Authenticate only when Hadoop security is enabled.
if grep -Eq \
    "<value>[[:space:]]*kerberos[[:space:]]*</value>" \
    /opt/p11/hadoop/etc/hadoop/core-site.xml
then
    export KRB5CCNAME="/tmp/p11-lce-yarn-wait-$$.ccache"

    cleanup_p11_lce_ticket() {{
        kdestroy 2>/dev/null || true
        rm -f "$KRB5CCNAME"
    }}

    trap cleanup_p11_lce_ticket EXIT

    kdestroy 2>/dev/null || true

    kinit \
        -kt /etc/security/keytabs/yarn.service.keytab \
        "yarn/$(hostname -f)@LAB.EXAMPLE.COM"
fi

for i in $(seq 1 30); do
  OUT="$(timeout 15 {HADOOP_HOME}/bin/yarn node -list 2>&1 || true)"
  COUNT="$(printf "%s\\n" "$OUT" | grep -c RUNNING || true)"
  if [ "$COUNT" -ge 2 ]; then
    printf "%s\\n" "$OUT"
    exit 0
  fi
  sleep 2
done
printf "%s\\n" "$OUT"
exit 1
'""",
        capture=True,
    ).stdout

    if yarn_nodes.count("RUNNING") < 2:
        raise P11Error(
            "Secure LCE validation: fewer than 2 RUNNING NodeManagers"
        )

    evidence["yarn_nodes"] = "PASS"

    # Prepare shared MapReduce staging parents and p11admin HDFS home.
    remote(
        master,
        f"""sudo -u hadoop bash -lc '
set -euo pipefail
. /etc/p11-platform.env

HDFS={HADOOP_HOME}/bin/hdfs

# Authenticate when this validator runs after Kerberos cutover.
if [ -f /etc/security/keytabs/hdfs.service.keytab ]; then
    export KRB5CCNAME="/tmp/p11-lce-hdfs-$$.ccache"
    kdestroy 2>/dev/null || true
    kinit -kt /etc/security/keytabs/hdfs.service.keytab "hdfs/$(hostname -f)@LAB.EXAMPLE.COM"
    klist -s
fi

$HDFS dfs -mkdir -p /tmp/hadoop-yarn/staging/history/done
$HDFS dfs -mkdir -p /tmp/hadoop-yarn/staging/history/done_intermediate

$HDFS dfs -chown hadoop:hadoop /tmp/hadoop-yarn
$HDFS dfs -chown hadoop:hadoop /tmp/hadoop-yarn/staging
$HDFS dfs -chown hadoop:hadoop /tmp/hadoop-yarn/staging/history
$HDFS dfs -chown hadoop:hadoop /tmp/hadoop-yarn/staging/history/done
$HDFS dfs -chown hadoop:hadoop /tmp/hadoop-yarn/staging/history/done_intermediate

$HDFS dfs -chmod 1777 /tmp/hadoop-yarn
$HDFS dfs -chmod 1777 /tmp/hadoop-yarn/staging
$HDFS dfs -chmod 0711 /tmp/hadoop-yarn/staging/history
$HDFS dfs -chmod 0770 /tmp/hadoop-yarn/staging/history/done
$HDFS dfs -chmod 1777 /tmp/hadoop-yarn/staging/history/done_intermediate

$HDFS dfs -mkdir -p /user/p11admin
$HDFS dfs -chown p11admin:p11admin /user/p11admin
$HDFS dfs -chmod 0750 /user/p11admin

if [ -f /etc/security/keytabs/hdfs.service.keytab ]; then
    kdestroy 2>/dev/null || true
    rm -f "$KRB5CCNAME"
fi
'""",
        capture=True,
    )

    # This is the important functional test: application owner is p11admin.
    mr = remote(
        master,
        f"""sudo -u p11admin bash -lc '
. /etc/p11-platform.env

# Authenticate workload owner when Kerberos is enabled.
if [ -f /home/p11admin/.keytabs/p11admin.keytab ]; then
    export KRB5CCNAME="/tmp/p11-lce-mr-$$.ccache"
    kdestroy 2>/dev/null || true
    kinit -kt /home/p11admin/.keytabs/p11admin.keytab p11admin@LAB.EXAMPLE.COM
    klist -s
fi
timeout 180s \
{HADOOP_HOME}/bin/yarn jar \
{HADOOP_HOME}/share/hadoop/mapreduce/hadoop-mapreduce-examples-{HADOOP_VERSION}.jar \
pi 2 100

RC=$?

if [ -f /home/p11admin/.keytabs/p11admin.keytab ]; then
    kdestroy 2>/dev/null || true
    rm -f "$KRB5CCNAME"
fi

exit "$RC"
'""",
        capture=True,
    )

    mr_text = (mr.stdout or "") + (mr.stderr or "")

    if "Estimated value of Pi is" not in mr_text:
        print(mr_text)
        raise P11Error(
            "LinuxContainerExecutor workload as p11admin failed"
        )

    evidence["mapreduce_as_p11admin"] = "PASS"

    secure_dir = RESULTS_DIR / "secure"
    secure_dir.mkdir(parents=True, exist_ok=True)

    path = secure_dir / "lce.json"
    path.write_text(json.dumps(evidence, indent=2) + "\n")

    print(f"evidence: {path}")
    print(json.dumps(evidence, indent=2))

    return evidence

def secure_lce():
    nodes = discover_nodes()

    validate_secure_identity(nodes)

    by = node_map(nodes)
    workers = [by["worker-01"], by["worker-02"]]

    parallel(
        workers,
        lambda n: remote_root(n, secure_lce_worker_script()),
        "SECURE LINUX CONTAINER EXECUTOR",
    )

    validate_secure_lce(nodes)

    print("P11 SECURE LCE: PASS")

    return nodes



# ---------------------------------------------------------------------------
# Secure profile orchestration
#
# Keep this False until every secure stage below is implemented and the
# consolidated secure validator passes on a real AWS deployment.
# ---------------------------------------------------------------------------

SECURE_AUTOMATION_READY = True


def _secure_stage_pending(stage):
    raise P11Error(
        f"{stage} automation is not implemented yet; "
        "secure full remains safety-gated"
    )


def secure_kerberos_client_script(master_fqdn):
    return f"""set -euo pipefail

dnf install -y krb5-workstation >/dev/null

cat >/etc/krb5.conf <<'EOF'
[libdefaults]
    default_realm = LAB.EXAMPLE.COM
    dns_lookup_realm = false
    dns_lookup_kdc = false
    rdns = false
    forwardable = true
    ticket_lifetime = 24h
    renew_lifetime = 7d

[realms]
    LAB.EXAMPLE.COM = {{
        kdc = {master_fqdn}
        admin_server = {master_fqdn}
    }}

[domain_realm]
    .lab.example.com = LAB.EXAMPLE.COM
    lab.example.com = LAB.EXAMPLE.COM
EOF

chmod 0644 /etc/krb5.conf

echo P11_KERBEROS_CLIENT_CONFIG=PASS
"""


def secure_kerberos_master_script(nodes):
    realm = "LAB.EXAMPLE.COM"
    by = node_map(nodes)
    master = by["master-01"]
    master_fqdn = master["fqdn"]

    principals = []

    for node in nodes:
        fqdn = node["fqdn"]

        principals.extend(
            [
                f"hdfs/{fqdn}@{realm}",
                f"yarn/{fqdn}@{realm}",
                f"HTTP/{fqdn}@{realm}",
            ]
        )

    principals.extend(
        [
            f"mapred/{master_fqdn}@{realm}",
            f"hive/{master_fqdn}@{realm}",
            f"p11admin@{realm}",
        ]
    )

    create_principals = []

    for principal in principals:
        create_principals.append(
            f"""if ! kadmin.local -q "getprinc {principal}" 2>&1 | grep -q '^Principal:'; then
    kadmin.local -q "addprinc -randkey {principal}" >/dev/null
    echo "CREATED_PRINCIPAL={principal}"
else
    echo "EXISTING_PRINCIPAL={principal}"
fi"""
        )

    export_keytabs = []

    for node in nodes:
        name = node["name"]
        fqdn = node["fqdn"]

        for service, filename in [
            ("hdfs", "hdfs"),
            ("yarn", "yarn"),
            ("HTTP", "http"),
        ]:
            out = (
                f"/var/lib/p11-keytabs-export/"
                f"{name}-{filename}.keytab"
            )
            principal = f"{service}/{fqdn}@{realm}"

            export_keytabs.append(
                f"""rm -f {out}
kadmin.local -q "ktadd -norandkey -k {out} {principal}" >/dev/null
chmod 0600 {out}"""
            )

    for service in ("mapred", "hive"):
        out = (
            f"/var/lib/p11-keytabs-export/"
            f"master-01-{service}.keytab"
        )
        principal = f"{service}/{master_fqdn}@{realm}"

        export_keytabs.append(
            f"""rm -f {out}
kadmin.local -q "ktadd -norandkey -k {out} {principal}" >/dev/null
chmod 0600 {out}"""
        )

    p11_keytab = (
        "/var/lib/p11-keytabs-export/p11admin.keytab"
    )

    export_keytabs.append(
        f"""rm -f {p11_keytab}
kadmin.local -q "ktadd -norandkey -k {p11_keytab} p11admin@{realm}" >/dev/null
chmod 0600 {p11_keytab}"""
    )

    principal_text = "\n\n".join(
        create_principals
    )

    keytab_text = "\n\n".join(
        export_keytabs
    )

    return f"""set -euo pipefail
umask 077

dnf install -y krb5-server krb5-workstation >/dev/null

mkdir -p /var/kerberos/krb5kdc

cat >/var/kerberos/krb5kdc/kdc.conf <<'EOF'
[kdcdefaults]
    kdc_ports = 88
    kdc_tcp_ports = 88

[realms]
    {realm} = {{
        acl_file = /var/kerberos/krb5kdc/kadm5.acl
        admin_keytab = /var/kerberos/krb5kdc/kadm5.keytab
        database_name = /var/kerberos/krb5kdc/principal
        key_stash_file = /var/kerberos/krb5kdc/.k5.{realm}
        max_life = 24h
        max_renewable_life = 7d
        supported_enctypes = aes256-cts-hmac-sha1-96:normal aes128-cts-hmac-sha1-96:normal
    }}
EOF

cat >/var/kerberos/krb5kdc/kadm5.acl <<'EOF'
*/admin@{realm} *
EOF

chmod 0600 /var/kerberos/krb5kdc/kdc.conf
chmod 0600 /var/kerberos/krb5kdc/kadm5.acl

SECRETS=/etc/p11-kerberos-secrets.env

if [ ! -f "$SECRETS" ]; then
    touch "$SECRETS"
    chmod 0600 "$SECRETS"
fi

if ! grep -q '^P11_KDC_MASTER_PASSWORD=' "$SECRETS"; then
    P11_KDC_MASTER_PASSWORD=$(openssl rand -hex 24)

    printf 'P11_KDC_MASTER_PASSWORD=%s\\n' \
        "$P11_KDC_MASTER_PASSWORD" \
        >>"$SECRETS"
fi

chmod 0600 "$SECRETS"

. "$SECRETS"

if [ ! -f /var/kerberos/krb5kdc/principal ]; then
    kdb5_util create \
        -s \
        -r {realm} \
        -P "$P11_KDC_MASTER_PASSWORD"

    echo KERBEROS_DATABASE_CREATED
else
    echo KERBEROS_DATABASE_ALREADY_EXISTS
fi

echo
echo "===== PRINCIPALS ====="

{principal_text}

systemctl enable krb5kdc kadmin >/dev/null

systemctl restart krb5kdc
systemctl restart kadmin

systemctl is-active --quiet krb5kdc
systemctl is-active --quiet kadmin

echo
echo "===== EXPORT KEYTABS ====="

rm -rf /var/lib/p11-keytabs-export

install -d \
    -o root \
    -g root \
    -m 0700 \
    /var/lib/p11-keytabs-export

{keytab_text}

install -d \
    -o root \
    -g hadoop \
    -m 0750 \
    /etc/security/keytabs

install \
    -o root \
    -g hadoop \
    -m 0440 \
    /var/lib/p11-keytabs-export/master-01-hdfs.keytab \
    /etc/security/keytabs/hdfs.service.keytab

install \
    -o root \
    -g hadoop \
    -m 0440 \
    /var/lib/p11-keytabs-export/master-01-yarn.keytab \
    /etc/security/keytabs/yarn.service.keytab

install \
    -o root \
    -g hadoop \
    -m 0440 \
    /var/lib/p11-keytabs-export/master-01-http.keytab \
    /etc/security/keytabs/http.service.keytab

install \
    -o root \
    -g hadoop \
    -m 0440 \
    /var/lib/p11-keytabs-export/master-01-mapred.keytab \
    /etc/security/keytabs/mapred.service.keytab

install \
    -o root \
    -g hadoop \
    -m 0440 \
    /var/lib/p11-keytabs-export/master-01-hive.keytab \
    /etc/security/keytabs/hive.service.keytab

install -d \
    -o p11admin \
    -g p11admin \
    -m 0700 \
    /home/p11admin/.keytabs

install \
    -o p11admin \
    -g p11admin \
    -m 0400 \
    /var/lib/p11-keytabs-export/p11admin.keytab \
    /home/p11admin/.keytabs/p11admin.keytab

echo P11_KERBEROS_MASTER_CONFIG=PASS
"""


def _install_keytab_from_master(
    master,
    target,
    source,
    destination,
):
    cp = remote(
        master,
        f"sudo base64 -w0 {source}",
        capture=True,
    )

    payload = (cp.stdout or "").strip()

    if not payload:
        raise P11Error(
            f"empty keytab payload for "
            f"{target['name']}:{destination}"
        )

    remote_root(
        target,
        f"""set -euo pipefail

install -d \
    -o root \
    -g hadoop \
    -m 0750 \
    /etc/security/keytabs

printf '%s' '{payload}' \
    | base64 -d \
    > {destination}

chown root:hadoop {destination}
chmod 0440 {destination}

test -s {destination}
""",
    )


def validate_secure_kerberos(nodes=None):
    if nodes is None:
        nodes = discover_nodes()

    by = node_map(nodes)
    master = by["master-01"]

    realm = "LAB.EXAMPLE.COM"
    master_fqdn = master["fqdn"]

    evidence = {
        "realm": realm,
        "kdc": master_fqdn,
        "services": {},
        "nodes": {},
        "master_extra": {},
    }

    service_state = remote(
        master,
        "sudo systemctl is-active krb5kdc kadmin",
        capture=True,
    ).stdout

    active_count = sum(
        1
        for line in service_state.splitlines()
        if line.strip() == "active"
    )

    if active_count != 2:
        raise P11Error(
            "Kerberos KDC/admin services are not both active"
        )

    evidence["services"]["krb5kdc"] = "PASS"
    evidence["services"]["kadmin"] = "PASS"

    for node in nodes:
        name = node["name"]
        fqdn = node["fqdn"]

        node_result = {}

        port = remote(
            node,
            (
                "timeout 5 bash -c "
                f"'</dev/tcp/{master_fqdn}/88'"
            ),
            check=False,
            capture=True,
        )

        if port.returncode != 0:
            raise P11Error(
                f"{name} cannot reach Kerberos KDC TCP/88"
            )

        node_result["kdc_tcp_88"] = "PASS"

        for service, keytab in [
            (
                "hdfs",
                "/etc/security/keytabs/"
                "hdfs.service.keytab",
            ),
            (
                "yarn",
                "/etc/security/keytabs/"
                "yarn.service.keytab",
            ),
        ]:
            principal = f"{service}/{fqdn}@{realm}"
            cache = (
                f"/tmp/p11-{service}-validate.ccache"
            )

            cp = remote(
                node,
                f"""sudo -u hadoop bash -c '
set -euo pipefail
export KRB5CCNAME={cache}
kdestroy 2>/dev/null || true
kinit -kt {keytab} {principal}
klist -s
kdestroy
'""",
                check=False,
                capture=True,
            )

            if cp.returncode != 0:
                print(cp.stdout or "")
                print(cp.stderr or "")

                raise P11Error(
                    "Kerberos keytab validation failed: "
                    f"{name} {principal}"
                )

            node_result[
                f"{service}_kinit"
            ] = "PASS"

        http_principal = f"HTTP/{fqdn}@{realm}"

        http = remote(
            node,
            (
                "sudo klist -kte "
                "/etc/security/keytabs/"
                "http.service.keytab "
                f"| grep -F '{http_principal}'"
            ),
            check=False,
            capture=True,
        )

        if http.returncode != 0:
            raise P11Error(
                f"HTTP keytab validation failed on {name}"
            )

        node_result["http_keytab"] = "PASS"

        evidence["nodes"][name] = node_result

    for service in ("mapred", "hive"):
        principal = (
            f"{service}/{master_fqdn}@{realm}"
        )

        keytab = (
            f"/etc/security/keytabs/"
            f"{service}.service.keytab"
        )

        cp = remote(
            master,
            (
                f"sudo klist -kte {keytab} "
                f"| grep -F '{principal}'"
            ),
            check=False,
            capture=True,
        )

        if cp.returncode != 0:
            raise P11Error(
                f"{service} service keytab "
                "validation failed"
            )

        evidence["master_extra"][
            f"{service}_keytab"
        ] = "PASS"

    p11 = remote(
        master,
        f"""cd /tmp && sudo -u p11admin bash -c '
set -euo pipefail
export KRB5CCNAME=/tmp/p11admin-validate.ccache
kdestroy 2>/dev/null || true
kinit \
    -kt /home/p11admin/.keytabs/p11admin.keytab \
    p11admin@{realm}
klist -s
kdestroy
'""",
        check=False,
        capture=True,
    )

    if p11.returncode != 0:
        print(p11.stdout or "")
        print(p11.stderr or "")

        raise P11Error(
            "p11admin Kerberos keytab validation failed"
        )

    evidence["master_extra"][
        "p11admin_kinit"
    ] = "PASS"

    evidence["overall"] = "PASS"

    secure_dir = RESULTS_DIR / "secure"
    secure_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = secure_dir / "kerberos.json"

    path.write_text(
        json.dumps(evidence, indent=2)
        + "\n"
    )

    print(f"evidence: {path}")
    print(json.dumps(evidence, indent=2))

    return evidence


def secure_kerberos():
    nodes = discover_nodes()

    validate_secure_identity(nodes)

    by = node_map(nodes)
    master = by["master-01"]

    workers = [
        by["worker-01"],
        by["worker-02"],
    ]

    parallel(
        nodes,
        lambda n: remote_root(
            n,
            secure_kerberos_client_script(
                master["fqdn"]
            ),
        ),
        "SECURE KERBEROS CLIENT CONFIG",
    )

    print(
        "\n========== SECURE KERBEROS KDC =========="
    )

    remote_root(
        master,
        secure_kerberos_master_script(nodes),
    )

    for worker in workers:
        for service in (
            "hdfs",
            "yarn",
            "http",
        ):
            source = (
                "/var/lib/p11-keytabs-export/"
                f"{worker['name']}-{service}.keytab"
            )

            destination = (
                "/etc/security/keytabs/"
                f"{service}.service.keytab"
            )

            _install_keytab_from_master(
                master,
                worker,
                source,
                destination,
            )

    remote_root(
        master,
        "rm -rf /var/lib/p11-keytabs-export",
    )

    validate_secure_kerberos(nodes)

    print("P11 SECURE KERBEROS: PASS")

    return nodes


def _node_private_ipv4(node):
    cp = remote(
        node,
        (
            "ip -o -4 addr show scope global "
            "| awk 'NR==1 {print $4}' "
            "| cut -d/ -f1"
        ),
        capture=True,
    )

    ip = (cp.stdout or "").strip()

    if not ip:
        raise P11Error(
            f"could not determine private IPv4 for "
            f"{node['name']}"
        )

    return ip


def _tls_material_complete(node):
    cp = remote(
        node,
        """sudo test -s /etc/security/tls/server.p12 &&
sudo test -s /etc/security/tls/truststore.p12 &&
sudo test -s /etc/security/tls/ca.crt &&
sudo test -s /etc/security/tls/storepass""",
        check=False,
        capture=True,
    )

    return cp.returncode == 0


def secure_tls_master_script(nodes, node_ips):
    cert_blocks = []

    for node in nodes:
        name = node["name"]
        fqdn = node["fqdn"]
        ip = node_ips[name]

        out = f"/var/lib/p11-tls-export/{name}"

        cert_blocks.append(
            f"""echo "===== TLS CERT: {name} ====="

OUT={out}

rm -rf "$OUT"

install -d \
    -o root \
    -g root \
    -m 0700 \
    "$OUT"

openssl genrsa \
    -out "$OUT/server.key" \
    2048 >/dev/null 2>&1

openssl req \
    -new \
    -key "$OUT/server.key" \
    -out "$OUT/server.csr" \
    -subj "/C=US/O=P11 Lab/OU=Hadoop/CN={fqdn}"

cat >"$OUT/extensions.cnf" <<'EOF'
[v3_req]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth,clientAuth
subjectAltName = DNS:{fqdn},DNS:{name},IP:{ip}
EOF

openssl x509 \
    -req \
    -in "$OUT/server.csr" \
    -CA "$PKI/ca.crt" \
    -CAkey "$PKI/ca.key" \
    -CAcreateserial \
    -out "$OUT/server.crt" \
    -days 825 \
    -sha256 \
    -extfile "$OUT/extensions.cnf" \
    -extensions v3_req \
    >/dev/null 2>&1

openssl verify \
    -CAfile "$PKI/ca.crt" \
    "$OUT/server.crt"

export STOREPASS

openssl pkcs12 \
    -export \
    -inkey "$OUT/server.key" \
    -in "$OUT/server.crt" \
    -certfile "$PKI/ca.crt" \
    -name "{fqdn}" \
    -out "$OUT/server.p12" \
    -passout env:STOREPASS

rm -f "$OUT/truststore.p12"

keytool \
    -importcert \
    -noprompt \
    -alias p11-root-ca \
    -file "$PKI/ca.crt" \
    -keystore "$OUT/truststore.p12" \
    -storetype PKCS12 \
    -storepass "$STOREPASS" \
    >/dev/null

cp "$PKI/ca.crt" "$OUT/ca.crt"

printf '%s' "$STOREPASS" \
    >"$OUT/storepass"

chmod 0600 \
    "$OUT/server.p12" \
    "$OUT/truststore.p12" \
    "$OUT/storepass"

chmod 0644 "$OUT/ca.crt"

openssl x509 \
    -in "$OUT/server.crt" \
    -noout \
    -subject \
    -issuer \
    -ext subjectAltName
"""
        )

    cert_text = "\n".join(cert_blocks)

    return f"""set -euo pipefail
umask 077

PKI=/etc/p11-pki
SECRETS=/etc/p11-tls-secrets.env

dnf install -y openssl >/dev/null

install -d \
    -o root \
    -g root \
    -m 0700 \
    "$PKI"

if [ -e "$PKI/ca.key" ] || [ -e "$PKI/ca.crt" ]; then
    if [ ! -s "$PKI/ca.key" ] || [ ! -s "$PKI/ca.crt" ]; then
        echo "Partial P11 automation CA material exists" >&2
        exit 1
    fi

    echo P11_TLS_CA_ALREADY_EXISTS
else
    openssl genrsa \
        -out "$PKI/ca.key" \
        4096 >/dev/null 2>&1

    chmod 0600 "$PKI/ca.key"

    openssl req \
        -x509 \
        -new \
        -key "$PKI/ca.key" \
        -sha256 \
        -days 3650 \
        -out "$PKI/ca.crt" \
        -subj "/C=US/O=P11 Lab/OU=Platform Security/CN=P11 Lab Root CA"

    chmod 0644 "$PKI/ca.crt"

    echo P11_TLS_CA_CREATED
fi

if [ ! -f "$SECRETS" ]; then
    touch "$SECRETS"
    chmod 0600 "$SECRETS"
fi

if ! grep -q '^P11_TLS_STOREPASS=' "$SECRETS"; then
    P11_TLS_STOREPASS=$(openssl rand -hex 16)

    printf 'P11_TLS_STOREPASS=%s\\n' \
        "$P11_TLS_STOREPASS" \
        >>"$SECRETS"
fi

chmod 0600 "$SECRETS"

. "$SECRETS"

STOREPASS="$P11_TLS_STOREPASS"

if [ -z "$STOREPASS" ]; then
    echo "P11 TLS store password is empty" >&2
    exit 1
fi

rm -rf /var/lib/p11-tls-export

install -d \
    -o root \
    -g root \
    -m 0700 \
    /var/lib/p11-tls-export

{cert_text}

echo
echo "===== CA FINGERPRINT ====="

openssl x509 \
    -in "$PKI/ca.crt" \
    -noout \
    -fingerprint \
    -sha256

echo P11_TLS_MASTER_GENERATION=PASS
"""


def _install_tls_file_from_master(
    master,
    target,
    source,
    destination,
    mode,
):
    cp = remote(
        master,
        f"sudo base64 -w0 {source}",
        capture=True,
    )

    payload = (cp.stdout or "").strip()

    if not payload:
        raise P11Error(
            f"empty TLS payload for "
            f"{target['name']}:{destination}"
        )

    remote_root(
        target,
        f"""set -euo pipefail

install -d \
    -o root \
    -g hadoop \
    -m 0750 \
    /etc/security/tls

printf '%s' '{payload}' \
    | base64 -d \
    > {destination}

chown root:hadoop {destination}
chmod {mode} {destination}

test -s {destination}
""",
    )


def validate_secure_tls(nodes=None):
    if nodes is None:
        nodes = discover_nodes()

    evidence = {
        "nodes": {},
        "ca_sha256": None,
    }

    fingerprints = []

    for node in nodes:
        name = node["name"]
        fqdn = node["fqdn"]
        ip = _node_private_ipv4(node)

        script = f"""set -euo pipefail

TLS=/etc/security/tls
PASS=$(cat "$TLS/storepass")

test -n "$PASS"

keytool \
    -list \
    -keystore "$TLS/server.p12" \
    -storetype PKCS12 \
    -storepass "$PASS" \
    | grep -q 'PrivateKeyEntry'

keytool \
    -list \
    -keystore "$TLS/truststore.p12" \
    -storetype PKCS12 \
    -storepass "$PASS" \
    -alias p11-root-ca \
    >/dev/null

CERT=$(mktemp)

trap 'rm -f "$CERT"' EXIT

openssl pkcs12 \
    -in "$TLS/server.p12" \
    -clcerts \
    -nokeys \
    -passin file:"$TLS/storepass" \
    -out "$CERT" \
    >/dev/null 2>&1

openssl verify \
    -CAfile "$TLS/ca.crt" \
    "$CERT"

echo "DIR=$(stat -c '%U:%G:%a' "$TLS")"
echo "SERVER=$(stat -c '%U:%G:%a' "$TLS/server.p12")"
echo "TRUST=$(stat -c '%U:%G:%a' "$TLS/truststore.p12")"
echo "PASSFILE=$(stat -c '%U:%G:%a' "$TLS/storepass")"
echo "CAFILE=$(stat -c '%U:%G:%a' "$TLS/ca.crt")"

openssl x509 \
    -in "$CERT" \
    -noout \
    -subject \
    -issuer \
    -ext subjectAltName

openssl x509 \
    -in "$TLS/ca.crt" \
    -noout \
    -fingerprint \
    -sha256
"""

        cp = remote_root(
            node,
            script,
            capture=True,
        )

        text = (
            (cp.stdout or "")
            + "\n"
            + (cp.stderr or "")
        )

        expected_permissions = [
            "DIR=root:hadoop:750",
            "SERVER=root:hadoop:440",
            "TRUST=root:hadoop:440",
            "PASSFILE=root:hadoop:440",
            "CAFILE=root:hadoop:444",
        ]

        for expected in expected_permissions:
            if expected not in text:
                print(text)

                raise P11Error(
                    f"TLS permission validation failed "
                    f"on {name}: expected {expected}"
                )

        if f"DNS:{fqdn}" not in text:
            print(text)

            raise P11Error(
                f"TLS FQDN SAN missing on {name}"
            )

        if f"DNS:{name}" not in text:
            print(text)

            raise P11Error(
                f"TLS short-name SAN missing on {name}"
            )

        if f"IP Address:{ip}" not in text:
            print(text)

            raise P11Error(
                f"TLS IPv4 SAN missing on {name}"
            )

        fingerprint = None

        for line in text.splitlines():
            if "Fingerprint=" in line:
                fingerprint = line.strip()
                break

        if not fingerprint:
            print(text)

            raise P11Error(
                f"could not read CA fingerprint on {name}"
            )

        fingerprints.append(fingerprint)

        evidence["nodes"][name] = {
            "private_ip": ip,
            "server_pkcs12": "PASS",
            "truststore": "PASS",
            "ca_verify": "PASS",
            "san_fqdn": "PASS",
            "san_short_name": "PASS",
            "san_private_ip": "PASS",
            "permissions": "PASS",
            "ca_fingerprint": fingerprint,
        }

    if len(set(fingerprints)) != 1:
        raise P11Error(
            "TLS CA fingerprint differs between nodes"
        )

    evidence["ca_sha256"] = fingerprints[0]
    evidence["overall"] = "PASS"

    secure_dir = RESULTS_DIR / "secure"

    secure_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = secure_dir / "tls.json"

    path.write_text(
        json.dumps(evidence, indent=2)
        + "\n"
    )

    print(f"evidence: {path}")
    print(json.dumps(evidence, indent=2))

    return evidence


def secure_tls():
    nodes = discover_nodes()

    validate_secure_kerberos(nodes)

    by = node_map(nodes)
    master = by["master-01"]

    existing = {
        node["name"]: _tls_material_complete(node)
        for node in nodes
    }

    print(
        "TLS material state: "
        + json.dumps(existing, sort_keys=True)
    )

    if all(existing.values()):
        print(
            "Existing complete TLS material detected "
            "on all nodes; preserving certificates."
        )

    else:
        automation_ca = remote(
            master,
            """sudo test -s /etc/p11-pki/ca.key &&
sudo test -s /etc/p11-pki/ca.crt""",
            check=False,
            capture=True,
        ).returncode == 0

        if any(existing.values()) and not automation_ca:
            raise P11Error(
                "partial TLS material exists but the "
                "automation CA private key is unavailable; "
                "refusing to rotate the live TLS estate"
            )

        node_ips = {
            node["name"]: _node_private_ipv4(node)
            for node in nodes
        }

        print(
            "\n========== SECURE TLS GENERATION =========="
        )

        remote_root(
            master,
            secure_tls_master_script(
                nodes,
                node_ips,
            ),
        )

        for node in nodes:
            source_dir = (
                "/var/lib/p11-tls-export/"
                f"{node['name']}"
            )

            for filename, mode in [
                ("server.p12", "0440"),
                ("truststore.p12", "0440"),
                ("storepass", "0440"),
                ("ca.crt", "0444"),
            ]:
                _install_tls_file_from_master(
                    master,
                    node,
                    f"{source_dir}/{filename}",
                    f"/etc/security/tls/{filename}",
                    mode,
                )

        remote_root(
            master,
            "rm -rf /var/lib/p11-tls-export",
        )

    validate_secure_tls(nodes)

    print("P11 SECURE TLS: PASS")

    return nodes


def secure_hadoop_node_script(node, nodes):
    realm = "LAB.EXAMPLE.COM"
    master = node_map(nodes)["master-01"]
    master_fqdn = master["fqdn"]

    auth_to_local = """RULE:[2:$1@$0](hdfs@LAB[.]EXAMPLE[.]COM)s/^.*$/hadoop/
RULE:[2:$1@$0](yarn@LAB[.]EXAMPLE[.]COM)s/^.*$/hadoop/
RULE:[2:$1@$0](mapred@LAB[.]EXAMPLE[.]COM)s/^.*$/hadoop/
RULE:[2:$1@$0](HTTP@LAB[.]EXAMPLE[.]COM)s/^.*$/hadoop/
RULE:[2:$1@$0](hive@LAB[.]EXAMPLE[.]COM)s/^.*$/hadoop/
DEFAULT"""

    core_props = {
        "hadoop.security.authentication": "kerberos",
        "hadoop.security.authorization": "true",
        "hadoop.rpc.protection": "authentication",
        "hadoop.security.auth_to_local.mechanism": "hadoop",
        "hadoop.security.auth_to_local": auth_to_local,
    }

    hdfs_props = {
        "dfs.block.access.token.enable": "true",
        "dfs.namenode.kerberos.principal":
            f"hdfs/_HOST@{realm}",
        "dfs.namenode.keytab.file":
            "/etc/security/keytabs/hdfs.service.keytab",
        "dfs.datanode.kerberos.principal":
            f"hdfs/_HOST@{realm}",
        "dfs.datanode.keytab.file":
            "/etc/security/keytabs/hdfs.service.keytab",
        "dfs.web.authentication.kerberos.principal":
            f"HTTP/_HOST@{realm}",
        "dfs.web.authentication.kerberos.keytab":
            "/etc/security/keytabs/http.service.keytab",
        "dfs.namenode.kerberos.internal.spnego.principal":
            f"HTTP/_HOST@{realm}",
        "dfs.data.transfer.protection": "authentication",
        "dfs.http.policy": "HTTPS_ONLY",
        "dfs.namenode.https-address": "0.0.0.0:9871",
        "dfs.datanode.https.address": "0.0.0.0:9865",
        "dfs.client.https.keystore.resource":
            "ssl-client.xml",
        "dfs.https.server.keystore.resource":
            "ssl-server.xml",
    }

    yarn_props = {
        "yarn.resourcemanager.principal":
            f"yarn/_HOST@{realm}",
        "yarn.resourcemanager.keytab":
            "/etc/security/keytabs/yarn.service.keytab",
        "yarn.nodemanager.principal":
            f"yarn/_HOST@{realm}",
        "yarn.nodemanager.keytab":
            "/etc/security/keytabs/yarn.service.keytab",

        # Preserve the already-proven LCE configuration.
        "yarn.nodemanager.container-executor.class":
            "org.apache.hadoop.yarn.server.nodemanager."
            "LinuxContainerExecutor",
        "yarn.nodemanager.linux-container-executor.group":
            "hadoop",
        "yarn.nodemanager.linux-container-executor."
        "nonsecure-mode.limit-users":
            "false",
    }

    mapred_props = {
        "mapreduce.jobhistory.principal":
            f"mapred/_HOST@{realm}",
        "mapreduce.jobhistory.keytab":
            "/etc/security/keytabs/mapred.service.keytab",
    }

    return f"""set -euo pipefail

CONF={HADOOP_HOME}/etc/hadoop
TLS=/etc/security/tls

test -s "$TLS/server.p12"
test -s "$TLS/truststore.p12"
test -s "$TLS/storepass"

STOREPASS=$(cat "$TLS/storepass")
export STOREPASS

python3 - <<'PY'
import os
import xml.etree.ElementTree as ET

conf = "{HADOOP_HOME}/etc/hadoop"

configs = {{
    "core-site.xml": {core_props!r},
    "hdfs-site.xml": {hdfs_props!r},
    "yarn-site.xml": {yarn_props!r},
    "mapred-site.xml": {mapred_props!r},
}}

def set_properties(path, values):
    tree = ET.parse(path)
    root = tree.getroot()

    for name, value in values.items():
        matches = [
            prop
            for prop in root.findall("property")
            if prop.findtext("name") == name
        ]

        if matches:
            prop = matches[0]

            for duplicate in matches[1:]:
                root.remove(duplicate)

            value_node = prop.find("value")

            if value_node is None:
                value_node = ET.SubElement(
                    prop,
                    "value",
                )

            value_node.text = str(value)

        else:
            prop = ET.SubElement(
                root,
                "property",
            )

            ET.SubElement(
                prop,
                "name",
            ).text = name

            ET.SubElement(
                prop,
                "value",
            ).text = str(value)

    tree.write(
        path,
        encoding="UTF-8",
        xml_declaration=True,
    )


for filename, values in configs.items():
    set_properties(
        os.path.join(conf, filename),
        values,
    )


storepass = os.environ["STOREPASS"]

ssl_server = {{
    "ssl.server.truststore.location":
        "/etc/security/tls/truststore.p12",
    "ssl.server.truststore.password":
        storepass,
    "ssl.server.truststore.type":
        "PKCS12",
    "ssl.server.keystore.location":
        "/etc/security/tls/server.p12",
    "ssl.server.keystore.password":
        storepass,
    "ssl.server.keystore.keypassword":
        storepass,
    "ssl.server.keystore.type":
        "PKCS12",
}}

ssl_client = {{
    "ssl.client.truststore.location":
        "/etc/security/tls/truststore.p12",
    "ssl.client.truststore.password":
        storepass,
    "ssl.client.truststore.type":
        "PKCS12",
}}


def write_config(path, values):
    root = ET.Element("configuration")

    for name, value in values.items():
        prop = ET.SubElement(
            root,
            "property",
        )

        ET.SubElement(
            prop,
            "name",
        ).text = name

        ET.SubElement(
            prop,
            "value",
        ).text = str(value)

    ET.ElementTree(root).write(
        path,
        encoding="UTF-8",
        xml_declaration=True,
    )


write_config(
    os.path.join(conf, "ssl-server.xml"),
    ssl_server,
)

write_config(
    os.path.join(conf, "ssl-client.xml"),
    ssl_client,
)

print("SECURE_HADOOP_XML_PATCH=PASS")
PY

chown root:hadoop \
    "$CONF/core-site.xml" \
    "$CONF/hdfs-site.xml" \
    "$CONF/yarn-site.xml" \
    "$CONF/mapred-site.xml" \
    "$CONF/ssl-server.xml" \
    "$CONF/ssl-client.xml"

chmod 0644 \
    "$CONF/core-site.xml" \
    "$CONF/hdfs-site.xml" \
    "$CONF/yarn-site.xml" \
    "$CONF/mapred-site.xml"

chmod 0640 \
    "$CONF/ssl-server.xml" \
    "$CONF/ssl-client.xml"

echo
echo "===== HADOOP_CONF_DIR OVERRIDE FIX ====="

python3 - "$CONF/hadoop-env.sh" <<'PY'
import sys

path = sys.argv[1]

with open(path) as handle:
    lines = handle.readlines()

replacement = (
    'export HADOOP_CONF_DIR='
    '"${{HADOOP_CONF_DIR:-'
    '{HADOOP_HOME}/etc/hadoop'
    '}}"\\n'
)

found = False
output = []

for line in lines:
    if line.startswith(
        "export HADOOP_CONF_DIR="
    ):
        if not found:
            output.append(replacement)
            found = True

        continue

    output.append(line)

if not found:
    output.append("\\n")
    output.append(replacement)

with open(path, "w") as handle:
    handle.writelines(output)
PY

chmod 0755 "$CONF/hadoop-env.sh"

echo
echo "===== HISTORYSERVER JAVA 17 FIX ====="

touch "$CONF/mapred-env.sh"

if ! grep -q \
    -- '--add-opens=java.base/java.lang=ALL-UNNAMED' \
    "$CONF/mapred-env.sh"
then
    cat >>"$CONF/mapred-env.sh" <<'EOF'

export MAPRED_HISTORYSERVER_OPTS="${{MAPRED_HISTORYSERVER_OPTS:-}} --add-opens=java.base/java.lang=ALL-UNNAMED"
EOF
fi

chmod 0755 "$CONF/mapred-env.sh"

echo
echo "===== XML PARSE ====="

python3 - <<'PY'
import os
import xml.etree.ElementTree as ET

conf = "{HADOOP_HOME}/etc/hadoop"

for name in (
    "core-site.xml",
    "hdfs-site.xml",
    "yarn-site.xml",
    "mapred-site.xml",
    "ssl-server.xml",
    "ssl-client.xml",
):
    ET.parse(
        os.path.join(conf, name)
    )

    print(name + "=PASS")
PY

echo P11_SECURE_HADOOP_CONFIG=PASS
"""


def _wait_secure_hdfs(master, timeout=240):
    deadline = time.time() + timeout

    command = f"""sudo -u hadoop bash -c '
set -euo pipefail
export KRB5CCNAME=/tmp/p11-hdfs-admin.ccache

kdestroy 2>/dev/null || true

kinit \
    -kt /etc/security/keytabs/hdfs.service.keytab \
    hdfs/{master["fqdn"]}@LAB.EXAMPLE.COM

{HADOOP_HOME}/bin/hdfs dfsadmin -report

kdestroy
'"""

    while time.time() < deadline:
        cp = remote(
            master,
            command,
            check=False,
            capture=True,
        )

        text = (
            (cp.stdout or "")
            + "\n"
            + (cp.stderr or "")
        )

        if (
            cp.returncode == 0
            and "Live datanodes (2)" in text
        ):
            return

        time.sleep(5)

    raise P11Error(
        "secure HDFS did not report 2 live DataNodes"
    )


def _wait_secure_yarn(master, timeout=240):
    deadline = time.time() + timeout

    command = f"""cd /tmp && sudo -u p11admin bash -c '
set -euo pipefail
export KRB5CCNAME=/tmp/p11-yarn-admin.ccache

kdestroy 2>/dev/null || true

kinit \
    -kt /home/p11admin/.keytabs/p11admin.keytab \
    p11admin@LAB.EXAMPLE.COM

{HADOOP_HOME}/bin/yarn node -list -all

kdestroy
'"""

    while time.time() < deadline:
        cp = remote(
            master,
            command,
            check=False,
            capture=True,
        )

        text = (
            (cp.stdout or "")
            + "\n"
            + (cp.stderr or "")
        )

        if (
            cp.returncode == 0
            and "Total Nodes:2" in text
            and text.count("RUNNING") >= 2
        ):
            return

        time.sleep(5)

    raise P11Error(
        "secure YARN did not report 2 RUNNING NodeManagers"
    )


def validate_secure_hadoop(nodes=None):
    if nodes is None:
        nodes = discover_nodes()

    by = node_map(nodes)
    master = by["master-01"]
    workers = [
        by["worker-01"],
        by["worker-02"],
    ]

    evidence = {
        "configuration": {},
        "services": {},
        "hdfs": {},
        "yarn": {},
        "tls": {},
    }

    for node in nodes:
        name = node["name"]
        fqdn = node["fqdn"]

        cp = remote(
            node,
            f"""python3 - <<'PY'
import xml.etree.ElementTree as ET

conf = "{HADOOP_HOME}/etc/hadoop"

wanted = {{
    "core-site.xml": [
        "hadoop.security.authentication",
        "hadoop.security.authorization",
        "hadoop.security.auth_to_local",
    ],
    "hdfs-site.xml": [
        "dfs.namenode.kerberos.principal",
        "dfs.datanode.kerberos.principal",
        "dfs.http.policy",
        "dfs.data.transfer.protection",
    ],
    "yarn-site.xml": [
        "yarn.resourcemanager.principal",
        "yarn.nodemanager.principal",
        "yarn.nodemanager.container-executor.class",
        "yarn.nodemanager.linux-container-executor.nonsecure-mode.limit-users",
    ],
    "mapred-site.xml": [
        "mapreduce.jobhistory.principal",
    ],
}}

for filename, names in wanted.items():
    root = ET.parse(
        conf + "/" + filename
    ).getroot()

    values = {{
        prop.findtext("name"):
            prop.findtext("value")
        for prop in root.findall(
            "property"
        )
    }}

    for key in names:
        print(
            filename
            + ":"
            + key
            + "="
            + str(values.get(key))
        )
PY""",
            capture=True,
        )

        text = cp.stdout or ""

        required = [
            "core-site.xml:hadoop.security.authentication=kerberos",
            "core-site.xml:hadoop.security.authorization=true",
            "hdfs-site.xml:dfs.http.policy=HTTPS_ONLY",
            "hdfs-site.xml:dfs.data.transfer.protection=authentication",
            "yarn-site.xml:yarn.nodemanager.container-executor.class="
            "org.apache.hadoop.yarn.server.nodemanager.LinuxContainerExecutor",
            "yarn-site.xml:yarn.nodemanager.linux-container-executor."
            "nonsecure-mode.limit-users=false",
        ]

        for expected in required:
            if expected not in text:
                print(text)

                raise P11Error(
                    f"secure Hadoop config validation "
                    f"failed on {name}: {expected}"
                )

        mapping = remote(
            node,
            f"""HADOOP_CONF_DIR={HADOOP_HOME}/etc/hadoop \
{HADOOP_HOME}/bin/hadoop kerbname \
'hdfs/{fqdn}@LAB.EXAMPLE.COM'

HADOOP_CONF_DIR={HADOOP_HOME}/etc/hadoop \
{HADOOP_HOME}/bin/hadoop kerbname \
'p11admin@LAB.EXAMPLE.COM'
""",
            capture=True,
        ).stdout

        if " to hadoop" not in mapping:
            raise P11Error(
                f"hdfs auth_to_local failed on {name}"
            )

        if " to p11admin" not in mapping:
            raise P11Error(
                f"p11admin auth_to_local failed on {name}"
            )

        evidence["configuration"][name] = "PASS"

    master_services = remote(
        master,
        """sudo systemctl is-active \
p11-namenode \
p11-resourcemanager \
p11-historyserver""",
        capture=True,
    ).stdout

    if (
        sum(
            line.strip() == "active"
            for line in master_services.splitlines()
        )
        != 3
    ):
        raise P11Error(
            "master secure Hadoop services are not active"
        )

    evidence["services"]["master-01"] = "PASS"

    for worker in workers:
        state = remote(
            worker,
            """sudo systemctl is-active \
p11-datanode \
p11-nodemanager""",
            capture=True,
        ).stdout

        if (
            sum(
                line.strip() == "active"
                for line in state.splitlines()
            )
            != 2
        ):
            raise P11Error(
                f"secure worker services not active "
                f"on {worker['name']}"
            )

        evidence["services"][
            worker["name"]
        ] = "PASS"

    hdfs_admin = remote(
        master,
        f"""sudo -u hadoop bash -c '
set -euo pipefail
export KRB5CCNAME=/tmp/p11-hdfs-proof.ccache

kdestroy 2>/dev/null || true

kinit \
    -kt /etc/security/keytabs/hdfs.service.keytab \
    hdfs/{master["fqdn"]}@LAB.EXAMPLE.COM

{HADOOP_HOME}/bin/hdfs dfsadmin -report

kdestroy
'""",
        capture=True,
    ).stdout

    if "Live datanodes (2)" not in hdfs_admin:
        raise P11Error(
            "secure HDFS does not show 2 live DataNodes"
        )

    evidence["hdfs"]["datanodes"] = "PASS"

    hdfs_user = remote(
        master,
        f"""cd /tmp && sudo -u p11admin bash -c '
set -euo pipefail

export KRB5CCNAME=/tmp/p11-hdfs-user-proof.ccache

kdestroy 2>/dev/null || true

kinit \
    -kt /home/p11admin/.keytabs/p11admin.keytab \
    p11admin@LAB.EXAMPLE.COM

TEST=/user/p11admin/secure-hdfs-automation-$$

printf "secure-hdfs-automation\\n" \
    >/tmp/p11-hdfs-user-input-$$

{HADOOP_HOME}/bin/hdfs dfs \
    -put /tmp/p11-hdfs-user-input-$$ "$TEST"

{HADOOP_HOME}/bin/hdfs dfs \
    -cat "$TEST"

{HADOOP_HOME}/bin/hdfs dfs \
    -rm "$TEST"

rm -f /tmp/p11-hdfs-user-input-$$

kdestroy
'""",
        capture=True,
    ).stdout

    if "secure-hdfs-automation" not in hdfs_user:
        raise P11Error(
            "p11admin secure HDFS read/write failed"
        )

    evidence["hdfs"]["p11admin_rw"] = "PASS"

    yarn = remote(
        master,
        f"""cd /tmp && sudo -u p11admin bash -c '
set -euo pipefail

export KRB5CCNAME=/tmp/p11-yarn-proof.ccache

kdestroy 2>/dev/null || true

kinit \
    -kt /home/p11admin/.keytabs/p11admin.keytab \
    p11admin@LAB.EXAMPLE.COM

{HADOOP_HOME}/bin/yarn node -list

kdestroy
'""",
        capture=True,
    ).stdout

    if (
        "Total Nodes:2" not in yarn
        or yarn.count("RUNNING") < 2
    ):
        raise P11Error(
            "secure YARN validation failed"
        )

    evidence["yarn"]["nodes"] = "PASS"

    endpoints = [
        (
            master,
            master["fqdn"],
            9871,
            "namenode",
        ),
        (
            workers[0],
            workers[0]["fqdn"],
            9865,
            "datanode-worker-01",
        ),
        (
            workers[1],
            workers[1]["fqdn"],
            9865,
            "datanode-worker-02",
        ),
    ]

    for node, fqdn, port, label in endpoints:
        cp = remote(
            node,
            f"""sudo curl \
--cacert /etc/security/tls/ca.crt \
-sS \
-o /dev/null \
-w '%{{http_code}} %{{ssl_verify_result}}' \
https://{fqdn}:{port}/""",
            check=False,
            capture=True,
        )

        output = (cp.stdout or "").strip()

        if (
            cp.returncode != 0
            or not output.endswith(" 0")
        ):
            print(output)
            print(cp.stderr or "")

            raise P11Error(
                f"HDFS TLS validation failed: {label}"
            )

        evidence["tls"][label] = "PASS"

    history = remote(
        master,
        "ss -ltn | grep -q ':10020 '",
        check=False,
        capture=True,
    )

    if history.returncode != 0:
        raise P11Error(
            "secure HistoryServer is not listening on 10020"
        )

    evidence["services"]["historyserver_rpc"] = "PASS"
    evidence["overall"] = "PASS"

    secure_dir = RESULTS_DIR / "secure"

    secure_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = secure_dir / "hadoop-security.json"

    path.write_text(
        json.dumps(evidence, indent=2)
        + "\n"
    )

    print(f"evidence: {path}")
    print(json.dumps(evidence, indent=2))

    return evidence


def secure_hadoop():
    nodes = discover_nodes()

    validate_secure_kerberos(nodes)
    validate_secure_tls(nodes)

    by = node_map(nodes)

    master = by["master-01"]

    workers = [
        by["worker-01"],
        by["worker-02"],
    ]

    print(
        "\n========== STOP HADOOP FOR SECURE CONFIG =========="
    )

    parallel(
        workers,
        lambda n: remote_root(
            n,
            """systemctl stop \
p11-nodemanager \
p11-datanode || true""",
        ),
        "STOP SECURE WORKERS",
    )

    remote_root(
        master,
        """systemctl stop \
p11-historyserver \
p11-resourcemanager \
p11-namenode || true""",
    )

    parallel(
        nodes,
        lambda n: remote_root(
            n,
            secure_hadoop_node_script(
                n,
                nodes,
            ),
        ),
        "SECURE HADOOP CONFIGURATION",
    )

    print(
        "\n========== START SECURE HDFS =========="
    )

    remote_root(
        master,
        "systemctl start p11-namenode",
    )

    time.sleep(4)

    parallel(
        workers,
        lambda n: remote_root(
            n,
            "systemctl start p11-datanode",
        ),
        "START SECURE DATANODES",
    )

    _wait_secure_hdfs(master)

    print(
        "\n========== START SECURE YARN =========="
    )

    remote_root(
        master,
        "systemctl start p11-resourcemanager",
    )

    time.sleep(3)

    parallel(
        workers,
        lambda n: remote_root(
            n,
            "systemctl start p11-nodemanager",
        ),
        "START SECURE NODEMANAGERS",
    )

    _wait_secure_yarn(master)

    print(
        "\n========== START SECURE HISTORYSERVER =========="
    )

    remote_root(
        master,
        "systemctl start p11-historyserver",
    )

    deadline = time.time() + 120

    while time.time() < deadline:
        cp = remote(
            master,
            "ss -ltn | grep -q ':10020 '",
            check=False,
            capture=True,
        )

        if cp.returncode == 0:
            break

        time.sleep(4)

    else:
        remote(
            master,
            """sudo journalctl \
-u p11-historyserver \
-n 160 \
--no-pager""",
            check=False,
        )

        raise P11Error(
            "secure HistoryServer failed to start"
        )

    validate_secure_hadoop(nodes)

    print("P11 SECURE HADOOP: PASS")

    return nodes


def secure_directory_master_script(master):
    fqdn = master["fqdn"]

    return f"""set -euo pipefail
umask 077

INSTANCE=p11ldap
BASE="dc=lab,dc=example,dc=com"
PEOPLE="ou=People,$BASE"
GROUPS_DN="ou=Groups,$BASE"

SECRETS=/etc/p11-ldap-secrets.env
TLS=/etc/security/tls
HOST={fqdn}

echo "===== INSTALL 389 DS ====="

dnf module enable -y idm:DL1 >/dev/null 2>&1 || true
dnf install -y \
    389-ds-base \
    openldap-clients \
    openssl \
    >/dev/null

echo "===== LDAP SECRETS ====="

if [ ! -f "$SECRETS" ]; then
    touch "$SECRETS"
fi

chmod 0600 "$SECRETS"
chown root:root "$SECRETS"

if ! grep -q '^P11_LDAP_DM_PASSWORD=' "$SECRETS"; then
    printf 'P11_LDAP_DM_PASSWORD=%s\\n' \
        "$(openssl rand -hex 24)" \
        >>"$SECRETS"
fi

if ! grep -q '^P11_LDAP_USER_PASSWORD=' "$SECRETS"; then
    printf 'P11_LDAP_USER_PASSWORD=%s\\n' \
        "$(openssl rand -hex 24)" \
        >>"$SECRETS"
fi

if ! grep -q '^P11_LDAP_BIND_PASSWORD=' "$SECRETS"; then
    printf 'P11_LDAP_BIND_PASSWORD=%s\\n' \
        "$(openssl rand -hex 24)" \
        >>"$SECRETS"
fi

chmod 0600 "$SECRETS"

. "$SECRETS"

test -n "$P11_LDAP_DM_PASSWORD"
test -n "$P11_LDAP_USER_PASSWORD"
test -n "$P11_LDAP_BIND_PASSWORD"

echo "LDAP_SECRET_FILE=PASS"

echo
echo "===== DIRECTORY INSTANCE ====="

if [ ! -d "/etc/dirsrv/slapd-$INSTANCE" ]; then
    cat >/tmp/p11-ds.inf <<EOF
[general]
config_version = 2
full_machine_name = $HOST
strict_host_checking = False

[slapd]
instance_name = $INSTANCE
root_dn = cn=Directory Manager
root_password = $P11_LDAP_DM_PASSWORD
port = 389
secure_port = 636
self_sign_cert = True

[backend-userroot]
create_suffix_entry = True
suffix = $BASE
EOF

    dscreate from-file /tmp/p11-ds.inf

    rm -f /tmp/p11-ds.inf

    echo DIRECTORY_INSTANCE_CREATED
else
    echo DIRECTORY_INSTANCE_ALREADY_EXISTS
fi

systemctl enable --now "dirsrv@$INSTANCE"

for i in $(seq 1 30); do
    if ldapwhoami \
        -x \
        -H ldap://127.0.0.1:389 \
        -D "cn=Directory Manager" \
        -w "$P11_LDAP_DM_PASSWORD" \
        >/dev/null 2>&1
    then
        break
    fi

    sleep 2
done

ldapwhoami \
    -x \
    -H ldap://127.0.0.1:389 \
    -D "cn=Directory Manager" \
    -w "$P11_LDAP_DM_PASSWORD" \
    >/dev/null

echo DIRECTORY_MANAGER_BIND=PASS

echo
echo "===== DIRECTORY TREE ====="

if ! ldapsearch \
    -x \
    -LLL \
    -H ldap://127.0.0.1:389 \
    -D "cn=Directory Manager" \
    -w "$P11_LDAP_DM_PASSWORD" \
    -b "$PEOPLE" \
    -s base \
    dn \
    >/dev/null 2>&1
then
    ldapadd \
        -x \
        -H ldap://127.0.0.1:389 \
        -D "cn=Directory Manager" \
        -w "$P11_LDAP_DM_PASSWORD" <<EOF
dn: $PEOPLE
objectClass: top
objectClass: organizationalUnit
ou: People
EOF

    echo PEOPLE_OU_CREATED
else
    echo PEOPLE_OU_ALREADY_EXISTS
fi

if ! ldapsearch \
    -x \
    -LLL \
    -H ldap://127.0.0.1:389 \
    -D "cn=Directory Manager" \
    -w "$P11_LDAP_DM_PASSWORD" \
    -b "$GROUPS_DN" \
    -s base \
    dn \
    >/dev/null 2>&1
then
    ldapadd \
        -x \
        -H ldap://127.0.0.1:389 \
        -D "cn=Directory Manager" \
        -w "$P11_LDAP_DM_PASSWORD" <<EOF
dn: $GROUPS_DN
objectClass: top
objectClass: organizationalUnit
ou: Groups
EOF

    echo GROUPS_OU_CREATED
else
    echo GROUPS_OU_ALREADY_EXISTS
fi

echo
echo "===== P11 USER ====="

P11USER="uid=p11user,$PEOPLE"

if ! ldapsearch \
    -x \
    -LLL \
    -H ldap://127.0.0.1:389 \
    -D "cn=Directory Manager" \
    -w "$P11_LDAP_DM_PASSWORD" \
    -b "$P11USER" \
    -s base \
    dn \
    >/dev/null 2>&1
then
    ldapadd \
        -x \
        -H ldap://127.0.0.1:389 \
        -D "cn=Directory Manager" \
        -w "$P11_LDAP_DM_PASSWORD" <<EOF
dn: $P11USER
objectClass: top
objectClass: person
objectClass: organizationalPerson
objectClass: inetOrgPerson
objectClass: posixAccount
uid: p11user
cn: P11 User
sn: User
uidNumber: 1200
gidNumber: 1200
homeDirectory: /home/p11user
loginShell: /bin/bash
userPassword: $P11_LDAP_USER_PASSWORD
EOF

    echo P11USER_CREATED
else
    echo P11USER_ALREADY_EXISTS
fi

echo
echo "===== HS2 BIND USER ====="

HS2BIND="uid=hs2bind,$PEOPLE"

if ! ldapsearch \
    -x \
    -LLL \
    -H ldap://127.0.0.1:389 \
    -D "cn=Directory Manager" \
    -w "$P11_LDAP_DM_PASSWORD" \
    -b "$HS2BIND" \
    -s base \
    dn \
    >/dev/null 2>&1
then
    ldapadd \
        -x \
        -H ldap://127.0.0.1:389 \
        -D "cn=Directory Manager" \
        -w "$P11_LDAP_DM_PASSWORD" <<EOF
dn: $HS2BIND
objectClass: top
objectClass: person
objectClass: organizationalPerson
objectClass: inetOrgPerson
uid: hs2bind
cn: HS2 LDAP Bind
sn: Bind
userPassword: $P11_LDAP_BIND_PASSWORD
EOF

    echo HS2BIND_CREATED
else
    echo HS2BIND_ALREADY_EXISTS
fi

echo
echo "===== DATA-USERS GROUP ====="

DATAUSERS="cn=data-users,$GROUPS_DN"

if ! ldapsearch \
    -x \
    -LLL \
    -H ldap://127.0.0.1:389 \
    -D "cn=Directory Manager" \
    -w "$P11_LDAP_DM_PASSWORD" \
    -b "$DATAUSERS" \
    -s base \
    dn \
    >/dev/null 2>&1
then
    ldapadd \
        -x \
        -H ldap://127.0.0.1:389 \
        -D "cn=Directory Manager" \
        -w "$P11_LDAP_DM_PASSWORD" <<EOF
dn: $DATAUSERS
objectClass: top
objectClass: groupOfNames
objectClass: posixGroup
cn: data-users
gidNumber: 1200
member: $P11USER
memberUid: p11user
EOF

    echo DATA_USERS_GROUP_CREATED
else
    echo DATA_USERS_GROUP_ALREADY_EXISTS
fi

echo
echo "===== HS2 READ ACI ====="

if ! ldapsearch \
    -x \
    -LLL \
    -o ldif-wrap=no \
    -H ldap://127.0.0.1:389 \
    -D "cn=Directory Manager" \
    -w "$P11_LDAP_DM_PASSWORD" \
    -b "$BASE" \
    -s base \
    aci \
    | grep -Fq 'acl "HS2 LDAP bind search"'
then
    ldapmodify \
        -x \
        -H ldap://127.0.0.1:389 \
        -D "cn=Directory Manager" \
        -w "$P11_LDAP_DM_PASSWORD" <<EOF
dn: $BASE
changetype: modify
add: aci
aci: (targetattr="uid || cn || objectClass || member || memberUid")(version 3.0; acl "HS2 LDAP bind search"; allow (read,search,compare)(userdn="ldap:///$HS2BIND");)
EOF

    echo HS2_READ_ACI_CREATED
else
    echo HS2_READ_ACI_ALREADY_EXISTS
fi

echo
echo "===== 389 DS TLS ====="

test -s "$TLS/server.p12"
test -s "$TLS/ca.crt"
test -s "$TLS/storepass"

SERVER_OK=0

if dsctl "$INSTANCE" tls show-server-cert 2>/dev/null \
    | grep -Fq "CN=$HOST"
then
    if dsctl "$INSTANCE" tls show-server-cert 2>/dev/null \
        | grep -Fq "CN=P11 Lab Root CA"
    then
        SERVER_OK=1
    fi
fi

if [ "$SERVER_OK" -eq 1 ]; then
    echo EXISTING_P11_LDAPS_CERTIFICATE_PRESERVED
else
    WORK=/var/lib/p11-directory-tls

    rm -rf "$WORK"

    install -d \
        -o root \
        -g root \
        -m 0700 \
        "$WORK"

    openssl pkcs12 \
        -in "$TLS/server.p12" \
        -clcerts \
        -nokeys \
        -passin file:"$TLS/storepass" \
        -out "$WORK/server.crt" \
        >/dev/null 2>&1

    openssl pkcs12 \
        -in "$TLS/server.p12" \
        -nocerts \
        -nodes \
        -passin file:"$TLS/storepass" \
        -out "$WORK/server.key" \
        >/dev/null 2>&1

    chmod 0600 \
        "$WORK/server.crt" \
        "$WORK/server.key"

    if ! dsctl "$INSTANCE" tls list-ca 2>/dev/null \
        | grep -Fq "P11 Lab Root CA"
    then
        dsctl "$INSTANCE" tls import-ca \
            "$TLS/ca.crt"

        echo P11_CA_IMPORTED_TO_389DS
    else
        echo P11_CA_ALREADY_IN_389DS
    fi

    systemctl stop "dirsrv@$INSTANCE"

    dsctl "$INSTANCE" tls import-server-key-cert \
        "$WORK/server.crt" \
        "$WORK/server.key"

    rm -rf "$WORK"

    systemctl start "dirsrv@$INSTANCE"

    echo P11_SERVER_CERT_IMPORTED_TO_389DS
fi

echo
echo "===== ENABLE LDAPS ====="

dsconf "$INSTANCE" config replace \
    nsslapd-security=on \
    >/dev/null

dsconf "$INSTANCE" security rsa enable \
    >/dev/null 2>&1 || true

systemctl restart "dirsrv@$INSTANCE"

for i in $(seq 1 30); do
    if ss -ltn | grep -q ':636 '; then
        break
    fi

    sleep 2
done

ss -ltn | grep -q ':389 '
ss -ltn | grep -q ':636 '

echo P11_SECURE_DIRECTORY_CONFIG=PASS
"""


def validate_secure_directory(nodes=None):
    if nodes is None:
        nodes = discover_nodes()

    master = node_map(nodes)["master-01"]
    fqdn = master["fqdn"]

    script = f"""set -euo pipefail

INSTANCE=p11ldap
BASE="dc=lab,dc=example,dc=com"
PEOPLE="ou=People,$BASE"
GROUPS_DN="ou=Groups,$BASE"

SECRETS=/etc/p11-ldap-secrets.env
CA=/etc/security/tls/ca.crt
HOST={fqdn}

test -s "$SECRETS"
test "$(stat -c '%a' "$SECRETS")" = "600"
test "$(stat -c '%U:%G' "$SECRETS")" = "root:root"

. "$SECRETS"

echo "SERVICE=$(systemctl is-active dirsrv@$INSTANCE)"

ss -ltn | grep -q ':389 '
ss -ltn | grep -q ':636 '

echo "LDAP_PORT=PASS"
echo "LDAPS_PORT=PASS"

LDAPTLS_CACERT="$CA" \
ldapwhoami \
    -x \
    -H "ldaps://$HOST:636" \
    -D "uid=p11user,$PEOPLE" \
    -w "$P11_LDAP_USER_PASSWORD" \
    >/dev/null

echo "P11USER_BIND=PASS"

LDAPTLS_CACERT="$CA" \
ldapsearch \
    -x \
    -LLL \
    -H "ldaps://$HOST:636" \
    -D "uid=hs2bind,$PEOPLE" \
    -w "$P11_LDAP_BIND_PASSWORD" \
    -b "$PEOPLE" \
    "(uid=p11user)" \
    uid \
    | grep -q '^uid: p11user$'

echo "HS2BIND_USER_SEARCH=PASS"

LDAPTLS_CACERT="$CA" \
ldapsearch \
    -x \
    -LLL \
    -H "ldaps://$HOST:636" \
    -D "uid=hs2bind,$PEOPLE" \
    -w "$P11_LDAP_BIND_PASSWORD" \
    -b "$GROUPS_DN" \
    "(cn=data-users)" \
    cn member memberUid \
    | grep -q '^cn: data-users$'

echo "HS2BIND_GROUP_SEARCH=PASS"

if LDAPTLS_CACERT="$CA" \
   ldapwhoami \
      -x \
      -H "ldaps://$HOST:636" \
      -D "uid=p11user,$PEOPLE" \
      -w "definitely-wrong-p11-password" \
      >/dev/null 2>&1
then
    echo INVALID_PASSWORD_REJECTED=FAIL
    exit 1
fi

echo INVALID_PASSWORD_REJECTED=PASS

LDAPTLS_CACERT="$CA" \
ldapsearch \
    -x \
    -LLL \
    -H "ldaps://$HOST:636" \
    -D "cn=Directory Manager" \
    -w "$P11_LDAP_DM_PASSWORD" \
    -b "uid=p11user,$PEOPLE" \
    -s base \
    uidNumber gidNumber homeDirectory \
    | grep -q '^uidNumber: 1200$'

echo P11USER_POSIX=PASS

LDAPTLS_CACERT="$CA" \
ldapsearch \
    -x \
    -LLL \
    -H "ldaps://$HOST:636" \
    -D "cn=Directory Manager" \
    -w "$P11_LDAP_DM_PASSWORD" \
    -b "cn=data-users,$GROUPS_DN" \
    -s base \
    member memberUid \
    | grep -q '^memberUid: p11user$'

echo DATA_USERS_MEMBERSHIP=PASS

ldapsearch \
    -x \
    -LLL \
    -o ldif-wrap=no \
    -H ldap://127.0.0.1:389 \
    -D "cn=Directory Manager" \
    -w "$P11_LDAP_DM_PASSWORD" \
    -b "$BASE" \
    -s base \
    aci \
    | grep -Fq 'acl "HS2 LDAP bind search"'

echo HS2_READ_ACI=PASS

openssl s_client \
    -connect "$HOST:636" \
    -servername "$HOST" \
    -CAfile "$CA" \
    -verify_return_error \
    </dev/null 2>&1 \
    | grep -q 'Verify return code: 0 (ok)'

echo LDAPS_CA_VERIFY=PASS

dsctl "$INSTANCE" tls show-server-cert \
    | grep -Fq "CN=$HOST"

dsctl "$INSTANCE" tls show-server-cert \
    | grep -Fq "CN=P11 Lab Root CA"

echo LDAPS_SERVER_CERT=PASS

echo P11_SECURE_DIRECTORY_VALIDATION=PASS
"""

    cp = remote_root(
        master,
        script,
        capture=True,
    )

    text = (
        (cp.stdout or "")
        + "\n"
        + (cp.stderr or "")
    )

    required = [
        "SERVICE=active",
        "LDAP_PORT=PASS",
        "LDAPS_PORT=PASS",
        "P11USER_BIND=PASS",
        "HS2BIND_USER_SEARCH=PASS",
        "HS2BIND_GROUP_SEARCH=PASS",
        "INVALID_PASSWORD_REJECTED=PASS",
        "P11USER_POSIX=PASS",
        "DATA_USERS_MEMBERSHIP=PASS",
        "HS2_READ_ACI=PASS",
        "LDAPS_CA_VERIFY=PASS",
        "LDAPS_SERVER_CERT=PASS",
        "P11_SECURE_DIRECTORY_VALIDATION=PASS",
    ]

    for expected in required:
        if expected not in text:
            print(text)

            raise P11Error(
                "secure directory validation failed: "
                f"{expected}"
            )

    evidence = {
        "instance": "p11ldap",
        "host": fqdn,
        "ports": {
            "ldap_389": "PASS",
            "ldaps_636": "PASS",
        },
        "tls": {
            "p11_ca_verified": "PASS",
            "server_certificate": "PASS",
        },
        "directory": {
            "people_ou": "PASS",
            "groups_ou": "PASS",
            "p11user": "PASS",
            "hs2bind": "PASS",
            "data_users_group": "PASS",
            "hs2_read_aci": "PASS",
        },
        "authentication": {
            "p11user_valid_password": "PASS",
            "invalid_password_rejected": "PASS",
            "hs2bind_user_search": "PASS",
            "hs2bind_group_search": "PASS",
        },
        "secrets": {
            "file_permissions": "PASS",
            "password_values_redacted": True,
        },
        "overall": "PASS",
    }

    secure_dir = RESULTS_DIR / "secure"

    secure_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = secure_dir / "directory.json"

    path.write_text(
        json.dumps(evidence, indent=2)
        + "\n"
    )

    print(f"evidence: {path}")
    print(json.dumps(evidence, indent=2))

    return evidence


def secure_directory():
    nodes = discover_nodes()

    validate_secure_tls(nodes)

    master = node_map(nodes)["master-01"]

    print(
        "\n========== SECURE LDAP / LDAPS =========="
    )

    remote_root(
        master,
        secure_directory_master_script(
            master
        ),
    )

    validate_secure_directory(nodes)

    print("P11 SECURE DIRECTORY: PASS")

    return nodes


def secure_hive_master_script(master):
    script = r"""set -euo pipefail
umask 077

HOST=@@FQDN@@
REALM=LAB.EXAMPLE.COM

HADOOP_CONF=@@HADOOP_HOME@@/etc/hadoop
HIVE_CONF=@@HIVE_HOME@@/conf

LDAP_SECRETS=/etc/p11-ldap-secrets.env
CRED_ENV=/etc/p11-hs2-credstore.env
CRED_DIR=/etc/security/credentials
PROVIDER=jceks://file/etc/security/credentials/hive.jceks

TLS=/etc/security/tls

test -s "$LDAP_SECRETS"
test -s "$TLS/server.p12"
test -s "$TLS/storepass"
test -s "$TLS/ca.crt"

. "$LDAP_SECRETS"

test -n "$P11_LDAP_BIND_PASSWORD"

echo "===== HS2 CREDENTIAL STORE ====="

if [ ! -f "$CRED_ENV" ]; then
    touch "$CRED_ENV"
fi

chmod 0600 "$CRED_ENV"
chown root:root "$CRED_ENV"

if ! grep -q '^HADOOP_CREDSTORE_PASSWORD=' "$CRED_ENV"; then
    printf 'HADOOP_CREDSTORE_PASSWORD=%s\n' \
        "$(openssl rand -hex 24)" \
        >>"$CRED_ENV"
fi

. "$CRED_ENV"

test -n "$HADOOP_CREDSTORE_PASSWORD"

install -d \
    -o root \
    -g hadoop \
    -m 0750 \
    "$CRED_DIR"

rm -f \
    "$CRED_DIR/hive.jceks" \
    "$CRED_DIR/.hive.jceks.crc"

export HADOOP_CREDSTORE_PASSWORD

HADOOP_CONF_DIR="$HADOOP_CONF" \
@@HADOOP_HOME@@/bin/hadoop credential create \
    hive.server2.authentication.ldap.bindpw \
    -provider "$PROVIDER" \
    -value "$P11_LDAP_BIND_PASSWORD" \
    >/dev/null

chown root:hadoop "$CRED_DIR/hive.jceks"
chmod 0640 "$CRED_DIR/hive.jceks"

if [ -e "$CRED_DIR/.hive.jceks.crc" ]; then
    chown root:hadoop "$CRED_DIR/.hive.jceks.crc"
    chmod 0640 "$CRED_DIR/.hive.jceks.crc"
fi

HADOOP_CONF_DIR="$HADOOP_CONF" \
@@HADOOP_HOME@@/bin/hadoop credential list \
    -provider "$PROVIDER" \
    | grep -q '^hive.server2.authentication.ldap.bindpw$'

echo HIVE_LDAP_JCEKS=PASS

echo
echo "===== HIVE SITE SECURITY ====="

STOREPASS=$(cat "$TLS/storepass")
export STOREPASS HOST PROVIDER

python3 - <<'PY'
import os
import xml.etree.ElementTree as ET

path = "@@HIVE_HOME@@/conf/hive-site.xml"

tree = ET.parse(path)
root = tree.getroot()

def remove_property(name):
    for prop in list(root.findall("property")):
        if prop.findtext("name") == name:
            root.remove(prop)

def set_property(name, value):
    remove_property(name)

    prop = ET.SubElement(root, "property")

    ET.SubElement(
        prop,
        "name",
    ).text = name

    ET.SubElement(
        prop,
        "value",
    ).text = str(value)


host = os.environ["HOST"]
provider = os.environ["PROVIDER"]
storepass = os.environ["STOREPASS"]

props = {
    # Secured Hive Metastore
    "hive.metastore.sasl.enabled":
        "true",

    "hive.metastore.kerberos.principal":
        "hive/_HOST@LAB.EXAMPLE.COM",

    "hive.metastore.kerberos.keytab.file":
        "/etc/security/keytabs/hive.service.keytab",

    # HiveServer2 LDAP authentication
    "hive.server2.authentication":
        "LDAP",

    "hive.server2.authentication.ldap.url":
        f"ldaps://{host}:636",

    "hive.server2.authentication.ldap.baseDN":
        "dc=lab,dc=example,dc=com",

    "hive.server2.authentication.ldap.userDNPattern":
        "uid=%s,ou=People,dc=lab,dc=example,dc=com",

    "hive.server2.authentication.ldap.groupDNPattern":
        "cn=%s,ou=Groups,dc=lab,dc=example,dc=com",

    "hive.server2.authentication.ldap.groupFilter":
        "data-users",

    "hive.server2.authentication.ldap.groupMembershipKey":
        "member",

    "hive.server2.authentication.ldap.groupClassKey":
        "groupOfNames",

    "hive.server2.authentication.ldap.binddn":
        "uid=hs2bind,ou=People,dc=lab,dc=example,dc=com",

    # JCEKS provider supplies bindpw.
    "hadoop.security.credential.provider.path":
        provider,

    # Service identity used for secured Hadoop/HMS access.
    "hive.server2.authentication.kerberos.principal":
        "hive/_HOST@LAB.EXAMPLE.COM",

    "hive.server2.authentication.kerberos.keytab":
        "/etc/security/keytabs/hive.service.keytab",

    "hive.server2.enable.doAs":
        "false",

    # TLS
    "hive.server2.use.SSL":
        "true",

    "hive.server2.keystore.path":
        "/etc/security/tls/server.p12",

    "hive.server2.keystore.password":
        storepass,

    "hive.server2.keystore.type":
        "PKCS12",

    # Binary Thrift endpoint
    "hive.server2.transport.mode":
        "binary",

    "hive.server2.thrift.bind.host":
        host,

    "hive.server2.thrift.port":
        "10000",
}

# Never leave LDAP bind password in XML.
remove_property(
    "hive.server2.authentication.ldap.bindpw"
)

for name, value in props.items():
    set_property(
        name,
        value,
    )

tree.write(
    path,
    encoding="UTF-8",
    xml_declaration=True,
)

ET.parse(path)

print("HIVE_SITE_SECURITY_PATCH=PASS")
PY

chown root:hadoop "$HIVE_CONF/hive-site.xml"
chmod 0640 "$HIVE_CONF/hive-site.xml"

echo
echo "===== JAVA 17 LDAP TRUST ====="

. /etc/p11-platform.env

test -x "$JAVA_HOME/bin/keytool"

"$JAVA_HOME/bin/keytool" \
    -delete \
    -cacerts \
    -storepass changeit \
    -alias p11-lab-root-ca \
    >/dev/null 2>&1 || true

"$JAVA_HOME/bin/keytool" \
    -importcert \
    -noprompt \
    -cacerts \
    -storepass changeit \
    -alias p11-lab-root-ca \
    -file "$TLS/ca.crt" \
    >/dev/null

"$JAVA_HOME/bin/keytool" \
    -list \
    -cacerts \
    -storepass changeit \
    -alias p11-lab-root-ca \
    >/dev/null

echo JAVA17_P11_CA=PASS

echo
echo "===== HIVE SYSTEMD CREDENTIAL ENV ====="

for UNIT in \
    p11-hive-metastore \
    p11-hiveserver2
do
    DROPIN="/etc/systemd/system/${UNIT}.service.d"

    install -d \
        -o root \
        -g root \
        -m 0755 \
        "$DROPIN"

    cat >"$DROPIN/credential-store.conf" <<'EOF'
[Service]
EnvironmentFile=/etc/p11-hs2-credstore.env
EOF

    chmod 0644 \
        "$DROPIN/credential-store.conf"
done

systemctl daemon-reload

echo HMS_SYSTEMD_CREDENTIAL_ENV=PASS
echo HS2_SYSTEMD_CREDENTIAL_ENV=PASS

echo
echo "===== SANITY ====="

python3 - <<'PY'
import xml.etree.ElementTree as ET

path = "@@HIVE_HOME@@/conf/hive-site.xml"

root = ET.parse(path).getroot()

values = {
    prop.findtext("name"):
        prop.findtext("value")
    for prop in root.findall("property")
}

required = {
    "hive.metastore.sasl.enabled":
        "true",

    "hive.server2.authentication":
        "LDAP",

    "hive.server2.use.SSL":
        "true",

    "hive.server2.enable.doAs":
        "false",

    "hadoop.security.credential.provider.path":
        "jceks://file/etc/security/credentials/hive.jceks",
}

for key, expected in required.items():
    actual = values.get(key)

    if actual != expected:
        raise SystemExit(
            f"{key}: expected {expected}, got {actual}"
        )

if (
    "hive.server2.authentication.ldap.bindpw"
    in values
):
    raise SystemExit(
        "LDAP bind password property exists in hive-site.xml"
    )

print("HIVE_SECURITY_SANITY=PASS")
PY

echo P11_SECURE_HIVE_CONFIG=PASS
"""

    return (
        script
        .replace("@@FQDN@@", master["fqdn"])
        .replace("@@HADOOP_HOME@@", HADOOP_HOME)
        .replace("@@HIVE_HOME@@", HIVE_HOME)
    )


def _wait_tcp(master, port, timeout=240):
    deadline = time.time() + timeout

    while time.time() < deadline:
        cp = remote(
            master,
            f"ss -ltn | grep -q ':{port} '",
            check=False,
            capture=True,
        )

        if cp.returncode == 0:
            return

        time.sleep(4)

    raise P11Error(
        f"service did not listen on TCP/{port}"
    )


def validate_secure_hive(nodes=None):
    if nodes is None:
        nodes = discover_nodes()

    master = node_map(nodes)["master-01"]

    script = r"""set -euo pipefail

HOST=@@FQDN@@

HADOOP_CONF=@@HADOOP_HOME@@/etc/hadoop
HIVE_CONF=@@HIVE_HOME@@/conf

LDAP_SECRETS=/etc/p11-ldap-secrets.env
CRED_ENV=/etc/p11-hs2-credstore.env
PROVIDER=jceks://file/etc/security/credentials/hive.jceks

TLS=/etc/security/tls

. "$LDAP_SECRETS"
. "$CRED_ENV"
. /etc/p11-platform.env

echo "===== SERVICES ====="

test "$(systemctl is-active p11-hive-metastore)" = "active"
test "$(systemctl is-active p11-hiveserver2)" = "active"

ss -ltn | grep -q ':9083 '
ss -ltn | grep -q ':10000 '

echo HMS_SERVICE=PASS
echo HS2_SERVICE=PASS

echo
echo "===== HIVE CONFIG ====="

python3 - <<'PY'
import xml.etree.ElementTree as ET

path = "@@HIVE_HOME@@/conf/hive-site.xml"

root = ET.parse(path).getroot()

values = {
    prop.findtext("name"):
        prop.findtext("value")
    for prop in root.findall("property")
}

required = {
    "hive.metastore.sasl.enabled":
        "true",

    "hive.metastore.kerberos.principal":
        "hive/_HOST@LAB.EXAMPLE.COM",

    "hive.metastore.kerberos.keytab.file":
        "/etc/security/keytabs/hive.service.keytab",

    "hive.server2.authentication":
        "LDAP",

    "hive.server2.authentication.ldap.url":
        "ldaps://@@FQDN@@:636",

    "hive.server2.authentication.ldap.groupFilter":
        "data-users",

    "hive.server2.authentication.ldap.binddn":
        "uid=hs2bind,ou=People,dc=lab,dc=example,dc=com",

    "hive.server2.use.SSL":
        "true",

    "hive.server2.enable.doAs":
        "false",

    "hadoop.security.credential.provider.path":
        "jceks://file/etc/security/credentials/hive.jceks",
}

for key, expected in required.items():
    actual = values.get(key)

    if actual != expected:
        raise SystemExit(
            f"{key}: expected {expected}, got {actual}"
        )

if (
    "hive.server2.authentication.ldap.bindpw"
    in values
):
    raise SystemExit(
        "plaintext LDAP bindpw property detected"
    )

print("HIVE_SITE_VALIDATION=PASS")
PY

echo
echo "===== JCEKS ====="

test "$(stat -c '%a' "$CRED_ENV")" = "600"
test "$(stat -c '%U:%G' "$CRED_ENV")" = "root:root"

test "$(stat -c '%a' /etc/security/credentials/hive.jceks)" = "640"
test "$(stat -c '%U:%G' /etc/security/credentials/hive.jceks)" = "root:hadoop"

export HADOOP_CREDSTORE_PASSWORD

HADOOP_CONF_DIR="$HADOOP_CONF" \
@@HADOOP_HOME@@/bin/hadoop credential list \
    -provider "$PROVIDER" \
    | grep -q '^hive.server2.authentication.ldap.bindpw$'

echo JCEKS_BINDPW_ALIAS=PASS
echo JCEKS_PERMISSIONS=PASS

for UNIT in \
    p11-hive-metastore \
    p11-hiveserver2
do
    DROPIN="/etc/systemd/system/${UNIT}.service.d/credential-store.conf"

    test -f "$DROPIN"

    if grep -q \
        'HADOOP_CREDSTORE_PASSWORD=' \
        "$DROPIN"
    then
        echo SYSTEMD_INLINE_CREDSTORE_PASSWORD=FAIL
        exit 1
    fi

    grep -q \
        '^EnvironmentFile=/etc/p11-hs2-credstore.env$' \
        "$DROPIN"

    systemctl show \
        "${UNIT}.service" \
        -p EnvironmentFiles \
        | grep -q '/etc/p11-hs2-credstore.env'
done

echo HMS_SYSTEMD_CREDENTIAL_ENV=PASS
echo HS2_SYSTEMD_CREDENTIAL_ENV=PASS
echo SYSTEMD_SECRET_ARG=PASS

echo
echo "===== HS2 TLS ====="

timeout 15 openssl s_client \
    -connect "$HOST:10000" \
    -servername "$HOST" \
    -CAfile "$TLS/ca.crt" \
    -verify_return_error \
    </dev/null 2>&1 \
    | grep -q 'Verify return code: 0 (ok)'

echo HS2_TLS=PASS

echo
echo "===== VALID LDAP + SQL ====="

INPUT=/tmp/p11-secure-hive-input.txt
SQL=/tmp/p11-secure-hive-validation.sql
OUT=/tmp/p11-secure-hive-validation.out

printf '1 secure-hive\n' >"$INPUT"

chown hadoop:hadoop "$INPUT"
chmod 0644 "$INPUT"

cat >"$SQL" <<'EOF'
CREATE DATABASE IF NOT EXISTS p11_secure_validation;
DROP TABLE IF EXISTS p11_secure_validation.secure_table;
CREATE TABLE p11_secure_validation.secure_table (
    id INT,
    value STRING
)
ROW FORMAT DELIMITED
FIELDS TERMINATED BY ' ';

LOAD DATA LOCAL INPATH
'/tmp/p11-secure-hive-input.txt'
OVERWRITE INTO TABLE
p11_secure_validation.secure_table;

SELECT id, value
FROM p11_secure_validation.secure_table;
EOF

chown hadoop:hadoop "$SQL"
chmod 0600 "$SQL"

set +e

sudo -u hadoop env \
    HADOOP_CONF_DIR="$HADOOP_CONF" \
    @@HIVE_HOME@@/bin/beeline \
    -u "jdbc:hive2://$HOST:10000/default;ssl=true" \
    -n p11user \
    -p "$P11_LDAP_USER_PASSWORD" \
    -f "$SQL" \
    >"$OUT" 2>&1

VALID_RC=$?

set -e

if [ "$VALID_RC" -ne 0 ]; then
    tail -120 "$OUT"
    echo VALID_LDAP_QUERY=FAIL
    exit 1
fi

grep -Eq \
    '\|[[:space:]]*1[[:space:]]*\|[[:space:]]*secure-hive[[:space:]]*\|' \
    "$OUT"

echo VALID_LDAP_QUERY=PASS
echo HIVE_SQL_READ_WRITE=PASS

echo
echo "===== SECURE HDFS BACKING STORAGE ====="

export KRB5CCNAME=/tmp/p11-hive-hdfs-proof.ccache

kdestroy 2>/dev/null || true

kinit \
    -kt /etc/security/keytabs/hdfs.service.keytab \
    hdfs/$HOST@LAB.EXAMPLE.COM

@@HADOOP_HOME@@/bin/hdfs dfs \
    -cat \
    '/user/hive/warehouse/p11_secure_validation.db/secure_table/*' \
    | grep -q '1 secure-hive'

kdestroy

echo HIVE_SECURE_HDFS_STORAGE=PASS

echo
echo "===== INVALID LDAP PASSWORD ====="

BAD=/tmp/p11-secure-hive-invalid.out

set +e

sudo -u hadoop env \
    HADOOP_CONF_DIR="$HADOOP_CONF" \
    @@HIVE_HOME@@/bin/beeline \
    -u "jdbc:hive2://$HOST:10000/default;ssl=true" \
    -n p11user \
    -p "definitely-wrong-p11-password" \
    -e "SHOW DATABASES;" \
    >"$BAD" 2>&1

BAD_RC=$?

set -e

if [ "$BAD_RC" -eq 0 ]; then
    echo INVALID_LDAP_PASSWORD_REJECTED=FAIL
    exit 1
fi

echo INVALID_LDAP_PASSWORD_REJECTED=PASS

echo
echo "===== CLEANUP ====="

sudo -u hadoop env \
    HADOOP_CONF_DIR="$HADOOP_CONF" \
    @@HIVE_HOME@@/bin/beeline \
    -u "jdbc:hive2://$HOST:10000/default;ssl=true" \
    -n p11user \
    -p "$P11_LDAP_USER_PASSWORD" \
    -e "DROP DATABASE IF EXISTS p11_secure_validation CASCADE;" \
    >/dev/null 2>&1

rm -f \
    "$INPUT" \
    "$SQL" \
    "$OUT" \
    "$BAD"

echo HIVE_SECURE_END_TO_END=PASS
"""

    script = (
        script
        .replace("@@FQDN@@", master["fqdn"])
        .replace("@@HADOOP_HOME@@", HADOOP_HOME)
        .replace("@@HIVE_HOME@@", HIVE_HOME)
    )

    cp = remote_root(
        master,
        script,
        capture=True,
    )

    text = (
        (cp.stdout or "")
        + "\n"
        + (cp.stderr or "")
    )

    required = [
        "HMS_SERVICE=PASS",
        "HS2_SERVICE=PASS",
        "HIVE_SITE_VALIDATION=PASS",
        "JCEKS_BINDPW_ALIAS=PASS",
        "JCEKS_PERMISSIONS=PASS",
        "HMS_SYSTEMD_CREDENTIAL_ENV=PASS",
        "HS2_SYSTEMD_CREDENTIAL_ENV=PASS",
        "SYSTEMD_SECRET_ARG=PASS",
        "HS2_TLS=PASS",
        "VALID_LDAP_QUERY=PASS",
        "HIVE_SQL_READ_WRITE=PASS",
        "HIVE_SECURE_HDFS_STORAGE=PASS",
        "INVALID_LDAP_PASSWORD_REJECTED=PASS",
        "HIVE_SECURE_END_TO_END=PASS",
    ]

    for expected in required:
        if expected not in text:
            print(text)

            raise P11Error(
                "secure Hive validation failed: "
                f"{expected}"
            )

    evidence = {
        "metastore": {
            "service": "PASS",
            "kerberos_sasl": "PASS",
        },
        "hiveserver2": {
            "service": "PASS",
            "tls": "PASS",
            "ldap_authentication": "PASS",
            "group_filter": "PASS",
            "do_as_false": "PASS",
        },
        "credential_store": {
            "jceks_bind_password": "PASS",
            "permissions": "PASS",
            "systemd_environment_file": "PASS",
            "plaintext_bindpw_in_hive_site": False,
        },
        "functional": {
            "valid_ldap_sql": "PASS",
            "sql_read_write": "PASS",
            "secure_hdfs_storage": "PASS",
            "invalid_ldap_password_rejected": "PASS",
        },
        "overall": "PASS",
    }

    secure_dir = RESULTS_DIR / "secure"

    secure_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = secure_dir / "hive-security.json"

    path.write_text(
        json.dumps(evidence, indent=2)
        + "\n"
    )

    print(f"evidence: {path}")
    print(json.dumps(evidence, indent=2))

    return evidence


def secure_hive():
    nodes = discover_nodes()

    validate_secure_hadoop(nodes)
    validate_secure_directory(nodes)

    master = node_map(nodes)["master-01"]

    print(
        "\n========== STOP HIVE SERVICES =========="
    )

    remote_root(
        master,
        """systemctl stop \
p11-hiveserver2 \
p11-hive-metastore || true""",
    )

    print(
        "\n========== SECURE HIVE CONFIGURATION =========="
    )

    remote_root(
        master,
        secure_hive_master_script(
            master
        ),
    )

    print(
        "\n========== START SECURE METASTORE =========="
    )

    remote_root(
        master,
        "systemctl restart p11-hive-metastore",
    )

    try:
        _wait_tcp(
            master,
            9083,
            timeout=180,
        )
    except P11Error:
        remote(
            master,
            """sudo journalctl \
-u p11-hive-metastore \
-n 180 \
--no-pager""",
            check=False,
        )

        raise

    print(
        "\n========== START SECURE HIVESERVER2 =========="
    )

    remote_root(
        master,
        "systemctl restart p11-hiveserver2",
    )

    try:
        _wait_tcp(
            master,
            10000,
            timeout=240,
        )
    except P11Error:
        remote(
            master,
            """sudo journalctl \
-u p11-hiveserver2 \
-n 220 \
--no-pager""",
            check=False,
        )

        raise

    validate_secure_hive(nodes)

    print("P11 SECURE HIVE: PASS")

    return nodes


def validate_secure_spark(nodes=None):
    if nodes is None:
        nodes = discover_nodes()

    master = node_map(nodes)["master-01"]

    script = r"""set -euo pipefail

cd /tmp

sudo -u p11admin bash <<'USER'
set -euo pipefail

cd /tmp

export HADOOP_CONF_DIR=@@HADOOP_HOME@@/etc/hadoop
export KRB5CCNAME=/tmp/p11-secure-spark-validation.ccache

kdestroy 2>/dev/null || true

kinit \
    -kt /home/p11admin/.keytabs/p11admin.keytab \
    p11admin@LAB.EXAMPLE.COM

JAR=$(
    find @@SPARK_HOME@@/examples/jars \
        -maxdepth 1 \
        -type f \
        -name 'spark-examples_*.jar' \
        | head -1
)

test -n "$JAR"
test -f "$JAR"

OUT=/tmp/p11-secure-spark-validation.out
STATUS=/tmp/p11-secure-spark-validation.status

rm -f "$OUT" "$STATUS"

set +e

@@SPARK_HOME@@/bin/spark-submit \
    --master yarn \
    --deploy-mode client \
    --class org.apache.spark.examples.SparkPi \
    "$JAR" \
    20 \
    >"$OUT" 2>&1

SPARK_RC=$?

set -e

if [ "$SPARK_RC" -ne 0 ]; then
    cat "$OUT"
    echo SECURE_SPARK_SUBMIT=FAIL
    exit 1
fi

grep -E \
    'Pi is roughly|Pi is approximately' \
    "$OUT"

APP_ID=$(
    grep -Eo \
        'application_[0-9]+_[0-9]+' \
        "$OUT" \
        | tail -1
)

if [ -z "$APP_ID" ]; then
    cat "$OUT"
    echo SECURE_SPARK_APPLICATION_ID=FAIL
    exit 1
fi

@@HADOOP_HOME@@/bin/yarn \
    application \
    -status "$APP_ID" \
    >"$STATUS" 2>&1

cat "$STATUS"

grep -Eq \
    'State[[:space:]]*:[[:space:]]*FINISHED' \
    "$STATUS"

grep -Eq \
    'Final-State[[:space:]]*:[[:space:]]*SUCCEEDED' \
    "$STATUS"

echo "SPARK_APPLICATION_ID=$APP_ID"
echo SECURE_SPARK_YARN=PASS

rm -f "$OUT" "$STATUS"

kdestroy 2>/dev/null || true
USER
"""

    script = (
        script
        .replace("@@HADOOP_HOME@@", HADOOP_HOME)
        .replace("@@SPARK_HOME@@", SPARK_HOME)
    )

    cp = remote_root(
        master,
        script,
        capture=True,
    )

    text = (
        (cp.stdout or "")
        + "\n"
        + (cp.stderr or "")
    )

    if "SECURE_SPARK_YARN=PASS" not in text:
        print(text)

        raise P11Error(
            "secure Spark-on-YARN validation failed"
        )

    import re

    match = re.search(
        r"SPARK_APPLICATION_ID=(application_\d+_\d+)",
        text,
    )

    if not match:
        print(text)

        raise P11Error(
            "secure Spark validation did not return application ID"
        )

    application_id = match.group(1)

    evidence = {
        "user": "p11admin",
        "kerberos": "PASS",
        "master": "yarn",
        "application_id": application_id,
        "state": "FINISHED",
        "final_state": "SUCCEEDED",
        "overall": "PASS",
    }

    secure_dir = RESULTS_DIR / "secure"

    secure_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = secure_dir / "spark-yarn.json"

    path.write_text(
        json.dumps(evidence, indent=2)
        + "\n"
    )

    print(
        f"SECURE_SPARK_YARN=PASS "
        f"application_id={application_id}"
    )

    print(f"evidence: {path}")

    return evidence


def secure_validate():
    nodes = discover_nodes()

    print(
        "\n========== SECURE IDENTITY VALIDATION =========="
    )

    validate_secure_identity(nodes)

    print(
        "\n========== SECURE KERBEROS VALIDATION =========="
    )

    validate_secure_kerberos(nodes)

    print(
        "\n========== SECURE TLS VALIDATION =========="
    )

    validate_secure_tls(nodes)

    print(
        "\n========== SECURE LCE VALIDATION =========="
    )

    validate_secure_lce(nodes)

    print(
        "\n========== SECURE HADOOP VALIDATION =========="
    )

    validate_secure_hadoop(nodes)

    print(
        "\n========== SECURE DIRECTORY VALIDATION =========="
    )

    validate_secure_directory(nodes)

    print(
        "\n========== SECURE HIVE VALIDATION =========="
    )

    validate_secure_hive(nodes)

    print(
        "\n========== SECURE SPARK-ON-YARN VALIDATION =========="
    )

    spark = validate_secure_spark(nodes)

    evidence = {
        "identity": "PASS",
        "kerberos": "PASS",
        "tls": "PASS",
        "linux_container_executor": "PASS",
        "mapreduce_as_p11admin": "PASS",
        "hdfs": "PASS",
        "yarn": "PASS",
        "directory_ldap_ldaps": "PASS",
        "hive_metastore_kerberos": "PASS",
        "hiveserver2_tls_ldap": "PASS",
        "hive_secure_sql": "PASS",
        "spark_on_yarn": {
            "status": "PASS",
            "user": "p11admin",
            "application_id": spark["application_id"],
            "final_state": "SUCCEEDED",
        },
        "overall": "PASS",
    }

    secure_dir = RESULTS_DIR / "secure"

    secure_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = secure_dir / "secure-validation.json"

    path.write_text(
        json.dumps(evidence, indent=2)
        + "\n"
    )

    print(
        "\n========== PROJECT 11 SECURE ACCEPTANCE =========="
    )

    print(json.dumps(evidence, indent=2))

    print(f"evidence: {path}")

    print("P11 SECURE VALIDATION: PASS")

    return evidence


def secure_full():
    """
    Build and validate the complete secure profile.

    The secure pipeline first establishes and validates the base platform,
    then applies and validates each security layer in dependency order.
    """

    if not SECURE_AUTOMATION_READY:
        raise P11Error(
            "secure full is safety-gated"
        )

    secure_dir = RESULTS_DIR / "secure"
    secure_dir.mkdir(parents=True, exist_ok=True)

    stages = []
    overall_start = time.time()

    pipeline = [
        ("prepare", prepare),
        ("install", install),
        ("configure-base", configure),
        ("start-base", start),
        ("validate-base", validate),
        ("secure-identity", secure_identity),
        ("secure-lce", secure_lce),
        ("secure-kerberos", secure_kerberos),
        ("secure-tls", secure_tls),
        ("secure-hadoop", secure_hadoop),
        ("secure-directory", secure_directory),
        ("secure-hive", secure_hive),
        ("secure-validate", secure_validate),
    ]

    for name, fn in pipeline:
        stage_start = time.time()

        print(
            f"\n========== SECURE STAGE: "
            f"{name.upper()} =========="
        )

        fn()

        elapsed = round(time.time() - stage_start, 3)

        stages.append(
            {
                "stage": name,
                "elapsed_seconds": elapsed,
            }
        )

        print(
            f"SECURE STAGE {name}: "
            f"{elapsed:.3f}s"
        )

    total = round(time.time() - overall_start, 3)

    metrics = {
        "profile": "secure",
        "stack": {
            "hadoop": HADOOP_VERSION,
            "hive": HIVE_VERSION,
            "spark": SPARK_VERSION,
            "java": 17,
        },
        "security": {
            "linux_container_executor": True,
            "kerberos": True,
            "tls": True,
            "ldap_ldaps": True,
        },
        "stages": stages,
        "total_elapsed_seconds": total,
    }

    metrics_path = secure_dir / "secure-provisioning-metrics.json"
    metrics_path.write_text(
        json.dumps(metrics, indent=2) + "\n"
    )

    print(f"evidence: {metrics_path}")
    print(
        f"\nP11 SECURE DISTRIBUTED HADOOP LAB: "
        f"PASS ({total:.3f}s)"
    )

    return metrics



def xml(props):
    body = ["<?xml version=\"1.0\"?>", "<configuration>"]
    for name, value in props:
        body.extend(
            [
                "  <property>",
                f"    <name>{name}</name>",
                f"    <value>{value}</value>",
                "  </property>",
            ]
        )
    body.append("</configuration>")
    return "\n".join(body) + "\n"


def config_files(nodes):
    master = node_map(nodes)["master-01"]
    master_fqdn = master["fqdn"]

    core = xml(
        [
            ("fs.defaultFS", f"hdfs://{master_fqdn}:8020"),
            ("hadoop.tmp.dir", "/data/hadoop/tmp"),
        ]
    )
    hdfs = xml(
        [
            ("dfs.replication", "2"),
            ("dfs.namenode.name.dir", "file:///data/hadoop/hdfs/namenode"),
            ("dfs.datanode.data.dir", "file:///data/hadoop/hdfs/datanode"),
            ("dfs.permissions.superusergroup", "hadoop"),
            ("dfs.namenode.http-address", f"{master_fqdn}:9870"),
        ]
    )
    yarn = xml(
        [
            ("yarn.resourcemanager.hostname", master_fqdn),
            ("yarn.nodemanager.aux-services", "mapreduce_shuffle"),
            ("yarn.nodemanager.resource.memory-mb", "12288"),
            ("yarn.nodemanager.resource.cpu-vcores", "3"),
            ("yarn.scheduler.minimum-allocation-mb", "512"),
            ("yarn.scheduler.maximum-allocation-mb", "4096"),
            ("yarn.log-aggregation-enable", "true"),
            ("yarn.nodemanager.local-dirs", "/data/hadoop/yarn/local"),
            ("yarn.nodemanager.log-dirs", "/data/hadoop/yarn/log"),
            ("yarn.resourcemanager.webapp.address", f"{master_fqdn}:8088"),
        ]
    )
    mapred = xml(
        [
            ("mapreduce.framework.name", "yarn"),
            ("mapreduce.map.memory.mb", "1024"),
            ("mapreduce.reduce.memory.mb", "1024"),
            ("mapreduce.jobhistory.address", f"{master_fqdn}:10020"),
            ("mapreduce.jobhistory.webapp.address", f"{master_fqdn}:19888"),
        ]
    )
    workers = "\n".join(n["fqdn"] for n in nodes if n["name"].startswith("worker-")) + "\n"
    return core, hdfs, yarn, mapred, workers


def systemd_unit(description, user, execstart, envfile="/etc/p11-platform.env"):
    return f'''[Unit]
Description={description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group=hadoop
EnvironmentFile={envfile}
ExecStart={execstart}
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
'''


def configure_node_script(node, nodes):
    core, hdfs, yarn, mapred, workers = config_files(nodes)
    is_master = node["name"] == "master-01"

    if is_master:
        service_units = (
            "cat >/etc/systemd/system/p11-namenode.service <<'EOF'\n"
            + systemd_unit(
                "Project 11 HDFS NameNode",
                "hadoop",
                f"{HADOOP_HOME}/bin/hdfs namenode",
            )
            + "EOF\n"
            + "cat >/etc/systemd/system/p11-resourcemanager.service <<'EOF'\n"
            + systemd_unit(
                "Project 11 YARN ResourceManager",
                "hadoop",
                f"{HADOOP_HOME}/bin/yarn resourcemanager",
            )
            + "EOF\n"
            + "cat >/etc/systemd/system/p11-historyserver.service <<'EOF'\n"
            + systemd_unit(
                "Project 11 MapReduce JobHistoryServer",
                "hadoop",
                f"{HADOOP_HOME}/bin/mapred historyserver",
            )
            + "EOF\n"
        )
    else:
        service_units = (
            "cat >/etc/systemd/system/p11-datanode.service <<'EOF'\n"
            + systemd_unit(
                "Project 11 HDFS DataNode",
                "hadoop",
                f"{HADOOP_HOME}/bin/hdfs datanode",
            )
            + "EOF\n"
            + "cat >/etc/systemd/system/p11-nodemanager.service <<'EOF'\n"
            + systemd_unit(
                "Project 11 YARN NodeManager",
                "hadoop",
                f"{HADOOP_HOME}/bin/yarn nodemanager",
            )
            + "EOF\n"
        )

    return f'''set -euo pipefail

JAVA_BIN="$(rpm -ql java-17-openjdk-headless | grep -E '/bin/java$' | head -1)"
JAVA_HOME="$(dirname "$(dirname "$JAVA_BIN")")"

mkdir -p \
  /data/hadoop/tmp \
  /data/hadoop/hdfs/namenode \
  /data/hadoop/hdfs/datanode \
  /data/hadoop/yarn/local \
  /data/hadoop/yarn/log \
  /var/log/p11-hadoop
chown -R hadoop:hadoop /data/hadoop /var/log/p11-hadoop {OPT_ROOT}

cat >/etc/p11-platform.env <<EOF
JAVA_HOME=$JAVA_HOME
HADOOP_HOME={HADOOP_HOME}
HADOOP_CONF_DIR={HADOOP_HOME}/etc/hadoop
HADOOP_LOG_DIR=/var/log/p11-hadoop
YARN_LOG_DIR=/var/log/p11-hadoop
HIVE_HOME={HIVE_HOME}
SPARK_HOME={SPARK_HOME}
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:{HADOOP_HOME}/bin:{HADOOP_HOME}/sbin:{HIVE_HOME}/bin:{SPARK_HOME}/bin
EOF
chmod 0644 /etc/p11-platform.env

cat >{HADOOP_HOME}/etc/hadoop/core-site.xml <<'EOF'
{core}EOF
cat >{HADOOP_HOME}/etc/hadoop/hdfs-site.xml <<'EOF'
{hdfs}EOF
cat >{HADOOP_HOME}/etc/hadoop/yarn-site.xml <<'EOF'
{yarn}EOF
cat >{HADOOP_HOME}/etc/hadoop/mapred-site.xml <<'EOF'
{mapred}EOF
python3 - <<'P11PY'
from pathlib import Path
p = Path("{HADOOP_HOME}/etc/hadoop/mapred-site.xml")
s = p.read_text()
if "yarn.app.mapreduce.am.env" not in s:
    props = """  <property>
    <name>yarn.app.mapreduce.am.env</name>
    <value>HADOOP_MAPRED_HOME={HADOOP_HOME}</value>
  </property>
  <property>
    <name>mapreduce.map.env</name>
    <value>HADOOP_MAPRED_HOME={HADOOP_HOME}</value>
  </property>
  <property>
    <name>mapreduce.reduce.env</name>
    <value>HADOOP_MAPRED_HOME={HADOOP_HOME}</value>
  </property>
"""
    s = s.replace("</configuration>", props + "</configuration>")
    p.write_text(s)
P11PY
cat >{HADOOP_HOME}/etc/hadoop/workers <<'EOF'
{workers}EOF

cat >>{HADOOP_HOME}/etc/hadoop/hadoop-env.sh <<EOF
export JAVA_HOME=$JAVA_HOME
export HADOOP_HOME={HADOOP_HOME}
export HADOOP_CONF_DIR={HADOOP_HOME}/etc/hadoop
EOF
cat >>{HADOOP_HOME}/etc/hadoop/yarn-env.sh <<EOF
export JAVA_HOME=$JAVA_HOME
export YARN_RESOURCEMANAGER_OPTS="${{YARN_RESOURCEMANAGER_OPTS:-}} --add-opens=java.base/java.lang=ALL-UNNAMED"
export YARN_NODEMANAGER_OPTS="${{YARN_NODEMANAGER_OPTS:-}} --add-opens=java.base/java.lang=ALL-UNNAMED"
EOF
cat >>{HADOOP_HOME}/etc/hadoop/mapred-env.sh <<EOF
export JAVA_HOME=$JAVA_HOME
EOF

chown -R hadoop:hadoop {HADOOP_HOME}/etc/hadoop

{service_units}

systemctl daemon-reload
echo P11_HADOOP_CONFIGURED
'''


def configure_hive_master_script(master):
    master_fqdn = master["fqdn"]
    hive_site = xml(
        [
            ("javax.jdo.option.ConnectionURL", "jdbc:postgresql://127.0.0.1:5432/metastore"),
            ("javax.jdo.option.ConnectionDriverName", "org.postgresql.Driver"),
            ("javax.jdo.option.ConnectionUserName", "hive"),
            ("javax.jdo.option.ConnectionPassword", "__P11_HIVE_DB_PASSWORD__"),
            ("hive.metastore.uris", f"thrift://{master_fqdn}:9083"),
            ("hive.server2.thrift.bind.host", master_fqdn),
            ("hive.server2.thrift.port", "10000"),
            ("hive.execution.engine", "mr"),
            ("hive.metastore.warehouse.dir", "/user/hive/warehouse"),
            ("hive.server2.enable.doAs", "false"),
            ("hive.metastore.event.db.notification.api.auth", "false"),
            ("hive.server2.max.start.attempts", "1"),
        ]
    )
    metastore_unit = systemd_unit(
        "Project 11 Hive Metastore", "hadoop", f"{HIVE_HOME}/bin/hive --service metastore"
    )
    hs2_unit = systemd_unit(
        "Project 11 HiveServer2", "hadoop", f"{HIVE_HOME}/bin/hiveserver2"
    )

    return f'''set -euo pipefail

dnf install -y postgresql-server postgresql postgresql-jdbc openssl

if [ ! -s /etc/p11-hive-db.env ]; then
  umask 077
  printf 'HIVE_DB_PASSWORD=%s\\n' "$(openssl rand -hex 24)" >/etc/p11-hive-db.env
fi
. /etc/p11-hive-db.env

if [ ! -f /var/lib/pgsql/data/PG_VERSION ]; then
  postgresql-setup --initdb
fi

HBA=/var/lib/pgsql/data/pg_hba.conf
python3 - <<'P11PG'
from pathlib import Path
p = Path("/var/lib/pgsql/data/pg_hba.conf")
lines = p.read_text().splitlines()
out = []
found = False
for line in lines:
    fields = line.split()
    if len(fields) >= 5 and fields[:4] == ["host", "all", "all", "127.0.0.1/32"]:
        if fields[4] in ("ident", "peer"):
            fields[4] = "md5"
            line = "\t".join(fields)
        if fields[4] in ("md5", "scram-sha-256"):
            found = True
    out.append(line)
if not found:
    out.insert(0, "host all all 127.0.0.1/32 md5")
p.write_text(chr(10).join(out) + chr(10))
P11PG
systemctl enable --now postgresql

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='hive'" | grep -q 1; then
  sudo -u postgres psql -c "CREATE ROLE hive LOGIN PASSWORD '$HIVE_DB_PASSWORD';"
else
  sudo -u postgres psql -c "ALTER ROLE hive PASSWORD '$HIVE_DB_PASSWORD';"
fi
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='metastore'" | grep -q 1; then
  sudo -u postgres createdb -O hive metastore
fi
systemctl restart postgresql

JDBC_JAR="$(rpm -ql postgresql-jdbc | grep -E '/postgresql[^/]*\\.jar$' | head -1)"
test -n "$JDBC_JAR"
ln -sfn "$JDBC_JAR" {HIVE_HOME}/lib/postgresql-jdbc.jar

cat >{HIVE_HOME}/conf/hive-site.xml <<'EOF'
{hive_site}EOF
sed -i "s/__P11_HIVE_DB_PASSWORD__/$HIVE_DB_PASSWORD/" {HIVE_HOME}/conf/hive-site.xml
chmod 0755 {HIVE_HOME} {HIVE_HOME}/conf
chown root:hadoop {HIVE_HOME}/conf/hive-site.xml
chmod 0640 {HIVE_HOME}/conf/hive-site.xml


cat >{HIVE_HOME}/conf/hive-env.sh <<EOF
export JAVA_HOME=\\$(dirname \\$(dirname \\$(rpm -ql java-17-openjdk-headless | grep -E "/bin/java$" | head -1)))
export HADOOP_HOME={HADOOP_HOME}
export HADOOP_CONF_DIR={HADOOP_HOME}/etc/hadoop
export HIVE_HOME={HIVE_HOME}
EOF
chmod 0755 {HIVE_HOME}/conf/hive-env.sh
chown -R hadoop:hadoop {HIVE_HOME}
chown root:hadoop {HIVE_HOME}/conf/hive-site.xml
chmod 0640 {HIVE_HOME}/conf/hive-site.xml

if ! sudo -u hadoop bash -lc '. /etc/p11-platform.env; {HIVE_HOME}/bin/schematool -dbType postgres -info' >/tmp/p11-schematool-info.log 2>&1; then
  sudo -u hadoop bash -lc '. /etc/p11-platform.env; {HIVE_HOME}/bin/schematool -dbType postgres -initSchema'
fi

cat >/etc/systemd/system/p11-hive-metastore.service <<'EOF'
{metastore_unit}EOF
cat >/etc/systemd/system/p11-hiveserver2.service <<'EOF'
{hs2_unit}EOF

systemctl daemon-reload
echo P11_HIVE_CONFIGURED
'''


def configure_spark_master_script():
    return f'''set -euo pipefail
cat >{SPARK_HOME}/conf/spark-env.sh <<'EOF'
export JAVA_HOME=$(dirname $(dirname $(rpm -ql java-17-openjdk-headless | grep -E "/bin/java$" | head -1)))
export HADOOP_CONF_DIR={HADOOP_HOME}/etc/hadoop
export YARN_CONF_DIR={HADOOP_HOME}/etc/hadoop
EOF
cat >{SPARK_HOME}/conf/spark-defaults.conf <<'EOF'
spark.master yarn
spark.submit.deployMode client
spark.eventLog.enabled true
spark.eventLog.dir hdfs:///spark-history
EOF
chmod 0755 {SPARK_HOME}/conf/spark-env.sh
chown -R hadoop:hadoop {SPARK_HOME}
echo P11_SPARK_CONFIGURED
'''


def configure():
    nodes = discover_nodes()
    parallel(nodes, lambda n: remote_root(n, configure_node_script(n, nodes)), "CONFIGURE HADOOP")
    master = node_map(nodes)["master-01"]
    remote_root(master, configure_hive_master_script(master))
    remote_root(master, configure_spark_master_script())
    return nodes


def start():
    nodes = discover_nodes()
    by = node_map(nodes)
    master = by["master-01"]
    workers = [by["worker-01"], by["worker-02"]]

    # Format NameNode only once.
    remote_root(
        master,
        f'''set -euo pipefail
if [ ! -d /data/hadoop/hdfs/namenode/current ]; then
  sudo -u hadoop bash -lc '. /etc/p11-platform.env; {HADOOP_HOME}/bin/hdfs namenode -format -force -nonInteractive -clusterid P11'
fi
systemctl enable --now p11-namenode
''',
    )

    for w in workers:
        remote_root(w, "systemctl enable --now p11-datanode")

    # Wait for HDFS before creating shared directories.
    deadline = time.time() + 240
    while time.time() < deadline:
        cp = remote(
            master,
            f"sudo -u hadoop bash -lc '. /etc/p11-platform.env; {HADOOP_HOME}/bin/hdfs dfsadmin -report'",
            check=False,
            capture=True,
        )
        if cp.returncode == 0 and "Live datanodes (2)" in cp.stdout:
            break
        time.sleep(5)
    else:
        raise P11Error("HDFS did not report 2 live DataNodes")

    remote(
        master,
        f'''sudo -u hadoop bash -lc '. /etc/p11-platform.env; \
{HADOOP_HOME}/bin/hdfs dfs -mkdir -p /tmp /user/hive/warehouse /spark-history /p11; \
{HADOOP_HOME}/bin/hdfs dfs -chmod 1777 /tmp; \
{HADOOP_HOME}/bin/hdfs dfs -chmod -R 1777 /user/hive/warehouse; \
{HADOOP_HOME}/bin/hdfs dfs -chmod 1777 /spark-history' ''',
    )

    remote_root(master, "systemctl enable --now p11-resourcemanager p11-historyserver")
    for w in workers:
        remote_root(w, "systemctl enable --now p11-nodemanager")

    remote_root(master, "systemctl enable --now p11-hive-metastore")
    deadline = time.time() + 180
    while time.time() < deadline:
        cp = remote(master, "ss -ltn | grep -q ':9083 '", check=False, capture=True)
        if cp.returncode == 0:
            break
        time.sleep(5)
    else:
        remote(master, "sudo journalctl -u p11-hive-metastore -n 120 --no-pager", check=False)
        raise P11Error("Hive Metastore did not listen on 9083")

    remote_root(master, "systemctl enable --now p11-hiveserver2")
    deadline = time.time() + 240
    while time.time() < deadline:
        cp = remote(master, "ss -ltn | grep -q ':10000 '", check=False, capture=True)
        if cp.returncode == 0:
            break
        time.sleep(5)
    else:
        remote(master, "sudo journalctl -u p11-hiveserver2 -n 160 --no-pager", check=False)
        raise P11Error("HiveServer2 did not listen on 10000")

    print("Distributed services started.")
    return nodes


def status():
    nodes = discover_nodes()
    by = node_map(nodes)
    master = by["master-01"]
    workers = [by["worker-01"], by["worker-02"]]

    result = {}
    result["master-01"] = remote(
        master,
        "systemctl is-active p11-namenode p11-resourcemanager p11-historyserver "
        "p11-hive-metastore p11-hiveserver2 || true",
        capture=True,
    ).stdout
    for w in workers:
        result[w["name"]] = remote(
            w,
            "systemctl is-active p11-datanode p11-nodemanager || true",
            capture=True,
        ).stdout
    write_json("service-status.json", result)
    print(json.dumps(result, indent=2))
    return result


def validate():
    nodes = discover_nodes()
    by = node_map(nodes)
    master = by["master-01"]
    evidence = {"versions": {}, "tests": {}}

    versions_cmd = f'''sudo -u hadoop bash -lc '. /etc/p11-platform.env; \
{HADOOP_HOME}/bin/hadoop version | head -1; \
{HIVE_HOME}/bin/hive --version | head -1; \
{SPARK_HOME}/bin/spark-submit --version 2>&1 | grep -m1 -E "version [0-9]" || true' '''
    evidence["versions"]["master"] = remote(master, versions_cmd, capture=True).stdout

    hdfs = remote(
        master,
        f'''sudo -u hadoop bash -lc '. /etc/p11-platform.env; \
{HADOOP_HOME}/bin/hdfs dfsadmin -report; \
printf "project11\\ndistributed-hadoop\\n" >/tmp/p11-hdfs-input.txt; \
{HADOOP_HOME}/bin/hdfs dfs -mkdir -p /p11/input; \
{HADOOP_HOME}/bin/hdfs dfs -put -f /tmp/p11-hdfs-input.txt /p11/input/input.txt; \
{HADOOP_HOME}/bin/hdfs dfs -cat /p11/input/input.txt' ''',
        capture=True,
    ).stdout
    if "Live datanodes (2)" not in hdfs or "distributed-hadoop" not in hdfs:
        raise P11Error("HDFS validation failed")
    evidence["tests"]["hdfs"] = "PASS"

    yarn_nodes = remote(
        master,
        f"sudo -u hadoop bash -lc '. /etc/p11-platform.env; timeout 20s {HADOOP_HOME}/bin/yarn node -list -all'",
        capture=True,
    ).stdout
    if yarn_nodes.count("RUNNING") < 2:
        raise P11Error("YARN does not show 2 RUNNING NodeManagers")
    evidence["tests"]["yarn_nodes"] = "PASS"

    mr = remote(
        master,
        f'''sudo -u hadoop bash -lc '. /etc/p11-platform.env; \
timeout 180s {HADOOP_HOME}/bin/yarn jar \
{HADOOP_HOME}/share/hadoop/mapreduce/hadoop-mapreduce-examples-{HADOOP_VERSION}.jar pi 2 100' ''',
        capture=True,
    )
    mr_text = (mr.stdout or "") + (mr.stderr or "")
    if "Estimated value of Pi is" not in mr_text:
        print(mr_text)
        raise P11Error("MapReduce/YARN Pi validation failed")
    evidence["tests"]["mapreduce"] = "PASS"

    remote_root(master, "printf '1\\n2\\n3\\n' >/tmp/p11-hive-numbers.txt; chmod 0644 /tmp/p11-hive-numbers.txt")
    hive_sql = """CREATE DATABASE IF NOT EXISTS p11;
USE p11;
DROP TABLE IF EXISTS numbers;
CREATE TABLE numbers(n INT) STORED AS TEXTFILE;
LOAD DATA LOCAL INPATH '/tmp/p11-hive-numbers.txt' OVERWRITE INTO TABLE numbers;
SELECT SUM(n) AS total FROM numbers;
"""
    remote_root(
        master,
        f"""cat >/tmp/p11-hive-validate.sql <<'P11SQL'
{hive_sql}P11SQL
chown hadoop:hadoop /tmp/p11-hive-numbers.txt /tmp/p11-hive-validate.sql
chmod 0644 /tmp/p11-hive-numbers.txt /tmp/p11-hive-validate.sql
""",
    )
    hive = remote(
        master,
        f"""sudo -u hadoop bash -lc '. /etc/p11-platform.env; timeout 120s {HIVE_HOME}/bin/beeline -u jdbc:hive2://{master["fqdn"]}:10000/default --silent=true --showHeader=false --outputformat=tsv2 -f /tmp/p11-hive-validate.sql'""",
        capture=True,
    )
    hive_text = (hive.stdout or "") + (hive.stderr or "")
    if not any(line.strip() == "6" for line in hive_text.splitlines()):
        print(hive_text)
        raise P11Error("Hive query validation failed")
    evidence["tests"]["hive"] = "PASS"

    spark = remote(
        master,
        f'''sudo -u hadoop bash -lc '. /etc/p11-platform.env; \
{SPARK_HOME}/bin/spark-submit \
--master yarn --deploy-mode client \
--conf spark.executor.instances=2 \
--conf spark.executor.cores=1 \
--conf spark.executor.memory=1g \
--class org.apache.spark.examples.SparkPi \
{SPARK_HOME}/examples/jars/spark-examples_2.12-{SPARK_VERSION}.jar 10' ''',
        capture=True,
    )
    spark_text = (spark.stdout or "") + (spark.stderr or "")
    if "Pi is roughly" not in spark_text:
        print(spark_text)
        raise P11Error("Spark-on-YARN validation failed")
    evidence["tests"]["spark_on_yarn"] = "PASS"

    evidence["tests"]["overall"] = "PASS"
    write_json("validation.json", evidence)
    print(json.dumps(evidence, indent=2))
    return evidence


def full():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stages = []
    overall_start = time.time()

    for name, fn in [
        ("prepare", prepare),
        ("install", install),
        ("configure", configure),
        ("start", start),
        ("validate", validate),
    ]:
        start_time = time.time()
        print(f"\n========== STAGE: {name.upper()} ==========")
        fn()
        elapsed = round(time.time() - start_time, 3)
        stages.append({"stage": name, "elapsed_seconds": elapsed})
        print(f"STAGE {name}: {elapsed:.3f}s")

    total = round(time.time() - overall_start, 3)
    metrics = {
        "stack": {
            "hadoop": HADOOP_VERSION,
            "hive": HIVE_VERSION,
            "spark": SPARK_VERSION,
            "java": 17,
        },
        "stages": stages,
        "total_elapsed_seconds": total,
        "note": "Measured from host preparation through functional validation; Terraform provisioning is measured separately unless Jenkins wraps both phases.",
    }
    write_json("platform-provisioning-metrics.json", metrics)
    print(f"\nP11 DISTRIBUTED HADOOP LAB: PASS ({total:.3f}s)")


def main():
    parser = argparse.ArgumentParser(description="Project 11 distributed Hadoop lab orchestrator")
    parser.add_argument(
        "command",
        choices=[
            "inventory",
            "prepare",
            "install",
            "configure",
            "start",
            "status",
            "validate",
            "secure-identity",
            "secure-lce",
            "secure-kerberos",
            "secure-tls",
            "secure-hadoop",
            "secure-directory",
            "secure-hive",
            "secure-validate",
            "full",
        ],
    )
    parser.add_argument(
        "--profile",
        choices=["base", "secure"],
        default="base",
        help="Deployment profile. base is the validated Hadoop/Hive/Spark stack; secure adds Kerberos/TLS/LDAP.",
    )
    args = parser.parse_args()

    try:
        if args.profile == "base":
            if args.command == "full":
                full()
            elif args.command.startswith("secure-"):
                raise P11Error(
                    f"{args.command} requires --profile secure"
                )
            else:
                {
                    "inventory": inventory,
                    "prepare": prepare,
                    "install": install,
                    "configure": configure,
                    "start": start,
                    "status": status,
                    "validate": validate,
                }[args.command]()

        else:
            secure_commands = {
                "secure-identity": secure_identity,
                "secure-lce": secure_lce,
                "secure-kerberos": secure_kerberos,
                "secure-tls": secure_tls,
                "secure-hadoop": secure_hadoop,
                "secure-directory": secure_directory,
                "secure-hive": secure_hive,
                "secure-validate": secure_validate,
            }

            if args.command == "full":
                secure_full()
            elif args.command in secure_commands:
                secure_commands[args.command]()
            elif args.command == "inventory":
                inventory()
            elif args.command == "status":
                status()
            else:
                raise P11Error(
                    f"{args.command} is a base-profile command; "
                    "use --profile base"
                )

    except P11Error as e:
        print(f"P11 ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
