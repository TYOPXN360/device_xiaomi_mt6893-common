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
    check('.name = "reclaim"' in mem,
          "the kernel supplies the memory.reclaim cftype required for memcg compaction")

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


def check_cpu_stat_v2_abi():
    print("cpu.stat v2 ABI (kernel/sched/core.c)")
    core = read(os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "kernel", "sched", "core.c"))
    cpuacct = read(os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "kernel", "sched",
                                "cpuacct.c"))

    # --- v1 must be untouched -------------------------------------------------
    m = re.search(r"static int cpu_stats_show\(.*?\n\}", core, re.S)
    check(m is not None, "found the v1 cpu_stats_show")
    if m:
        v1 = m.group(0)
        check("throttled_time %llu" in v1, "v1 keeps the throttled_time field name")
        check("div_u64" not in v1, "v1 keeps its raw (unrescaled) value")
        check("usage_usec" not in v1, "v1 does not gain the v2 field names")

    # --- v2 handler ----------------------------------------------------------
    m = re.search(r"static int cpu_dfl_stats_show\(.*?\n\}", core, re.S)
    check(m is not None, "found the v2 cpu_dfl_stats_show")
    if m:
        v2 = m.group(0)
        printed = set(re.findall(r'seq_printf\(sf, "([a-z_]+)', v2))
        check(printed == {"usage_usec", "user_usec", "system_usec", "nr_periods", "nr_throttled", "throttled_usec"},
              "v2 cpu.stat reports all six ABI fields",
              "(printed %s)" % sorted(printed))
        check("throttled_time" not in printed,
              "v2 never prints throttled_time as a field name")
        check("throttled_usec" in printed, "v2 prints throttled_usec")
        check("NSEC_PER_USEC" in v2,
              "v2 converts the nanosecond value to microseconds")
        check("#ifdef CONFIG_CFS_BANDWIDTH" in v2,
              "v2 has its own CFS_BANDWIDTH branch for the no-bandwidth case")
        check(v2.count("seq_printf") >= 6,
              "both the bandwidth and the no-bandwidth arms print all three fields",
              "(found %d prints)" % v2.count("seq_printf"))

    # --- task_group-backed usage counters -----------------------------------
    for f in ("usage_usec", "user_usec", "system_usec"):
        check(f in (m.group(0) if m else ""),
              "v2 cpu.stat emits %s from per-group counters" % f)
    check("cpuacct_get_task_group_usage" in core,
          "core reads task_group counters through the dedicated helper")
    check("cpuacct_get_task_group_usage" in cpuacct,
          "cpuacct.c implements the task_group stats reader")
    check('#include "cpuacct.h"' in core,
          "core includes the stats helper declaration")
    check("css_ca(" not in core,
          "core does not reinterpret a task_group css as a cpuacct css")
    check("css_tg(css)" in cpuacct,
          "reader converts the cpu-controller css to task_group")
    check("READ_ONCE(tsk->sched_task_group)" in cpuacct,
          "accounting snapshots sched_task_group once")
    check("for (; tg; tg = tg->parent)" in cpuacct and
          "if (!tg->parent)" in cpuacct,
          "accounting walks the parent chain through the root group")
    check("__this_cpu_add(tg->cpustat->usage_ns, val)" in cpuacct and
          "__this_cpu_add(tg->cpustat->user_ns, val)" in cpuacct and
          "__this_cpu_add(tg->cpustat->sys_ns, val)" in cpuacct,
          "counters use dynamic per-CPU task_group members")
    check("task_group_account_usage(tsk, cputime)" in cpuacct and
          "task_group_account_cputime(tsk, index, val)" in cpuacct,
          "runtime and user/system counters use separate sources")
    cpuacct_h = read(os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "kernel", "sched",
                                  "cpuacct.h"))
    paired_guard = "#if defined(CONFIG_CGROUP_CPUACCT) && defined(CONFIG_CGROUP_SCHED)"
    check(cpuacct.count(paired_guard) >= 4,
          "task_group accounting helpers and call sites require both configs")
    check(core.find(paired_guard) < core.find("root_task_group.cpustat = alloc_percpu"),
          "root task_group counters are initialized only when both configs are enabled")
    check("extern void cpuacct_charge" in cpuacct_h and
          "extern void cpuacct_account_field" in cpuacct_h,
          "legacy cpuacct APIs remain available under CONFIG_CGROUP_CPUACCT alone")
    check("#ifdef CONFIG_CGROUP_SCHED" in cpuacct_h and
          "extern void cpuacct_get_task_group_usage" in cpuacct_h,
          "the task_group reader declaration is guarded by CONFIG_CGROUP_SCHED")
    sched = read(os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "kernel", "sched",
                              "sched.h"))
    check("struct task_group_cputat" in sched and "usage_ns" in sched and
          "user_ns" in sched and "sys_ns" in sched,
          "task_group stores only the three reported per-CPU counters")

    # --- the v2 comment documents counter sources and config -----------------
    m = re.search(r"/\*\n \* cgroup v2 .cpu\.stat.*?\n \*/", core, re.S)
    check(m is not None, "the v2 handler carries an explanatory comment")
    if m:
        c = m.group(0)
        for phrase in ("usage_usec", "cpuacct_charge()", "cpuacct_account_field()",
                       "ancestor", "NSEC_PER_USEC", "CONFIG_CGROUP_CPUACCT"):
            check(phrase in c, "the comment documents %r" % phrase)

    selftest = os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "tools", "testing",
                            "selftests", "cgroup")
    test_c = read(os.path.join(selftest, "cpu_stat_test.c"))
    test_sh = read(os.path.join(selftest, "run_cpu_stat_test.sh"))
    test_mk = read(os.path.join(selftest, "Makefile"))
    top_mk = read(os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "tools", "testing",
                               "selftests", "Makefile"))
    check("TARGETS += cgroup" in top_mk,
          "the cgroup selftest is in the existing kselftest target list")
    check("include ../lib.mk" in test_mk and "TEST_GEN_PROGS" in test_mk and
          "TEST_PROGS" in test_mk,
          "the cgroup test follows the existing kselftest harness")
    for name in ("test_user_workload", "test_syscall_workload", "test_usage_counter",
                 "test_parent_includes_children", "test_migration"):
        check(name in test_c, "selftest covers %s" % name)
    check('TEST_BINARY="${TEST_BINARY:-' in test_sh,
          "the shell runner falls back to the test binary beside itself")
    check('"$TEST_BINARY" "$CG2_MOUNT"' in test_sh,
          "the shell runner invokes the generated C test")
    check("cpu_stat_selftest.%d.%ld" in test_c,
          "the selftest uses a unique per-run cgroup name")
    check("enable_cpu_controller(parent_dir)" in test_c and
          "enable_cpu_controller(child_a)" not in test_c and
          "enable_cpu_controller(child_b)" not in test_c,
          "only the task-free parent delegates the cpu controller")
    check("SIGKILL" not in test_c and "kill(" not in test_c,
          "the selftest never kills processes already in a test cgroup")
    check("if (mkdir(path, 0755) < 0)" in test_c and "errno != EEXIST" not in test_c,
          "the selftest refuses to reuse an existing cgroup")
    last_child_remove = test_c.rfind("rmdir(path)")
    disable_cpu = test_c.find('write(fd, "-cpu", 4)')
    parent_remove = test_c.find("rmdir(parent_dir)")
    check(last_child_remove >= 0 and disable_cpu > last_child_remove and
          parent_remove > disable_cpu,
          "cleanup removes children, disables cpu, then removes the parent")

    # --- placement: the handler must exist whenever cpu_dfl_files is compiled -
    m = re.search(r"static struct cftype cpu_dfl_files\[\] = \{(.*?)\n\};", core, re.S)
    check(m is not None, "found cpu_dfl_files")
    if m:
        check(".seq_show = cpu_dfl_stats_show" in m.group(1),
              "cpu_dfl_files' stat entry uses the v2 handler")
        check("throttled_time" not in m.group(1),
              "cpu_dfl_files does not still point at the v1-only name")
    # The v2 handler must be defined after the closing of the big CFS_BANDWIDTH
    # block, otherwise its own #ifdef would be unreachable and the function
    # would vanish when CONFIG_CFS_BANDWIDTH=n.
    band_close = core.index("#endif /* CONFIG_CFS_BANDWIDTH */")
    handler = core.index("static int cpu_dfl_stats_show")
    check(handler > band_close,
          "cpu_dfl_stats_show sits outside the CONFIG_CFS_BANDWIDTH block")
    check(core.index("static struct cftype cpu_dfl_files[]") > handler,
          "cpu_dfl_stats_show is defined before it is referenced")


def check_memcg_reclaim():
    print("memory.reclaim: upstream reclaim interface and swappiness override")
    mem = read(MEMCONTROL_C)
    vmscan = read(os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "mm", "vmscan.c"))
    swap_h = read(os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "include", "linux",
                               "swap.h"))
    doc = read(os.path.join(ROOT, "kernel", "xiaomi", "mt6893", "Documentation",
                            "cgroup-v2.txt"))
    tp = read(TASK_PROFILES_CPP)

    check("static ssize_t memory_reclaim(" in mem,
          "memcontrol implements the upstream write handler")
    reclaim_entry = mem.find('.name = "reclaim"')
    check(reclaim_entry >= 0 and "CFTYPE_NOT_ON_ROOT" in mem[reclaim_entry:reclaim_entry + 180],
          "memory.reclaim is exposed only on non-root memory cgroups")
    check('"swappiness=%d"' in mem and "memparse(buf, &buf) / PAGE_SIZE" in mem,
          "the upstream token parser accepts a byte target and nested swappiness key")
    check("swappiness < 0 || swappiness > 200" in mem,
          "per-call swappiness is constrained to 0..200")
    check("MEM_CGROUP_RECLAIM_RETRIES" in mem and "lru_add_drain_all()" in mem,
          "reclaim retries and drains LRU additions before reporting shortfall")
    check("!reclaimed && !nr_retries--" in mem and "return -EAGAIN;" in mem,
          "reclaim exhaustion returns EAGAIN as Android expects")
    check("try_to_free_mem_cgroup_pages(memcg," in mem and "swappiness < 0 ? NULL" in mem,
          "the handler passes only an explicit override to the reclaim API")
    check("int *swappiness);" in swap_h and
          "int *swappiness)" in vmscan and ".proactive_swappiness = swappiness" in vmscan,
          "the upstream per-call pointer is carried through scan_control")
    check("sc_swappiness(sc, memcg)" in vmscan and "return *sc->proactive_swappiness;" in vmscan,
          "classic and LRU_GEN paths share the upstream policy helper")
    check('same semantics as vm.swappiness' in doc and "-EAGAIN" in doc,
          "the local cgroup v2 documentation matches upstream semantics")
    check("swappiness=0" in tp and "swappiness=200" in tp,
          "Android libprocessgroup uses supported swappiness values")
    jni = read(os.path.join(ROOT, "frameworks", "base", "services", "core", "jni",
                            "com_android_server_am_CachedAppOptimizer.cpp"))
    check('std::ofstream reclaim_file("/sys/fs/cgroup/system/memory.reclaim")' in jni,
          "CachedAppOptimizer consumes system memory.reclaim")


def check_vr_profiles_untouched():
    print("VR capacity profiles left alone (no consumer, no inferable mask)")
    stock = load(STOCK_PROFILES)
    names = {p["Name"] for p in stock.get("Profiles", [])}
    vr = ("VrProcessCapacityLow", "VrProcessCapacityNormal", "VrProcessCapacityHigh",
          "VrServiceCapacityLow", "VrServiceCapacityNormal", "VrServiceCapacityHigh")
    for n in vr:
        check(n in names, "%s still exists in the shared AOSP file (unmodified)" % n)
    ours_p = profile_map(load(TASK_PROFILES_JSON))
    check(not any(n in ours_p for n in vr),
          "the device overlay does not redefine any VR profile")
    aggs = " ".join(" ".join(a["Profiles"]) for a in load(TASK_PROFILES_JSON)
                    .get("AggregateProfiles", []))
    check("Vr" not in aggs, "the device overlay references no VR aggregate")


def check_cpuset_partitions_absent():
    print("cpuset partitions: absent in this tree, not backported")
    src = read(CPUSET_C)
    for f in ("cpus.exclusive", "cpus.partition", "cpus.isolated"):
        check(f not in src, "no %s cftype was invented" % f)
    check("cpuset_partition" not in src, "no partition bookkeeping was added")


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
               check_cpuset_abi_names, check_memcg_compaction_gate,
               check_cpu_stat_v2_abi, check_memcg_reclaim,
               check_vr_profiles_untouched, check_cpuset_partitions_absent,
               check_defconfig,
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