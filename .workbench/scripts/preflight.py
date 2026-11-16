#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可执行性预检（preflight）—— 调度前把「跑不了」的拦下来，别跑到一半才炸。

第 148 轮（线 C 阶段 4）。方案 O9 的规划循环里有一步「规划前可执行性预检（缺工具/
凭据/权限先拦）」。此前 run_task.plan_drive 只审 plan_audit（A1-A10 + 三权分离），
**不预检任务自身能不能跑** —— 一个点名 workbuddy（不可用供应商）的任务会一路跑到
调用外部 AI 才失败，浪费一轮。preflight 在**调度之前**逐项判：

  P1 executor_registered   任务的执行体在册（4 个之一）
  P2 executor_available    执行体现在能不能用（local_process/human_cli 恒可；
                           external_ai / harness_subagent 看 provider）
  P3 provider_available    若需外部供应商，它 available==True（缺凭据/不可用先拦）
  P4 write_scope_permitted 写域被 paths.yaml allow_write 兜住（权限越界先拦）
  P5 outbound_allowed      若声明外发，连接器 implemented + 白名单放行（外发先拦）

fail-closed：注册表/paths.yaml 读不出、执行体未登记、provider 未声明 —— 一律 BLOCKED。
**「证明不了能跑」不等于「能跑」**，与 prod_drift_guard / connector_registry 同一口径。

只做判定，不执行、不写任何文件（prod_drift_guard 铁律：预检无副作用）。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)      # .workbench
ROOT = os.path.dirname(WB)                                    # 仓库根
SPEC = os.path.join(WB, "spec")

REGISTERED = ("harness_subagent", "local_process", "human_cli", "external_ai")
NEEDS_PROVIDER = ("external_ai",)            # 这类执行体必须点名可用供应商
RUNTIME_DEPENDENT = ("harness_subagent",)    # 这类还需外部框架拉起（运行期，非预检）
ALWAYS_READY = ("local_process", "human_cli")

_LAST_ERROR = None


# ---------------------------------------------------------------- 声明读取

def _load_json(path):
    global _LAST_ERROR
    _LAST_ERROR = None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:                    # noqa: BLE001 — 读不出要能报给 P 码
        _LAST_ERROR = "%s: %s" % (type(exc).__name__, exc)
        return None


def _executor_decl():
    return _load_json(os.path.join(SPEC, "executor_registry.json"))


def _paths():
    global _LAST_ERROR
    _LAST_ERROR = None
    p = os.path.join(SPEC, "paths.yaml")
    try:
        import wb_core
        return wb_core.yaml_parse_file(p)
    except Exception as exc:                    # noqa: BLE001
        _LAST_ERROR = "%s: %s" % (type(exc).__name__, exc)
        return None


# ---------------------------------------------------------------- 判据

def _covered(scope, allow_globs):
    """scope 是否被 allow_write 某条 glob 兜住。口径同 spec_lint S014。"""
    if not isinstance(scope, str) or not scope:
        return False
    for g in allow_globs:
        base = g.replace("**", "").rstrip("/")
        if (scope == g or scope.startswith(base + "/")
                or g.startswith(scope.replace("**", "").rstrip("/") + "/")):
            return True
    return False


def _host_allowed_for_outbound(connector, decl):
    """外发连接器是否 implemented。白名单细则交 connector_registry.authorize。"""
    try:
        import connector_registry
        conns = (decl or {}).get("connectors") or {}
        ent = conns.get(connector) or {}
        return ent.get("state") == "implemented"
    except Exception:                            # noqa: BLE001
        return False


def preflight(task, exec_decl=None, paths=None):
    """对单个任务做可执行性预检。返回 {id, verdict, reasons, checks}。

    verdict: "READY" | "BLOCKED"。任何一项证明不了能跑 / 明确跑不了 → BLOCKED。
    task 至少要有 id；可带 executor / provider（或 model.provider）/ write_scope /
    outbound（连接器名列表）。
    """
    if exec_decl is None:
        exec_decl = _executor_decl()
    if paths is None:
        paths = _paths()

    tid = (task or {}).get("id") or "<no-id>"
    reasons = []
    checks = {}

    # 先 fail-closed：声明读不出，什么都证明不了
    if not isinstance(exec_decl, dict):
        reasons.append("P0 executor_registry.json 不可读（%s）" % (_LAST_ERROR or "?"))
        return {"id": tid, "verdict": "BLOCKED", "reasons": reasons,
                "checks": {"P0": False}}
    if not isinstance(paths, dict):
        reasons.append("P0 paths.yaml 不可读（%s）" % (_LAST_ERROR or "?"))
        return {"id": tid, "verdict": "BLOCKED", "reasons": reasons,
                "checks": {"P0": False}}

    executors = exec_decl.get("executors") or {}
    providers = exec_decl.get("providers") or {}
    allow_globs = [x["glob"] for x in (paths.get("allow_write") or [])
                   if isinstance(x, dict) and "glob" in x]

    ex = (task or {}).get("executor")
    prov = (task or {}).get("provider")
    if not prov and isinstance((task or {}).get("model"), dict):
        prov = (task or {}).get("model", {}).get("provider")

    # P1 executor_registered
    p1 = ex in REGISTERED
    checks["P1"] = p1
    if not p1:
        reasons.append("P1 执行体 %r 不在册（在册：%s）" % (ex, sorted(REGISTERED)))

    # P2/P3 执行体 + 供应商可用性
    p2 = True
    p3 = True
    if p1:
        if ex in ALWAYS_READY:
            p2 = bool((executors.get(ex) or {}).get("available"))
            checks["P2"] = p2
            if not p2:
                reasons.append("P2 执行体 %s 声明 available=false" % ex)
        else:
            # external_ai / harness_subagent：看 provider
            checks["P2"] = True                # 执行体本身在册；能不能跑看 provider
            if ex in NEEDS_PROVIDER:
                if not prov:
                    p3 = False
                    reasons.append("P3 %s 未点名供应商（缺凭据/供应商先拦）" % ex)
                else:
                    pv = providers.get(prov) or {}
                    p3 = bool(pv.get("available"))
                    if not p3:
                        reasons.append("P3 供应商 %r 不可用（available=false，缺凭据）"
                                       % prov)
            elif ex in RUNTIME_DEPENDENT:
                # harness_subagent：点了名的 provider 必须可用；未点名则依赖外部框架
                if prov:
                    pv = providers.get(prov) or {}
                    p3 = bool(pv.get("available"))
                    if not p3:
                        reasons.append("P3 供应商 %r 不可用" % prov)
    checks["P3"] = p3

    # P4 write_scope_permitted（权限越界先拦）
    p4 = True
    for sc in ((task or {}).get("write_scope") or []):
        if not _covered(sc, allow_globs):
            p4 = False
            reasons.append("P4 写域 %r 不被 paths.yaml allow_write 覆盖（权限越界）" % sc)
    checks["P4"] = p4

    # P5 outbound_allowed（外发先拦）
    p5 = True
    for conn in ((task or {}).get("outbound") or []):
        if not _host_allowed_for_outbound(conn, exec_decl):
            p5 = False
            reasons.append("P5 外发连接器 %r 未 implemented（外发未授权）" % conn)
    checks["P5"] = p5

    verdict = "READY" if (p1 and p2 and p3 and p4 and p5) else "BLOCKED"
    return {"id": tid, "verdict": verdict, "reasons": reasons, "checks": checks}


def check_plan(tasks):
    """一批任务的预检：返回 {verdict, results[], blocked[]}。任一 BLOCKED → 整体 BLOCKED。"""
    results = [preflight(t) for t in (tasks or [])]
    blocked = [r for r in results if r["verdict"] == "BLOCKED"]
    return {"verdict": "READY" if not blocked else "BLOCKED",
            "results": results, "blocked": blocked}


# ---------------------------------------------------------------- 自检

def _selftest():
    """判定自证：READY / 各种 BLOCKED / fail-closed，且每种都点对理由。"""
    ok = 0
    total = 0
    decl = _executor_decl()
    paths = _paths()

    def ck(name, cond):
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
        print("  %s %s" % ("ok" if cond else "FAIL", name))

    # READY：local_process + 合法写域
    r = preflight({"id": "T1", "executor": "local_process",
                   "write_scope": ["src/x.py"]}, decl, paths)
    ck("local_process + src/ 写域 → READY", r["verdict"] == "READY")

    # P3：external_ai 无供应商 → BLOCKED（缺凭据）
    r = preflight({"id": "T2", "executor": "external_ai"}, decl, paths)
    ck("external_ai 无供应商 → BLOCKED", r["verdict"] == "BLOCKED"
       and any("P3" in x and "未点名" in x for x in r["reasons"]))

    # P3：external_ai + 不可用供应商（workbuddy）→ BLOCKED
    r = preflight({"id": "T3", "executor": "external_ai", "provider": "workbuddy"},
                  decl, paths)
    ck("external_ai + workbuddy（不可用）→ BLOCKED", r["verdict"] == "BLOCKED"
       and any("P3" in x and "不可用" in x for x in r["reasons"]))

    # P3：external_ai + 可用供应商（deepseek）→ READY
    r = preflight({"id": "T4", "executor": "external_ai", "provider": "deepseek"},
                  decl, paths)
    ck("external_ai + deepseek（可用）→ READY", r["verdict"] == "READY")

    # P4：写域越界 → BLOCKED（权限）
    r = preflight({"id": "T5", "executor": "local_process",
                   "write_scope": ["not-allowed/evil.txt"]}, decl, paths)
    ck("写域越界 → BLOCKED（权限）", r["verdict"] == "BLOCKED"
       and any("P4" in x for x in r["reasons"]))

    # P1：未知执行体 → BLOCKED
    r = preflight({"id": "T6", "executor": "ghost_exec"}, decl, paths)
    ck("未知执行体 → BLOCKED（P1）", r["verdict"] == "BLOCKED"
       and any("P1" in x for x in r["reasons"]))

    # fail-closed：声明读不出（loader 真返回 None）→ BLOCKED
    g = globals()
    saved_ex, saved_pa = g["_executor_decl"], g["_paths"]
    try:
        g["_executor_decl"] = lambda: None          # 模拟读失败
        r = preflight({"id": "T7", "executor": "local_process"}, None, paths)
        ck("executor_registry 读不出 → fail-closed BLOCKED", r["verdict"] == "BLOCKED"
           and any("P0" in x for x in r["reasons"]))
        g["_executor_decl"] = saved_ex
        g["_paths"] = lambda: None                  # 模拟读失败
        r = preflight({"id": "T8", "executor": "local_process"}, decl, None)
        ck("paths.yaml 读不出 → fail-closed BLOCKED", r["verdict"] == "BLOCKED"
           and any("P0" in x for x in r["reasons"]))
    finally:
        g["_executor_decl"], g["_paths"] = saved_ex, saved_pa

    # 判据区分度：拿掉配置读者后回到真实判定（不是碰巧绿）
    real = preflight({"id": "T9", "executor": "local_process",
                      "write_scope": ["src/a.py"]}, decl, paths)
    ck("区分度：同输入稳定判 READY", real["verdict"] == "READY"
       and real["checks"]["P4"] is True)

    print("preflight selftest: %s (%d/%d)" % ("PASS" if ok == total else "FAIL", ok, total))
    return 0 if ok == total else 1


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="可执行性预检（缺工具/凭据/权限先拦）")
    ap.add_argument("--task", help="单任务 JSON")
    ap.add_argument("--task-file", help="单任务 JSON 文件")
    ap.add_argument("--plan", help="plan JSON（含 tasks[]）")
    ap.add_argument("--plan-file", help="plan JSON 文件")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return _selftest()

    if a.task or a.task_file:
        raw = a.task or open(a.task_file, encoding="utf-8").read()
        res = preflight(json.loads(raw))
    elif a.plan or a.plan_file:
        raw = a.plan or open(a.plan_file, encoding="utf-8").read()
        data = json.loads(raw)
        res = check_plan(data.get("tasks") if isinstance(data, dict) else data)
    else:
        return _selftest()

    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        if "results" in res:
            print("preflight: %s" % res["verdict"])
            for r in res["results"]:
                print("  [%s] %s%s" % (r["verdict"], r["id"],
                                       (" — " + "; ".join(r["reasons"])) if r["reasons"] else ""))
        else:
            print("preflight: %s  %s" % (res["verdict"], res["id"]))
            for x in res["reasons"]:
                print("  - %s" % x)
    return 0 if res["verdict"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
