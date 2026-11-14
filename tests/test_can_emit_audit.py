"""K12：can_emit 全角色逐字对账（第四十轮）。

第三十九轮只给 `roles.lead.seat.can_emit` 做了镜像对账，其余 18 个 actor
仍只有 K6 的「集合比对」：集合会吞掉重复项、不查事件号是否真在 spec、
不打印被检查了几个 actor —— 「通过了」与「没跑」在输出上等价。

本测试把判据当纯函数验：喂进去的是 agents.json 原有三张表的**副本**，
不往生产里塞第二份真相。四条红路径都必须真红，一条绿路径不许假绿。
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

R = []


def ok(name, cond, got=None):
    R.append(bool(cond))
    print("%-58s %s%s" % (name, "ok" if cond else "FAIL",
                          "" if got is None else "  got=%r" % (got,)))


def load():
    with open(os.path.join(WB, "spec", "events.schema.json"), encoding="utf-8") as fh:
        ev = json.load(fh)
    with open(os.path.join(WB, "roles", "agents.json"), encoding="utf-8") as fh:
        ag = json.load(fh)
    by_id = {e["id"]: e for e in ev["events"]}
    return ag, by_id, ev


def main():
    import constitution_check as cc

    ag, by_id, ev = load()

    # ── 1. 生产真值：全绿，且 NOTE 报出被检查的 actor 数 ────────────────
    note, fails = cc.can_emit_audit(ag, by_id)
    ok("生产 can_emit_audit 零失败", not fails, fails)
    ok("NOTE 写明是 K12", "NOTE K12" in note, note)
    ok("NOTE 写明 actors=19", "actors=19" in note, note)
    ok("NOTE 写明授权未使用不判红", "授权未使用不判红" in note, note)
    # 19 个 actor 的声明总数 = 各表 can_emit 长度之和（真值，不写死数字）
    want_total = (sum(len(r.get("can_emit") or []) for r in ag["roles"].values())
                  + sum(len(r.get("can_emit") or []) for r in ag["system_actors"].values())
                  + len(ag["human"]["can_emit"]))
    ok("NOTE 的 declared 与三张表长度和一致",
       "declared=%d" % want_total in note, note)

    # ── 2. 重复项必须红（K6 用 set，吞掉了；K12 用 list，抓得到） ──────
    a2 = copy.deepcopy(ag)
    a2["roles"]["coder"]["can_emit"] = ["E09", "E09"]
    n2, f2 = cc.can_emit_audit(a2, by_id)
    ok("重复项被 K12 抓住", any("有重复项" in x["detail"] for x in f2), f2)
    ok("重复项失败带角色名", any("roles.coder" in x["detail"] for x in f2), f2)

    # ── 3. 不是列表必须红 ─────────────────────────────────────────────
    a3 = copy.deepcopy(ag)
    a3["roles"]["architect"]["can_emit"] = "E29"
    n3, f3 = cc.can_emit_audit(a3, by_id)
    ok("非列表被 K12 抓住", any("不是列表" in x["detail"] for x in f3), f3)

    # ── 4. 事件号不在 spec 必须红 ──────────────────────────────────────
    a4 = copy.deepcopy(ag)
    a4["roles"]["lead"]["can_emit"] = list(ag["roles"]["lead"]["can_emit"]) + ["E99"]
    n4, f4 = cc.can_emit_audit(a4, by_id)
    ok("不存在的事件号被 K12 抓住",
       any("E99" in x["detail"] and "没有这个事件号" in x["detail"] for x in f4), f4)

    # ── 5. 声明大于权限必须红 ──────────────────────────────────────────
    # E53 persona_upgrade 的 actors 只有 human:*，给 coder 就是越权。
    e53 = by_id["E53"]
    ok("探针前提：E53 不给 coder",
       "coder" not in (e53.get("actors") or []), e53.get("actors"))
    a5 = copy.deepcopy(ag)
    a5["roles"]["coder"]["can_emit"] = ["E09", "E53"]
    n5, f5 = cc.can_emit_audit(a5, by_id)
    ok("声明大于权限被 K12 抓住",
       any("E53" in x["detail"] and "声明大于权限" in x["detail"] for x in f5), f5)

    # ── 6. 授权未使用不许假红（收敛 ≠ 越权） ───────────────────────────
    # architect 的 spec 授权含 E01/E02/E08，can_emit 只列 E29 —— 这是收敛。
    a6 = copy.deepcopy(ag)
    a6["roles"]["architect"]["can_emit"] = []
    n6, f6 = cc.can_emit_audit(a6, by_id)
    ok("清空 architect 的 can_emit 不判红", not f6, f6)
    # E08 走 *authority*，即使列了也不该被「声明大于权限」误判
    a7 = copy.deepcopy(ag)
    a7["roles"]["coder"]["can_emit"] = ["E09", "E08"]
    n7, f7 = cc.can_emit_audit(a7, by_id)
    ok("E08 *authority* 不误判", not f7, f7)

    # ── 7. 源码真值：check() 真的把 K12 接进巡检 ───────────────────────
    src = open(os.path.join(WB, "scripts", "constitution_check.py"),
               encoding="utf-8").read()
    ok("源码有 def can_emit_audit", "def can_emit_audit(" in src, True)
    ok("check() 真打印 K12 NOTE", "print(_note)" in src, True)
    ok("check() 真把 K12 失败并进结果", "fails.extend(_f12)" in src, True)
    ok("docstring 列了 K12", "K12 全角色 can_emit" in src, True)
    ok("K12 不往 agents.json 写副本", '"seat"' not in src.split("def can_emit_audit(")[1].split("def check(")[0], True)

    good = all(R)
    print("\ntest_can_emit_audit: %s (%d/%d)"
          % ("PASS" if good else "FAIL", sum(R), len(R)))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
