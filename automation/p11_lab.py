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

JAVA_BIN="$(readlink -f "$(command -v java)")"
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

JAVA_BIN="$(readlink -f "$(command -v java)")"
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
cat >{HADOOP_HOME}/etc/hadoop/workers <<'EOF'
{workers}EOF

cat >>{HADOOP_HOME}/etc/hadoop/hadoop-env.sh <<EOF
export JAVA_HOME=$JAVA_HOME
export HADOOP_HOME={HADOOP_HOME}
export HADOOP_CONF_DIR={HADOOP_HOME}/etc/hadoop
EOF
cat >>{HADOOP_HOME}/etc/hadoop/yarn-env.sh <<EOF
export JAVA_HOME=$JAVA_HOME
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
if ! grep -Eq '^host[[:space:]]+all[[:space:]]+all[[:space:]]+127\\.0\\.0\\.1/32[[:space:]]+' "$HBA"; then
  sed -i '1ihost all all 127.0.0.1/32 md5' "$HBA"
fi
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

cat >{HIVE_HOME}/conf/hive-env.sh <<EOF
export JAVA_HOME=\\$(dirname \\$(dirname \\$(readlink -f \\$(command -v java))))
export HADOOP_HOME={HADOOP_HOME}
export HADOOP_CONF_DIR={HADOOP_HOME}/etc/hadoop
export HIVE_HOME={HIVE_HOME}
EOF
chmod 0755 {HIVE_HOME}/conf/hive-env.sh
chown -R hadoop:hadoop {HIVE_HOME}

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
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(command -v java))))
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
        f"sudo -u hadoop bash -lc '. /etc/p11-platform.env; {HADOOP_HOME}/bin/yarn node -list -all'",
        capture=True,
    ).stdout
    if yarn_nodes.count("RUNNING") < 2:
        raise P11Error("YARN does not show 2 RUNNING NodeManagers")
    evidence["tests"]["yarn_nodes"] = "PASS"

    mr = remote(
        master,
        f'''sudo -u hadoop bash -lc '. /etc/p11-platform.env; \
{HADOOP_HOME}/bin/yarn jar \
{HADOOP_HOME}/share/hadoop/mapreduce/hadoop-mapreduce-examples-{HADOOP_VERSION}.jar pi 2 100' ''',
        capture=True,
    )
    mr_text = (mr.stdout or "") + (mr.stderr or "")
    if "Estimated value of Pi is" not in mr_text:
        print(mr_text)
        raise P11Error("MapReduce/YARN Pi validation failed")
    evidence["tests"]["mapreduce"] = "PASS"

    remote_root(master, "printf '1\\n2\\n3\\n' >/tmp/p11-hive-numbers.txt; chmod 0644 /tmp/p11-hive-numbers.txt")
    hive_sql = (
        "CREATE DATABASE IF NOT EXISTS p11; "
        "USE p11; "
        "DROP TABLE IF EXISTS numbers; "
        "CREATE TABLE numbers(n INT) STORED AS TEXTFILE; "
        "LOAD DATA LOCAL INPATH '/tmp/p11-hive-numbers.txt' OVERWRITE INTO TABLE numbers; "
        "SELECT SUM(n) AS total FROM numbers;"
    )
    hive = remote(
        master,
        f'''sudo -u hadoop bash -lc '. /etc/p11-platform.env; \
{HIVE_HOME}/bin/beeline -u jdbc:hive2://localhost:10000/default \
--silent=true --showHeader=false --outputformat=tsv2 \
-e {shlex.quote(hive_sql)}' ''',
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
        choices=["inventory", "prepare", "install", "configure", "start", "status", "validate", "full"],
    )
    args = parser.parse_args()

    try:
        {
            "inventory": inventory,
            "prepare": prepare,
            "install": install,
            "configure": configure,
            "start": start,
            "status": status,
            "validate": validate,
            "full": full,
        }[args.command]()
    except P11Error as e:
        print(f"P11 ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
