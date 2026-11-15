#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_plan_drive.py — 多任务 DAG 驱动防回归（附录 M / 第十五轮开放项①）。

钉住的事：
  1. --plan dry 真读 plan/plan_audit/handoff/enums，输出 waves/tasks/ran 且 rc=0
  2. team_protocol.audit_plan / validate_handoff 在驱动里被真调用（monkeypatch 计数）
  3. 缺 A4 / plan_audit 缺 A4 覆盖 / 三权未分离 / 环 / 软依赖被硬阻塞 → REFUSE 非 0
  4. DAG 只按 depends_on / hard_dependencies 连边：软依赖不阻塞主链、环与悬空被检出
  5. main() 遇到 --plan 真分流到 plan_drive（不进单任务 store 逻辑）

全程 tempfile.mkdtemp() 隔离，rt.WB 指到临时目录，不写生产链 .workbench/events/**。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, ".workbench", "scripts"))

import run_task as rt  # noqa: E402
import team_protocol as tp  # noqa: E402

import atexit
import shutil
import tempfile


def _rmtree(path):
    """删一棵树，只读文件也删。

    为什么不直接 `shutil.rmtree(path, True)`：Windows 上 `.git/objects/**`
    里是只读文件，`rmtree` 遇到只读项会**静默失败并留下整棵树**（实测改完
    单跑 test_revert_boundary.py 仍有 3 个目录各 19-20 项留下，只读文件
    全在 .git 下）。先删一次，删不掉就摘掉只读位再删。
    """
    shutil.rmtree(path, ignore_errors=True)
    if not os.path.exists(path):
        return
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                os.chmod(os.path.join(root, fn), 0o777)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _mkdtemp(prefix):
    """建临时目录，并在进程退出时删掉。

    为什么不用裸 `tempfile.mkdtemp(...)`：整轮回归由 `run_all_checks.sh` 的
    TEMP 沙箱兜底，但**单跑这一个文件时没有任何东西负责清理**——2026-11-15
    一天里 %TEMP% 攒下 264 个 `wb_*` 目录 / 17.5 MB，实测单跑
    `test_scheduler.py` 一次就新增 7 个。沙箱管「整轮」，管不到「随手跑一个」。
    """
    d = tempfile.mkdtemp(prefix=prefix)
    atexit.register(_rmtree, d)
    return d


OKS = 0
FAILS = []
CALLS = {"audit_plan": 0, "validate_handoff": 0}
_ORIG_AUDIT = tp.audit_plan
_ORIG_HANDOFF = tp.validate_handoff
_ORIG_WB = rt.WB
_EV = None  # 临时目录里真实存在的证据文件（绝对路径）


def ok(name, cond, detail=""):
    global OKS
    if cond:
        OKS += 1
        print("  ok  %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s  %s" % (name, detail))


# ---------------------------------------------------------------- 夹具
def _contract():
    """只读生产 enums.json 的 team_protocol 段，写进临时 enums 保真。"""
    with open(os.path.join(ROOT, ".workbench", "spec", "enums.json"),
              encoding="utf-8") as fh:
        return json.load(fh)["team_protocol"]


def _task(tid, scope, dep=None, soft=None, is_soft=False):
    acc = {"cmd": "python tests/test_%s.py" % tid.lower(),
           "exit_code": 0, "evidence": "evidence/%s.txt" % tid}
    if tid == "T-A":
        acc["guard"] = "不改验收口径"  # A1 硬约束的任务级承接物
    return {
        "id": tid,
        "write_scope": scope,
        "depends_on": dep or [],
        "soft_dependencies": soft or [],
        "soft_dependency": is_soft,
        "acceptance": acc,
        "contract_ref": "contracts/api.md" if dep else None,
        "base": "HEAD",
        "rollback": "update-ref + checkout base -- <touched>",
        "budget_min": 45,
    }


def _plan():
    """合规 plan：2 个无依赖任务 + 1 个依赖任务（另声明 1 条软依赖）。"""
    return {
        "intent": {"hard_constraints": ["不改验收口径"],
                   "objective": "并行落地两个模块 + 一个依赖任务"},
        "tasks": [
            _task("T-A", ["src/a.py"]),
            _task("T-B", ["src/b.py"]),
            _task("T-C", ["src/c.py"], dep=["T-A"], soft=["T-B"]),
        ],
        "spec_refs": ["role:lead", "role:verifier"],
        "open_items": [{"id": "R-40", "text": "token 账本待采样"}],
        "target_level": "A",
    }


def _audit():
    return {"result": "PASS", "producer": "architect", "auditor": "auditor",
            "dispatcher": "lead",
            "checks": {"A%d" % i: "pass" for i in range(1, 11)}}


def _handoff(evidence_abs):
    return {"task": "T-C", "produced": ["src/c.py"],
            "consumed": ["contracts/api.md"],
            "contract_ref": "contracts/api.md",
            "evidence": [evidence_abs + "@exit=0"],
            "decisions": ["整数半点进位"], "open_questions": [],
            "context_refs": ["seq=165"]}


def _write(tmp, name, obj):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    return p


def _setup(tmp):
    global _EV
    os.makedirs(os.path.join(tmp, "spec"), exist_ok=True)
    _write(tmp, os.path.join("spec", "enums.json"),
           {"team_protocol": _contract()})
    _EV = os.path.join(tmp, "evidence.txt").replace("\\", "/")
    with open(_EV, "w", encoding="utf-8") as fh:
        fh.write("ran: T-A T-B T-C exit=0\n")
    return _EV


def _drive(tmp, plan=None, audit=None, handoff=None):
    """写三个 json + 直接调 plan_drive(dry=True)，捕获 stdout。"""
    plan = _plan() if plan is None else plan
    audit = _audit() if audit is None else audit
    handoff = _handoff(_EV) if handoff is None else handoff
    pp = _write(tmp, "plan.json", plan)
    ap = _write(tmp, "plan_audit.json", audit)
    hp = _write(tmp, "handoff.json", handoff)
    a = argparse.Namespace(plan=pp, plan_audit=ap, plan_handoff=hp,
                           plan_dry=True, dry=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = rt.plan_drive(a, None)
    return rc, buf.getvalue()


def _install_spies():
    """计数 wrapper 仍调原函数：findings 真实生效，不为了让测试过而放宽。"""
    def counting_audit(plan, c):
        CALLS["audit_plan"] += 1
        return _ORIG_AUDIT(plan, c)

    def counting_handoff(h, c, wb=None):
        CALLS["validate_handoff"] += 1
        return _ORIG_HANDOFF(h, c, wb=wb)

    tp.audit_plan = counting_audit
    tp.validate_handoff = counting_handoff


# ---------------------------------------------------------------- 1) 合规 dry
def t_happy_path(tmp):
    CALLS["audit_plan"] = 0
    CALLS["validate_handoff"] = 0
    rc, out = _drive(tmp)
    ok("合规 plan dry 返回 0", rc == 0, (rc, out[-600:]))
    ok("输出含 PLAN_DRIVE PASS", "PLAN_DRIVE PASS" in out, out[-600:])
    ok("输出含 waves=2", "waves=2" in out, out[-600:])
    ok("输出含 tasks=3", "tasks=3" in out, out[-600:])
    ok("输出含 ran: 行", "ran:" in out, out[-600:])
    ok("输出逐 wave 打印任务 ran: T-A / T-C",
       "ran: T-A" in out and "ran: T-C" in out, out[-600:])
    ok("输出含软依赖标记 soft=", "soft=" in out, out[-600:])
    ok("输出标明 dry 未写链", "dry" in out.lower(), out[-600:])
    ok("team_protocol.audit_plan 真被调用",
       CALLS["audit_plan"] == 1, CALLS)
    ok("team_protocol.validate_handoff 真被调用",
       CALLS["validate_handoff"] >= 1, CALLS)
    ok("输出含 audit_plan ran 行", "audit_plan(plan, contract)" in out)
    ok("输出含 validate_handoff ran 行", "validate_handoff(...)" in out)
    ok("驱动源码探针 6 标记全真", tp._driver_probe()["wired"] is True,
       tp._driver_probe()["markers"])


# ---------------------------------------------------------------- 2) 失败注入
def t_failures(tmp):
    # 缺 A4（验收缺 evidence）→ audit_plan findings
    p = _plan()
    p["tasks"][2]["acceptance"].pop("evidence")
    rc, out = _drive(tmp, plan=p)
    ok("缺 A4 验收 evidence → 非 0", rc != 0, rc)
    ok("缺 A4 输出 PLAN_DRIVE REFUSE", "PLAN_DRIVE REFUSE" in out, out[-400:])
    ok("缺 A4 findings 真打印 A4", "A4" in out, out[-400:])

    # plan_audit.checks 缺 A4 覆盖 → _plan_check_audit 拦
    bad = _audit()
    bad["checks"].pop("A4")
    rc, out = _drive(tmp, audit=bad)
    ok("plan_audit 缺 A4 覆盖 → 非 0", rc != 0, rc)
    ok("plan_audit 缺 A4 输出未覆盖", "未覆盖" in out, out[-400:])

    # auditor == producer → 三权未分离
    bad = _audit()
    bad["auditor"] = "architect"
    rc, out = _drive(tmp, audit=bad)
    ok("auditor 等于 producer → 非 0", rc != 0, rc)
    ok("三权未分离被点明", "三权未分离" in out, out[-400:])

    # result != PASS
    bad = _audit()
    bad["result"] = "FAIL"
    rc, out = _drive(tmp, audit=bad)
    ok("plan_audit.result != PASS → 非 0", rc != 0, rc)
    ok("result 不合规被点明", "result" in out, out[-400:])

    # 环：T-A 依赖 T-C，T-C 本就依赖 T-A
    cyc = _plan()
    cyc["tasks"][0]["depends_on"] = ["T-C"]
    rc, out = _drive(tmp, plan=cyc)
    ok("依赖成环 → 非 0", rc != 0, rc)
    ok("环被 audit_plan 点明", "环" in out, out[-400:])

    # 悬空依赖
    dang = _plan()
    dang["tasks"][0]["depends_on"] = ["T-NOPE"]
    rc, out = _drive(tmp, plan=dang)
    ok("悬空依赖 → 非 0", rc != 0, rc)
    ok("悬空被点明", "悬空" in out, out[-400:])

    # 软依赖被硬阻塞：把 soft_dependency 任务写进 depends_on
    soft = _plan()
    soft["tasks"][1]["soft_dependency"] = True
    soft["tasks"][2]["depends_on"] = ["T-A", "T-B"]
    rc, out = _drive(tmp, plan=soft)
    ok("软依赖被硬阻塞 → 非 0", rc != 0, rc)
    ok("重新引入串行栅栏被点明",
       "软依赖" in out and "栅栏" in out, out[-400:])

    # handoff evidence 指向不存在文件 → validate_handoff 拦
    badh = _handoff(_EV)
    badh["evidence"] = [os.path.join(tmp, "nope.txt")]
    rc, out = _drive(tmp, handoff=badh)
    ok("handoff 证据不存在 → 非 0", rc != 0, rc)
    ok("handoff 不合规被点明", "handoff" in out.lower(), out[-400:])

    # 单任务参数不回归：无 --plan 时 a.plan 为 None（解析层）
    a = rt.main  # 仅确认未破坏原入口
    ok("main 入口仍在（单任务路径未删）", callable(a))


# ---------------------------------------------------------------- 3) DAG 单元
def t_dag_units():
    p = _plan()
    by_id, hard_deps, errs = rt._plan_dag(p)
    ok("_plan_dag 合规方案无 errs", errs == [], errs)
    ok("硬依赖只连 depends_on", hard_deps["T-C"] == ["T-A"], hard_deps)
    ok("soft_dependencies 不进主链连边",
       "T-B" not in hard_deps["T-C"], hard_deps)
    waves, werrs = rt._plan_waves(by_id, hard_deps)
    ok("合规 DAG 分 2 wave 且无错", len(waves) == 2 and werrs == [],
       (waves, werrs))
    ok("wave1 含两个无依赖任务", set(waves[0]) == {"T-A", "T-B"}, waves)
    ok("wave2 只含依赖任务", waves[1] == ["T-C"], waves)

    cyc = _plan()
    cyc["tasks"][0]["depends_on"] = ["T-C"]
    by2, hd2, _ = rt._plan_dag(cyc)
    _, we2 = rt._plan_waves(by2, hd2)
    ok("_plan_waves 检出环", bool(we2), we2)

    dang = _plan()
    dang["tasks"][0]["depends_on"] = ["T-NOPE"]
    _, _, er3 = rt._plan_dag(dang)
    ok("_plan_dag 检出悬空依赖", any("悬空" in e for e in er3), er3)

    alt = _plan()
    alt["tasks"][2].pop("depends_on")
    alt["tasks"][2]["hard_dependencies"] = ["T-A"]
    _, hd4, _ = rt._plan_dag(alt)
    ok("hard_dependencies 亦进主链", hd4["T-C"] == ["T-A"], hd4)

    soft = _plan()
    soft["tasks"][1]["soft_dependency"] = True
    soft["tasks"][2]["depends_on"] = ["T-A", "T-B"]
    _, hd5, _ = rt._plan_dag(soft)
    ok("soft_dependency 目标任务不进硬连边",
       hd5["T-C"] == ["T-A"], hd5)

    dup = _plan()
    dup["tasks"][1]["id"] = "T-A"
    _, _, er6 = rt._plan_dag(dup)
    ok("_plan_dag 检出重复 id", any("重复" in e for e in er6), er6)


# ---------------------------------------------------------------- 4) main 分流
def t_main_dispatch(tmp):
    sentinel = object()
    seen = {}
    orig_store, orig_pd = rt.store, rt.plan_drive
    rt.store = lambda: sentinel

    def fake_pd(a, st):
        seen["a"] = a
        seen["st"] = st
        return 7

    rt.plan_drive = fake_pd
    try:
        rc = rt.main(["--plan", os.path.join(tmp, "plan.json"), "--plan-dry"])
    finally:
        rt.store, rt.plan_drive = orig_store, orig_pd
    ok("main --plan 返回 plan_drive 的返回码", rc == 7, rc)
    ok("main --plan 真分流到 plan_drive", "a" in seen, seen)
    ok("main --plan 把 store() 结果传给驱动", seen.get("st") is sentinel)
    ok("main --plan 解析 plan_dry=True",
       seen.get("a") is not None and seen["a"].plan_dry is True)
    ok("main --plan 参数带 --plan 路径",
       seen.get("a") is not None and seen["a"].plan.endswith("plan.json"))
    ok("main --plan 单任务字段保持默认（未破坏）",
       seen.get("a") is not None and seen["a"].resume is False)


def main():
    global _EV
    tmp = _mkdtemp(prefix="tp-plandrive-")
    try:
        _setup(tmp)
        rt.WB = tmp                 # 驱动的 enums / config / 证据基准都落临时目录
        _install_spies()
        for fn in (t_happy_path, t_failures, t_main_dispatch):
            print("[%s]" % fn.__name__)
            fn(tmp)
        print("[t_dag_units]")
        t_dag_units()
    finally:
        tp.audit_plan = _ORIG_AUDIT
        tp.validate_handoff = _ORIG_HANDOFF
        rt.WB = _ORIG_WB
        shutil.rmtree(tmp, ignore_errors=True)
    print("test_plan_drive: %s (%d/%d)" %
          ("PASS" if not FAILS else "FAIL", OKS, OKS + len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  - %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
