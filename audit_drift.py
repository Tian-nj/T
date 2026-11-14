"""audit_drift.py — 运行期视图 vs 磁盘内容：spec_tables() 与 spec/*.json 是否已漂移。

这个检查的来历：v3.2 早期，wb_core.py 里硬编码过一份 spec_tables()（728 行文件
的最后 80 行），于是仓里同时存在两个权威源 —— 正是 v3.1 的病，被 v3.2 以自己的
方式复刻了。**那份副本已经删掉**，spec_tables() 现在直接读 spec/*.json。
所以本脚本今天比的是**同一份文件的两种读法**：运行期（带 mtime 缓存）与磁盘内容。

留着它，是因为"删掉副本"这个动作本身不会自我验证：缓存失效写错、路径拼错、
读取顺序变了 —— 任何一处都会让运行期视图悄悄停留在旧内容上，而那正是
「两份真相」的现代形态。一致 → NO DRIFT；不一致 → DRIFT，说明读侧有 bug。

本脚本逐字段比对：
  A. 事件表：E## ↔ kind，编号集合、kind、退休标记
  B. 状态表：12 态集合
  C. 迁移表：T## ↔ (from,to,authority) 逐条
  D. reason_code 集合
  E. actor 集合
  F. 冻结白名单
任何一项不等 → 报 DRIFT，exit 1。

**不比 per-event `actors`**：那一项归 audit_closure 的 C11 管。这里的职责是
「两份表是否一致」，C11 的职责是「规则有没有被执行」—— 两件事共用一张表，
将来任何一边改动都会让另一边的断言变成假绿。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".workbench/scripts")

WB = ".workbench"
SPEC = os.path.join(WB, "spec")


def jload(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_core():
    """从 wb_core 取它自己那份 spec_tables()。"""
    import wb_core  # noqa

    return wb_core.spec_tables()


def cmp_set(label, a, b, findings):
    a, b = set(a), set(b)
    if a != b:
        findings.append(
            {
                "label": label,
                "missing_in_core": sorted(b - a),
                "extra_in_core": sorted(a - b),
            }
        )
        return False
    return True


def main() -> int:
    core = load_core()
    ev = jload(os.path.join(SPEC, "events.schema.json"))
    st = jload(os.path.join(SPEC, "states.json"))
    en = jload(os.path.join(SPEC, "enums.json"))

    findings = []

    # v2 修正：v1 用错了键名，把"我读不到"误报成"人家不一致"。
    # 真实结构（实测）：
    #   events.schema.json → events[].{id,kind,retired,writable_in_frozen}
    #                        retired_ids[], counts{}
    #                        冻结白名单**没有**顶层列表，由 writable_in_frozen 派生
    #   states.json        → states[], transitions[].{id,from,to,authority,carrier}
    #   enums.json         → reason_code[].code（不是 reason_codes）、actor（单数，不是 actors）
    # 这正是本仓反复踩的"读错源、不报错、只撒谎"。

    # ---- A 事件表 ----
    core_ev = {eid: kind for eid, kind in core["events"]}
    spec_ev = {e["id"]: e["kind"] for e in ev["events"]}
    cmp_set("事件编号集合", set(core_ev), set(spec_ev), findings)
    kind_drift = [
        (k, core_ev[k], spec_ev[k]) for k in sorted(set(core_ev) & set(spec_ev)) if core_ev[k] != spec_ev[k]
    ]
    if kind_drift:
        findings.append({"label": "事件 kind 不一致", "rows": kind_drift})

    cmp_set("退休事件集合", core["retired"], set(ev.get("retired_ids", [])), findings)

    # 冻结白名单：spec 侧由 writable_in_frozen 派生
    spec_frozen = {e["id"] for e in ev["events"] if e.get("writable_in_frozen")}
    cmp_set("冻结白名单（由 writable_in_frozen 派生）", core["frozen_writable"], spec_frozen, findings)

    declared = ev.get("counts") or {}
    if declared.get("total") != len(core_ev):
        findings.append({"label": "事件总数", "core": len(core_ev), "schema_counts_total": declared.get("total")})
    if declared.get("retired") != len(core["retired"]):
        findings.append({"label": "退休数", "core": len(core["retired"]), "schema": declared.get("retired")})

    # ---- B 状态表 ----
    cmp_set("状态集合", core["states"], set(st.get("states", [])), findings)

    # ---- C 迁移表 ----
    core_tr = {t[0]: (t[1], t[2], tuple(sorted(t[4]))) for t in core["transitions"]}
    spec_tr = {
        t["id"]: (t.get("from"), t.get("to"), tuple(sorted(t.get("authority", []))))
        for t in st.get("transitions", [])
    }
    cmp_set("迁移编号集合", set(core_tr), set(spec_tr), findings)
    tr_drift = [(k, core_tr[k], spec_tr[k]) for k in sorted(set(core_tr) & set(spec_tr)) if core_tr[k] != spec_tr[k]]
    if tr_drift:
        findings.append({"label": "迁移内容不一致", "rows": tr_drift})

    # ---- D reason_code（键是 reason_code，元素是 dict）----
    spec_rc = {r["code"] for r in en.get("reason_code", [])}
    cmp_set("reason_code 集合", core["reason_codes"], spec_rc, findings)

    # ---- E actor（键是单数 actor）----
    cmp_set("actor 集合", core["actors"], set(en.get("actor", [])), findings)

    # ---- 报告 ----
    print("=" * 78)
    print("真相源漂移审计 audit_drift.py")
    print("=" * 78)
    print("  A wb_core.spec_tables()  ← 运行期从 spec/*.json 读取（曾经的硬编码副本已删）")
    print("  B .workbench/spec/*.json ← gen_spec.py 生成物")
    print()
    print("  事件 %d / 状态 %d / 迁移 %d / reason_code %d / actor %d / 冻结 %d"
          % (len(core_ev), len(core["states"]), len(core_tr),
             len(core["reason_codes"]), len(core["actors"]), len(core["frozen_writable"])))
    print()

    if not findings:
        print("RESULT: NO DRIFT — spec_tables() 与 spec/*.json 逐项一致")
        print()
        # 这段文案曾经写着"真正的修法是删掉一份，让 spec_tables() 从
        # spec/*.json 读" —— 而那个修法**早就落地了**：spec_tables() 现在
        # 就是运行时读 spec/*.json（见 A 段描述），仓里已经没有第二份手写表。
        # 一句过时的建议比没有建议更坏：下一个人会照着它去找那份不存在的
        # 副本，找不到之后得出"审计脚本在胡说"的结论，连带不再相信它报的真问题。
        print("  说明：A 段已无独立副本，两个来源是同一份文件的运行期视图与磁盘内容。")
        print("  这次一致说明生成物与加载结果没有分叉；改 spec 后仍须重跑 gen_spec.py。")
        rc = 0
    else:
        print("RESULT: DRIFT (%d 处)" % len(findings))
        for f in findings:
            print("  [%s] %s" % ("DRIFT", f.pop("label")))
            for k, v in f.items():
                print("      %s = %s" % (k, v))
        rc = 1

    with open("evidence/audit_drift.json", "w", encoding="utf-8") as fh:
        json.dump({"findings": findings}, fh, ensure_ascii=False, indent=1, default=str)
    print()
    print("wrote evidence/audit_drift.json")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
