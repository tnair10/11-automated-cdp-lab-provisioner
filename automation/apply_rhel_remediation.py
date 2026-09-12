#!/usr/bin/env python3
import argparse,json
from pathlib import Path

SIM={"services.chronyd.enabled","services.chronyd.active","services.time_synced","services.firewalld.enabled","services.fapolicyd.enabled","java.major_version"}

def put(p,s):
    p.parent.mkdir(parents=True,exist_ok=True)
    old=p.read_text() if p.exists() else None
    if old==s:return False
    p.write_text(s);return True

def main():
    ap=argparse.ArgumentParser(description="Project 11 sandbox remediation executor")
    ap.add_argument("--plan",required=True);ap.add_argument("--root",required=True);ap.add_argument("--json-out")
    a=ap.parse_args(); root=Path(a.root).resolve()
    if root==Path("/"): ap.error("refusing real root filesystem in sandbox stage")
    plan=json.loads(Path(a.plan).read_text())
    if plan.get("blocker_count",0): ap.error("refusing plan with blockers")
    acts={x["id"]:x for x in plan.get("actions",[])}
    changed=[]; same=[]; simulated=[]
    def track(i,p,s):
        (changed if put(p,s) else same).append(i)
    if "network.hostname" in acts:
        track("network.hostname",root/"etc/hostname",str(acts["network.hostname"]["expected"])+"\n")
    vals={}
    if "network.ipv6_disabled" in acts:
        vals["net.ipv6.conf.all.disable_ipv6"]=1; vals["net.ipv6.conf.default.disable_ipv6"]=1
    if "kernel.net.core.somaxconn" in acts: vals["net.core.somaxconn"]=acts["kernel.net.core.somaxconn"]["expected"]
    if "kernel.vm.swappiness" in acts: vals["vm.swappiness"]=acts["kernel.vm.swappiness"]["expected"]
    if vals:
        ids=[i for i in ("network.ipv6_disabled","kernel.net.core.somaxconn","kernel.vm.swappiness") if i in acts]
        text="# Managed by Project 11\n"+"".join(f"{k} = {vals[k]}\n" for k in sorted(vals))
        bucket=changed if put(root/"etc/sysctl.d/99-p11-host-prep.conf",text) else same
        bucket.extend(ids)
    if "services.selinux.mode" in acts:
        p=root/"etc/selinux/config"; lines=p.read_text().splitlines() if p.exists() else []
        mode=str(acts["services.selinux.mode"]["expected"]); out=[]; found=False
        for line in lines:
            if line.startswith("SELINUX="): out.append("SELINUX="+mode); found=True
            else: out.append(line)
        if not found: out.append("SELINUX="+mode)
        track("services.selinux.mode",p,"\n".join(out).rstrip()+"\n")
    if "kernel.transparent_hugepages" in acts:
        text="[Unit]\nDescription=Disable THP for Project 11\nAfter=local-fs.target\n\n[Service]\nType=oneshot\nExecStart=/bin/sh -c \"echo never > /sys/kernel/mm/transparent_hugepage/enabled\"\nRemainAfterExit=yes\n\n[Install]\nWantedBy=multi-user.target\n"
        track("kernel.transparent_hugepages",root/"etc/systemd/system/p11-disable-thp.service",text)
    if "cgroups.mode" in acts:
        if acts["cgroups.mode"]["expected"]!="v1": ap.error("sandbox supports only cgroup target v1")
        track("cgroups.mode",root/"etc/default/grub.d/99-p11-cgroup-v1.cfg","GRUB_CMDLINE_LINUX=\"$GRUB_CMDLINE_LINUX systemd.unified_cgroup_hierarchy=0\"\n")
    simulated=[i for i in acts if i in SIM]
    manifest={"stage":"sandbox","node":plan.get("node"),"simulated_command_actions":simulated}
    mchg=put(root/"var/lib/p11/remediation-intent.json",json.dumps(manifest,indent=2,sort_keys=True)+"\n")
    result={"mode":"sandbox-apply","changed_count":len(changed),"unchanged_count":len(same),"simulated_count":len(simulated),"manifest_changed":mchg,"reboot_required":plan.get("reboot_required",False),"changed":changed,"unchanged":same,"simulated":simulated}
    print("========================================\n PROJECT 11 SANDBOX REMEDIATION\n========================================")
    print("Changed:",len(changed));print("Already compliant:",len(same));print("Simulated commands:",len(simulated));print("Reboot required:",result["reboot_required"])
    for i in changed: print("CHANGED",i)
    for i in same: print("OK     ",i)
    for i in simulated: print("SIMULATE",i)
    print("Real host mutation is disabled in this stage.")
    if a.json_out: Path(a.json_out).write_text(json.dumps(result,indent=2)+"\n")
if __name__=="__main__": main()
