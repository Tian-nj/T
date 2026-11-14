# -*- coding: utf-8 -*-
"""第四十轮 ⑤：主理人（lead 角色）真派活的路径判据。

要挡住的不是「代码里有没有 lead 这个词」，而是那四处读-判-写：
cmd_run 的 exclusive / wants_spawn、cmd_finish 的补 E05 / 释放租约。
以前四处各写一份 ("coder","architect") 字面量，lead 全被排除 ——
于是 lead 派活时状态永远停在 claimed，task-submit 要 in_progress，整条
「主理人自己动手」链路断掉，而任何单测都不会红（没人拿 lead 跑过）。

真跑一次 T-R40 的链上证据在第四十轮报告 §4；本测试负责「下次别改回去」：
  · 源码真值：一份 SPAWN_ROLES 四个读者，verifier 仍排除
  · 行为：announce_worker(role=lead, pid=真值) 真写 E05 + T06
  · 不编 pid：pl["pid"] 来自参数，pid_observable 两个方向都写
  · 驱动器：run_task --role 接受 lead（--dry 不抛 SystemExit(2)）
"""
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, ROOT)

import agent_runner as ar  # noqa: E402
import diff_sandbox_check as dsc  # noqa: E402
import wb_core  # noqa: E402
import run_task as rt  # noqa: E402

FAIL = []
N = [0]


def ck(name, cond, detail=""):
    N[0] += 1
    if not cond:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))
    else:
        print("  ok   %s" % name)


def src(p):
    with io.open(p, "r", encoding="utf-8") as fh:
        return fh.read()


def fresh():
    """临时仓：spec/ + config.yaml + 空 events/（不复制生产链）。"""
    tmp = tempfile.mkdtemp(prefix="lead-dispatch-")
    wb = os.path.join(tmp, ".workbench")
    os.makedirs(os.path.join(wb, "events"))
    shutil.copytree(os.path.join(ROOT, ".workbench", "spec"),
                    os.path.join(wb, "spec"))
    shutil.copy(os.path.join(ROOT, ".workbench", "config.yaml"),
                os.path.join(wb, "config.yaml"))
    return tmp, wb


# ---------------------------------------------------------------- [1] 源码真值
def t_source_truth():
    print("[1] 源码真值：一份常量，四个读者")
    a = src(os.path.join(SCRIPTS, "agent_runner.py"))
    r = src(os.path.join(ROOT, "run_task.py"))
    ck("SPAWN_ROLES 含 lead",
       'SPAWN_ROLES = ("coder", "architect", "lead")' in a)
    ck("LEASE_ROLES 复用 SPAWN_ROLES（不抄第二份）",
       "LEASE_ROLES = SPAWN_ROLES" in a)
    head = a.split("SPAWN_ROLES =")[1].split("\n")[0]
    ck("verifier 不在 SPAWN_ROLES", '"verifier"' not in head, head)
    ck("cmd_run exclusive 用 LEASE_ROLES",
       "exclusive = a.role in LEASE_ROLES" in a)
    ck("cmd_run wants_spawn 用 SPAWN_ROLES",
       "wants_spawn = a.role in SPAWN_ROLES" in a)
    ck("cmd_finish 补 E05 用 SPAWN_ROLES",
       'if a.role in SPAWN_ROLES and cur == "claimed":' in a)
    ck("cmd_finish 释放租约用 LEASE_ROLES",
       "if a.role in LEASE_ROLES:\n        ep = None" in a)
    ck("四处旧字面量已清零",
       a.count('a.role in ("coder", "architect")') == 0,
       str(a.count('a.role in ("coder", "architect")')))
    ck("finish() 的 allow/deny 仍是 worker 专属（不误套到 lead）",
       'if role in ("coder", "architect"):' in a)
    ck("run_task --role 收 lead",
       'choices=["coder", "architect", "lead"]' in r)
    ck("运行时常量含 lead",
       ar.SPAWN_ROLES == ("coder", "architect", "lead"), str(ar.SPAWN_ROLES))
    ck("LEASE_ROLES 就是同一个对象",
       ar.LEASE_ROLES is ar.SPAWN_ROLES)


# ---------------------------------------------------------------- [2] 真写 E05
def t_lead_real_spawn():
    print("[2] 真写 E05 + T06（lead 角色，真 pid）")
    tmp, wb = fresh()
    try:
        st = wb_core.Store(wb, "0" * 32)
        tid = "T-LEAD-D"
        # 铺到 claimed：与 test_boss_report_revert.py::seed 同一套造链形状
        st.append_event("state", {"task": tid, "from": None, "to": "draft"},
                        "lead", tid)
        st.append_event("draft_approved", {"task": tid}, "lead", tid)
        st.transition(tid, "pending", "lead")
        st.append_event("claim", {"task": tid, "epoch": 1}, "lead", tid)
        st.transition(tid, "claimed", "lead")
        ck("铺链后当前态是 claimed", st.current_state(tid) == "claimed",
           str(st.current_state(tid)))

        sp, tr = ar.announce_worker(st, tid, "lead", 4321, "",
                                    spec_executor="local_process")
        pl = sp.get("payload") or {}
        ck("E05 真落行", sp.get("kind") == "spawn", str(sp.get("kind")))
        ck("E05.role = lead", pl.get("role") == "lead", str(pl.get("role")))
        ck("E05.pid 来自参数不是编的", pl.get("pid") == 4321, str(pl.get("pid")))
        ck("E05.pid_observable = True（真进程方向也写）",
           pl.get("pid_observable") is True, str(pl.get("pid_observable")))
        ck("E05.executor 写进链",
           pl.get("executor") == "local_process", str(pl.get("executor")))
        ck("E05 由 system:runner 写",
           sp.get("actor") == "system:runner", str(sp.get("actor")))
        ck("T06 迁移真发生", tr is not None and
           (tr.get("payload") or {}).get("to") == "in_progress",
           str(tr))
        ck("当前态推进到 in_progress",
           st.current_state(tid) == "in_progress", str(st.current_state(tid)))
        tampered = None
        try:
            ver = st.rebuild()
        except Exception as exc:  # noqa: BLE001
            tampered = exc
        ck("链自校验通过（没有 ChainTampered）", tampered is None, str(tampered))
        ck("rebuild 认得这个任务",
           tid in (ver or {}).get("tasks", {}), str((ver or {}).get("tasks", {}).keys()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- [3] 不编 pid
def t_no_fake_pid():
    print("[3] pid 不许编，pid_observable 两个方向都写")
    a = src(os.path.join(SCRIPTS, "agent_runner.py"))
    ck("pl[\"pid\"] 直接取 int(pid)",
       'pl = {"task": tid, "pid": int(pid), "role": role,' in a)
    ck("pid_observable 是 int(pid) > 0（不是只在 <=0 时写）",
       'pl["pid_observable"] = int(pid) > 0' in a)
    ck("announce_worker docstring 仍声明只能由起进程的一方调用",
       "只能由真正起了 worker 的那个进程做" in a)


# ---------------------------------------------------------------- [4] 驱动器收 lead
def t_run_task_accepts_lead():
    print("[4] run_task --role lead 不被 argparse 拒")
    # 只量 choices，不碰生产链：把 store()/drive() 打桩掉。
    # 不解桩的话 main() 会 store() 读真实 events/，
    # 「argparse 收不收 lead」这件事就被链上有没有这个任务污染了 ——
    # 那是另一件事，不该由本测试量。
    old = (rt.store, rt.state_of, rt.drive, rt._boss_report)
    seen = {}

    def fake_store():
        return object()

    def fake_state_of(st, tid):
        return None

    def fake_drive(a, st):
        seen["role"] = a.role
        return 0

    def fake_report(a):
        return None

    rt.store, rt.state_of = fake_store, fake_state_of
    rt.drive, rt._boss_report = fake_drive, fake_report
    rc = None
    exc = None
    try:
        rc = rt.main(["--task", "T-LEAD-DRY", "--role", "lead",
                      "--title", "dry probe", "--dry"])
    except SystemExit as e:  # argparse 拒绝才会走到这里
        exc = e
    except Exception as e:  # noqa: BLE001
        exc = e
    finally:
        rt.store, rt.state_of, rt.drive, rt._boss_report = old
    ck("--role lead 不抛 SystemExit(2)", not isinstance(exc, SystemExit),
       repr(exc))
    ck("drive 真被调用且拿到 role=lead", seen.get("role") == "lead",
       str(seen))
    ck("驱动器正常收敛 rc=0", rc == 0, repr((rc, exc)))


# ---------------------------------------------------------------- [5] 写域两把尺子
def t_lead_write_scope_two_rulers():
    """T-R40 真跑暴露的缺陷：lead 白名单含 mid-path **，不能当 task scope。

    scope 用前缀相交（scope_to_prefix 只认尾部通配），allow 用完整 glob；
    把角色白名单直接塞进 scope，check() 在见到第一个 changed 之前就抛
    "mid-path wildcard not allowed in scope"，整条链路断在 E05/E08 之后。
    另一个真缺陷：paths.yaml 的 deny 是 `{glob, reason}` dict 列表，
    check() 对 deny 只 str() 不提取，runner 以前直接传 dict 列表 ——
    "谁都不许写" 的 11 条规则全变成永不命中的字符串。
    """
    print("[5] lead 写域：task scope / role allow / deny 三处各量各的")
    a = src(os.path.join(SCRIPTS, "agent_runner.py"))
    ck("源码真把 task_scope(tid) 赋给 lead 的 scope",
       "scope = [s for s in task_scope(tid)" in a)
    ck("源码真把 spec.write_scope 赋给 lead 的 allow",
       'allows = [s for s in (spec.get("write_scope") or [])' in a)
    ck("源码真提取 deny 的 glob（不是 str(dict)）",
       "def _globs(items):" in a and "x.get(\"glob\")" in a)
    ck("check() 的 deny 走 _globs",
       "_globs(denys)" in a)
    ck("源码里写明 lead 的两把尺子理由",
       "mid-path wildcard" in a and "scope_to_prefix" in a)

    # 三道行为判据：合法路径过；task scope 外拒；deny 路径拒。
    spec = {"write_scope": [
        "docs/architecture/**",
        ".workbench/tasks/**/brief.md",
        ".workbench/tasks/**/acceptance.draft.yaml",
    ]}
    role_allow = [s for s in spec["write_scope"] if s.strip()]
    task_scope = ["docs/architecture/**"]
    paths = wb_core.yaml_parse_file(os.path.join(ROOT, ".workbench", "spec",
                                                 "paths.yaml"))
    allow = [x if isinstance(x, str) else x.get("glob")
             for x in (paths.get("allow_write") or [])]
    deny = [x if isinstance(x, str) else x.get("glob")
            for x in (paths.get("deny") or [])]
    good = ["docs/architecture/T-R40-lead-dispatch.md"]
    outside = ["src/hack.py"]
    denied = ["docs/architecture/.env"]

    g = dsc.check(task_scope, good, allow, deny, [])
    ck("合法 lead 产物过 runner 层三闸门", g["ok"], str(g))
    o = dsc.check(task_scope, outside, allow, deny, [])
    ck("task scope 外仍拒（role allow 不能盖掉任务声明）",
       not o["ok"] and any("outside task scope" in v["reason"]
                           for v in o["violations"]), str(o))
    d = dsc.check(task_scope, denied, allow, deny, [])
    ck("deny glob 真生效（dict→glob 提取后命中）",
       not d["ok"] and any("denied by spec/paths.yaml" in v["reason"]
                           for v in d["violations"]), str(d))
    # role allow 本身：mid-path ** 必须靠 glob_match，不是 scope_to_prefix。
    mp = ".workbench/tasks/T-R40/brief.md"
    ck("mid-path ** 在 allow 语义下可匹配",
       dsc.glob_match(".workbench/tasks/**/brief.md", mp), mp)
    try:
        dsc.scope_to_prefix(".workbench/tasks/**/brief.md")
        bad = False
    except ValueError:
        bad = True
    ck("同一 mid-path ** 在 scope 语义下必须抛（两把尺子不同）", bad)


def main():
    t_source_truth()
    t_lead_real_spawn()
    t_no_fake_pid()
    t_run_task_accepts_lead()
    t_lead_write_scope_two_rulers()
    print("test_lead_dispatch: %s (%d checks, %d failed)" %
          ("PASS" if not FAIL else "FAIL", N[0], len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
