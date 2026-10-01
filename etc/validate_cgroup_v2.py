#!/usr/bin/env python3
"""Static checks for the mt6893 cgroup v2 backport.

Run from the repository root:
    python3 device/xiaomi/mt6893-common/etc/validate_cgroup_v2.py

Static only: no device, no build. The checks cover the things that are easy to
get wrong and hard to notice at runtime:

  1. /vendor/etc/task_profiles.json is produced from a single source file that
     really contains the stock schedtune profiles *and* the cpuset migration
     (two PRODUCT_COPY_FILES entries for one destination would leave whichever
     landed last, not a merge);
  2. the re-declared profiles are checked against TaskProfile::MoveTo, which
     REPLACES the action list - so each replacement has to list everything it
     still needs, and the replaced capacity profiles must end up carrying the
     SetAttribute we expect;
  3. every profile and aggregate we touch is reachable: the capacity profiles
     from the CPUSET_SP_* aggregates dispatched by sched_policy.cpp, and the
     camera profiles from the rc files that name them;
  4. the kernel names the cpuset v2 effective-mask files the way the v2 ABI
     spells them, and leaves the v1 names alone;
  5. the memcg compaction path is gated on a memory.reclaim capability check
     that this kernel cannot satisfy, so it degrades instead of erroring.

Exit status is 0 when every check passes, 1 otherwise.
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # .../mt6893-common/etc
DEVICE = os.path.dirname(HERE)                              # .../mt6893-common
# Repository root: etc -> mt6893-common -> xiaomi -> device -> <root>
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(DEVICE)))

CGROUPS_JSON = os.path.join(HERE, "cgroups.json")
TASK_PROFILES_JSON = os.path.join(HERE, "task_profiles.json")
POWERHINT_JSON = os.path.join(DEVICE, "configs", "powerhint.json")
POWER_RC = os.path.join(DEVICE, "init", "init.mt6893.power.rc")
MT6893_MK = os.path.join(DEVICE, "mt6893.mk")
CPUSET_C = os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "kernel", "cgroup", "cpuset.c")
MEMCONTROL_C = os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "mm", "memcontrol.c")
TASK_PROFILES_CPP = os.path.join(ROOT, "system", "core", "libprocessgroup",
                                 "task_profiles.cpp")
SCHED_POLICY_CPP = os.path.join(ROOT, "system", "core", "libprocessgroup",
                                "sched_policy.cpp")
STOCK_PROFILES = os.path.join(ROOT, "system", "core", "libprocessgroup", "profiles",
                              "task_profiles.json")
STOCK_PROFILES_30 = os.path.join(ROOT, "system", "core", "libprocessgroup", "profiles",
                                 "task_profiles_30.json")
CHOPIN_DEFCONFIG = os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "arch", "arm64",
                                "configs", "chopin_defconfig")

# Files that reference a profile by name in a "task_profiles" rc directive (or
# map a legacy cgroup path onto one).
RC_CONSUMERS = [
    "frameworks/av/camera/cameraserver/cameraserver.rc",
    "frameworks/av/services/camera/virtualcamera/virtual_camera.hal.rc",
    "hardware/interfaces/camera/provider/2.5/default/"
    "android.hardware.camera.provider@2.5-service.rc",
    "system/core/init/service_parser.cpp",
]

FAILURES = []
CHECKS = 0


def check(ok, label, detail=""):
    global CHECKS
    CHECKS += 1
    if ok:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def profile_map(data):
    return {p["Name"]: p for p in data.get("Profiles", []) if "Name" in p}


def action_summary(profile):
    return [(a["Name"], a.get("Params", {}).get("Controller", ""),
             a.get("Params", {}).get("Name", ""), a.get("Params", {}).get("Value", ""))
            for a in profile.get("Actions", [])]


def cpuset_values(profile):
    return [t[3] for t in action_summary(profile)
            if t[0] == "SetAttribute" and t[2] == "CpusetCpus"]


# ---------------------------------------------------------------- packaging --

def check_single_packaged_file():
    print("packaging: one vendor task_profiles.json from one source")
    mk = read(MT6893_MK)

    copies = re.findall(r"^\s*(\S+):\$\(TARGET_COPY_OUT_VENDOR\)/etc/task_profiles\.json",
                        mk, re.M)
    check(len(copies) == 1,
          "exactly one source is copied to /vendor/etc/task_profiles.json",
          "(found %s)" % copies)
    if copies:
        check(copies[0] == "$(LOCAL_PATH)/etc/task_profiles.json",
              "that source is the device file, not the stock one", "(got %s)" % copies[0])
        check(os.path.exists(os.path.join(DEVICE, "etc", "task_profiles.json")),
              "the device task_profiles.json exists")
    check("task_profiles_30.json:$(TARGET_COPY_OUT_VENDOR)" not in mk,
          "the stock task_profiles_30.json is not copied to the same target any more")

    # libprocessgroup reads the vendor file exactly once, so two files to one
    # destination could never be a merge at runtime.
    tp = read(TASK_PROFILES_CPP)
    n = len(re.findall(r"Load\(CgroupMap::GetInstance\(\), TASK_PROFILE_DB_VENDOR_FILE\)", tp))
    check(n == 1, "libprocessgroup loads the vendor profile file exactly once",
          "(found %d call sites)" % n)


def check_stock_profiles_preserved():
    print("packaging: stock schedtune content is carried over")
    ours = load(TASK_PROFILES_JSON)
    stock30 = load(STOCK_PROFILES_30)

    our_attrs = {a["Name"] for a in ours.get("Attributes", [])}
    for a in stock30.get("Attributes", []):
        check(a["Name"] in our_attrs, "stock attribute %s is present" % a["Name"])

    ours_p = profile_map(ours)
    stock_p = profile_map(stock30)
    missing = [n for n in stock_p if n not in ours_p]
    check(not missing, "every stock task_profiles_30.json profile is carried over",
          "(missing %s)" % missing)

    # These only carry schedtune actions, and /dev/stune is mounted on v1, so
    # they have to keep them verbatim.
    for name in ("HighEnergySaving", "NormalPerformance", "ServicePerformance",
                 "HighPerformance", "MaxPerformance", "RealtimePerformance",
                 "NNApiHALPerformance", "Dex2oatPerformance", "CpuPolicySpread",
                 "CpuPolicyPack"):
        if name not in ours_p:
            check(False, "%s carried over" % name)
            continue
        check(action_summary(ours_p[name]) == action_summary(stock_p[name]),
              "%s keeps its stock actions" % name,
              "(ours %s vs stock %s)" % (action_summary(ours_p[name]),
                                         action_summary(stock_p[name])))


# ----------------------------------------------------------- MoveTo merging --

def check_moveto_semantics():
    print("profile merge semantics (TaskProfile::MoveTo replaces)")
    tp = read(TASK_PROFILES_CPP)
    check("profile->elements_ = std::move(elements_);" in tp,
          "MoveTo replaces the action list (asserted, not assumed)")
    check("profile->MoveTo(iter->second.get())" in tp,
          "TaskProfiles::Load routes a re-declared profile through MoveTo")

    ours_p = profile_map(load(TASK_PROFILES_JSON))
    stock_p = profile_map(load(STOCK_PROFILES))

    # Replaced capacity profiles must end up carrying exactly the mask write,
    # and the only thing they dropped may be the dead cpuset JoinCgroup.
    expected = {
        "ProcessCapacityLow": "0-3",          # /dev/cpuset/background
        "ProcessCapacityNormal": "0-7",       # reset back to the default mask
        "ProcessCapacityHigh": "0-6",         # /dev/cpuset/foreground
        "ProcessCapacityHighWI": "0-6",       # /dev/cpuset/foreground_window
        "ProcessCapacityMax": "0-7",          # /dev/cpuset/top-app
        "ServiceCapacityLow": "0-3",          # /dev/cpuset/system-background
        "ServiceCapacityRestricted": "0-3",   # /dev/cpuset/restricted
        "CameraServiceCapacity": "0-7",       # /dev/cpuset/camera-daemon
    }
    for name, mask in expected.items():
        p = ours_p.get(name)
        if p is None:
            check(False, "%s is declared" % name)
            continue
        check(cpuset_values(p) == [mask],
              "%s ends up setting CpusetCpus to %s" % (name, mask),
              "(got %s)" % cpuset_values(p))
        check(len(action_summary(p)) == 1,
              "%s has exactly the one SetAttribute action" % name,
              "(got %s)" % action_summary(p))
        old = stock_p.get(name)
        if old:
            controllers = {a.get("Params", {}).get("Controller")
                           for a in old.get("Actions", []) if a["Name"] == "JoinCgroup"}
            check(controllers <= {"cpuset"},
                  "%s only replaced a cpuset JoinCgroup" % name,
                  "(stock controllers %s)" % sorted(str(c) for c in controllers))

    # CameraServicePerformance keeps a working schedtune action, so the
    # replacement has to re-list it next to the new mask.
    p = ours_p.get("CameraServicePerformance")
    check(p is not None, "CameraServicePerformance is declared")
    if p:
        acts = action_summary(p)
        stock_acts = action_summary(profile_map(load(STOCK_PROFILES_30))
                                    ["CameraServicePerformance"])
        for a in stock_acts:
            check(a in acts, "CameraServicePerformance re-lists its stock %s action" % a[0],
                  "(ours %s)" % acts)
        check(cpuset_values(p) == ["0-7"], "CameraServicePerformance adds the 0-7 mask")


# ------------------------------------------------------------- reachability --

def check_profiles_are_consumed():
    print("profiles are reachable from a real caller")
    ours = load(TASK_PROFILES_JSON)
    ours_p = profile_map(ours)
    stock = load(STOCK_PROFILES)
    stock_aggs = {a["Name"]: a["Profiles"] for a in stock.get("AggregateProfiles", [])}

    sp = read(SCHED_POLICY_CPP)
    dispatched = set(re.findall(r'SetTaskProfiles\(tid, \{"(CPUSET_SP_[A-Z_]+)"\}', sp))
    check(dispatched, "sched_policy.cpp dispatches CPUSET_SP_* aggregates",
          "(found %s)" % sorted(dispatched))

    replaced = {"ProcessCapacityLow", "ProcessCapacityNormal", "ProcessCapacityHigh",
                "ProcessCapacityHighWI", "ProcessCapacityMax", "ServiceCapacityLow",
                "ServiceCapacityRestricted"}
    for agg in sorted(dispatched):
        members = stock_aggs.get(agg, [])
        check(any(m in replaced for m in members),
              "%s reaches a replaced capacity profile" % agg, "(members %s)" % members)

    # The reset path, verified end to end rather than by name only.
    ours_aggs = {a["Name"]: a["Profiles"] for a in ours.get("AggregateProfiles", [])}
    check("CPUSET_SP_DEFAULT" in ours_aggs, "CPUSET_SP_DEFAULT is redefined")
    if "CPUSET_SP_DEFAULT" in ours_aggs:
        members = ours_aggs["CPUSET_SP_DEFAULT"]
        missing = [m for m in stock_aggs.get("CPUSET_SP_DEFAULT", []) if m not in members]
        check(not missing, "CPUSET_SP_DEFAULT keeps all of its stock members",
              "(dropped %s)" % missing)
        check("ProcessCapacityNormal" in members,
              "CPUSET_SP_DEFAULT resets the mask via ProcessCapacityNormal")
        check("ProcessCapacityNormal" in members
              and "TimerSlackNormal" in members,
              "the reset does not displace the stock TimerSlackNormal")
        check(cpuset_values(ours_p["ProcessCapacityNormal"]) == ["0-7"],
              "the reset really writes the full 0-7 mask")

    named = set()
    for rel in RC_CONSUMERS:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            check(False, "consumer file exists: %s" % rel)
            continue
        text = read(path)
        for m in re.findall(r"task_profiles\s+([A-Za-z0-9_ ]+)", text):
            named.update(m.split())
        for m in re.findall(r'\{"/dev/cpuset/[^"]+",\s*"([A-Za-z0-9_]+)"\}', text):
            named.add(m)

    for name in ("CameraServiceCapacity", "CameraServicePerformance"):
        check(name in named, "%s is named by a real caller (rc / service_parser)" % name,
              "(named: %s)" % sorted(named))

    stock30_names = set(profile_map(load(STOCK_PROFILES_30)))
    orphans = [n for n in ours_p
               if n not in named and n not in replaced and n not in stock30_names]
    check(not orphans, "no profile we declare is left without a caller",
          "(orphans %s)" % orphans)


# ------------------------------------------------------------ dead v1 paths --

def check_dead_paths_removed():
    print("dead /dev/cpuset configuration")
    rc = read(POWER_RC)
    check(not re.search(r"^\s*write\s+/dev/cpuset", rc, re.M),
          "init.mt6893.power.rc no longer writes /dev/cpuset")
    check("/dev/cpuset" in rc,
          "init.mt6893.power.rc explains where the masks went instead")

    ph = load(POWERHINT_JSON)
    paths = [e.get("Path", "") for section in ph.values() for e in section]
    check(not any(p.startswith("/dev/cpuset") for p in paths),
          "powerhint.json has no /dev/cpuset Path left",
          "(found %s)" % [p for p in paths if p.startswith("/dev/cpuset")])

    cg = load(CGROUPS_JSON)
    v1 = [c["Controller"] for c in cg.get("Cgroups", [])]
    check("cpuset" not in v1 and "cpu" not in v1,
          "cpuset/cpu are not re-requested on the v1 hierarchy", "(found %s)" % v1)


def check_cgroups_json():
    print("cgroups.json")
    data = load(CGROUPS_JSON)
    v2 = [c["Controller"] for c in data["Cgroups2"].get("Controllers", [])]
    for want in ("cpuset", "cpu", "memory", "pids", "freezer"):
        check(want in v2, "v2 controllers include %s" % want, "(found %s)" % v2)
    for c in data["Cgroups2"].get("Controllers", []):
        if c["Controller"] == "cpuset":
            check(c.get("NeedsActivation") is True, "cpuset is marked NeedsActivation")
            check(c.get("MaxActivationDepth") == 3,
                  "cpuset MaxActivationDepth reaches root -> apps -> apps/uid_N",
                  "(got %r)" % c.get("MaxActivationDepth"))
    comment = " ".join(data.get("_comment", []))
    check("is still mounted by init" not in comment,
          "the stale '/dev/cpuset is still mounted by init' claim is gone")


# --------------------------------------------------------------- kernel side --

def check_cpuset_abi_names():
    print("cpuset v2 ABI file names")
    src = read(CPUSET_C)
    m = re.search(r"static struct cftype cpuset_dfl_cftypes\[\]\s*=\s*\{(.*?)\n\};", src, re.S)
    check(m is not None, "found cpuset_dfl_cftypes")
    if m:
        names = re.findall(r'\.name\s*=\s*"([^"]+)"', m.group(1))
        check("cpus.effective" in names, "v2 array spells 'cpus.effective'", "(got %s)" % names)
        check("mems.effective" in names, "v2 array spells 'mems.effective'", "(got %s)" % names)
        check("effective_cpus" not in names and "effective_mems" not in names,
              "v2 array no longer uses the non-ABI names", "(got %s)" % names)
        check("cpus" in names and "mems" in names,
              "v2 array still exposes cpuset.cpus and cpuset.mems")

    m1 = re.search(r"static struct cftype files\[\]\s*=\s*\{(.*?)\n\};", src, re.S)
    check(m1 is not None, "found the cpuset v1 files[] array")
    if m1:
        names = re.findall(r'\.name\s*=\s*"([^"]+)"', m1.group(1))
        check("effective_cpus" in names, "v1 array keeps effective_cpus", "(got %s)" % names)
        check("effective_mems" in names, "v1 array keeps effective_mems", "(got %s)" % names)
        check("cpus.effective" not in names, "v1 array does not gain the v2-only names")


def check_memcg_compaction_gate():
    print("memcg compaction is gated on memory.reclaim availability")
    mem = read(MEMCONTROL_C)
    check('.name = "reclaim"' not in mem,
          "this kernel has no memory.reclaim cftype (so compaction cannot run)")

    tp = read(TASK_PROFILES_CPP)
    check("bool CompactMemcgAction::IsValid(" in tp, "CompactMemcgAction::IsValid exists")
    check("access(memory_reclaim_path.c_str(), F_OK) != 0" in tp,
          "IsValid probes memory.reclaim with access(F_OK)")
    check("bool TaskProfile::IsValidForProcess(" in tp,
          "TaskProfile::IsValidForProcess walks the actions")
    check("IsValidForProcess(uid_t, pid_t pid) const" in tp,
          "the per-process validity entry point exists")

    java = os.path.join(ROOT, "frameworks/base/services/core/java/com/android/server/am/"
                               "CachedAppOptimizer.java")
    check(os.path.exists(java), "CachedAppOptimizer.java found")
    if os.path.exists(java):
        text = read(java)
        check("profileValidForMemcg" in text,
              "CachedAppOptimizer gates memcg compaction on profileValidForMemcg()")
        check("performMemcgCompaction" in text, "performMemcgCompaction is the gated branch")

    # The gate has to be consulted *before* the memcg path is chosen.
    if os.path.exists(java):
        text = read(java)
        m = re.search(r"if \(Flags\.useMemcgForCompaction\(\) &&\s*"
                      r"profileValidForMemcg\(resolvedProfile\)\)", text)
        check(m is not None,
              "the validity check guards the branch (not a later, cosmetic call)")


def check_defconfig():
    print("chopin_defconfig")
    text = read(CHOPIN_DEFCONFIG)
    for opt in ("CONFIG_CGROUPS", "CONFIG_CGROUP_PIDS", "CONFIG_CPUSETS", "CONFIG_MEMCG",
                "CONFIG_CGROUP_FREEZER", "CONFIG_CGROUP_CPUACCT", "CONFIG_CGROUP_SCHED",
                "CONFIG_FAIR_GROUP_SCHED", "CONFIG_CFS_BANDWIDTH"):
        check(re.search(r"^%s=y$" % opt, text, re.M) is not None, "%s=y is present" % opt)


def check_defconfig_uniqueness():
    print("chopin_defconfig: no duplicate options")
    text = read(CHOPIN_DEFCONFIG)
    seen = {}
    for name in re.findall(r"^(CONFIG_[A-Za-z0-9_]+)=", text, re.M):
        seen[name] = seen.get(name, 0) + 1
    dupes = sorted(n for n, c in seen.items() if c > 1)
    check(not dupes, "no CONFIG option is declared twice in the defconfig",
          "(duplicated: %s)" % dupes)
    check(len(re.findall(r"^CONFIG_CPUSETS=y$", text, re.M)) == 1,
          "CONFIG_CPUSETS=y appears exactly once")
    check(len(re.findall(r"^CONFIG_CGROUPS=y$", text, re.M)) == 1,
          "CONFIG_CGROUPS=y appears exactly once")


def check_no_stale_doc_refs():
    print("source comments cite files that exist in this tree")
    doc = os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "Documentation", "cgroup-v2.txt")
    check(os.path.exists(doc), "Documentation/cgroup-v2.txt exists in this tree")

    core = read(os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "kernel", "sched", "core.c"))
    check("Documentation/admin-guide/cgroup-v2.rst" not in core,
          "core.c no longer cites the non-existent admin-guide/cgroup-v2.rst")
    check("Documentation/cgroup-v2.txt" in core,
          "core.c cites the spec that actually ships with this tree")

    cpuset = read(CPUSET_C)
    check("Documentation/admin-guide/cgroup-v2.rst" not in cpuset,
          "cpuset.c no longer cites the non-existent admin-guide/cgroup-v2.rst")
    # This tree's spec never spells out these file names, so the comment above
    # the v2 entries must not present it as the local authority for them.
    idx = cpuset.find('.name = "cpus.effective"')
    check(idx != -1, "found the cpus.effective cftype")
    if idx != -1:
        preceding = cpuset[max(0, idx - 1200):idx]
        start = preceding.rfind("/*")
        comment = preceding[start:] if start != -1 else ""
        check("does not" in comment and "spell out these names" in comment,
              "cpuset.c does not claim the local spec defines these names",
              "(nearest comment: %r)" % comment[-160:])
        check("Documentation/cgroup-v2.txt" in comment,
              "cpuset.c mentions the local spec only to say it is silent")


def check_vendor_rc_has_no_dead_cpuset():
    print("vendor rc files do not write dead /dev/cpuset paths")
    vendor_rc = os.path.join(ROOT, "vendor", "xiaomi", "chopin", "proprietary", "vendor",
                             "etc", "init", "camerahalserver.rc")
    check(os.path.exists(vendor_rc), "camerahalserver.rc found")
    if os.path.exists(vendor_rc):
        text = read(vendor_rc)
        check(not re.search(r"^\s*write\s+/dev/cpuset", text, re.M),
              "camerahalserver.rc no longer writes /dev/cpuset/camera-background/cpus")
        # Unrelated directives must survive the cleanup.
        check("chmod 0666 /sys/kernel/fpsgo/common/force_onoff" in text,
              "camerahalserver.rc keeps its unrelated chmod")
        check("task_profiles CameraServiceCapacity MaxPerformance" in text,
              "camerahalserver.rc keeps its task_profiles directive")


def main():
    for fn in (check_single_packaged_file, check_stock_profiles_preserved,
               check_moveto_semantics, check_profiles_are_consumed,
               check_dead_paths_removed, check_cgroups_json,
               check_cpuset_abi_names, check_memcg_compaction_gate, check_defconfig,
               check_defconfig_uniqueness, check_no_stale_doc_refs,
               check_vendor_rc_has_no_dead_cpuset):
        fn()
        print("")

    print("%d checks, %d failures" % (CHECKS, len(FAILURES)))
    if FAILURES:
        print("failed: %s" % ", ".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())