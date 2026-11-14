# -*- coding: utf-8 -*-
"""第四十轮：boss_report 的历史偏离风险段只读 spec/deviations.json。

不许出现「硬编码条数 / 引用编号」这种第二份真相：
  · 表的 event 是事件号（E32），链上是 kind —— 映射从 events.schema.json 读；
  · 命中条件 = kind 对得上 + actor 逐字相等 + seq <= max_seq；
  · 表读不到 = 风险（fail-closed），不是「没有偏离」；
  · 改表（max_seq / ref / actor / criterion）汇报必须跟着变；
  · 链上超截止的同类行不混入（有界放行，不是永久豁免）。

**为什么用 FakeStore 而不是 append_event 造链**：E32 现在只能由
human_cli 签出来、且 actors 只有 human:*。R-26 那 6 条 lead 写的
acceptance_approved 是判据收紧之前的历史行，任何新的写入口都写不出来
（这正是那条判据的用途）。要测「渲染层怎么读这些历史行」，只能把
预构造的行喂给只读的 render()，不能用写入口重新制造历史。
生产链那一段仍用真 Store 复核。
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

import boss_report  # noqa: E402
import wb_core      # noqa: E402

UUID = "0" * 32
OK = [0]
FAIL = []


def ok(name, cond, detail=""):
    if cond:
        OK[0] += 1
        print("  ok   %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL %s   %s" % (name, detail))


class FakeStore:
    """只读替身：root 指到临时仓的 .workbench，_rows() 返回预构造链行。"""

    def __init__(self, root, rows):
        self.root = root
        self._data = list(rows)

    def _rows(self):
        return iter(self._data)


def fresh():
    tmp = tempfile.mkdtemp(prefix="wb-br-dev-")
    wb = os.path.join(tmp, ".workbench")
    os.makedirs(os.path.join(wb, "events"), exist_ok=True)
    shutil.copytree(os.path.join(WB, "spec"), os.path.join(wb, "spec"))
    shutil.copy2(os.path.join(WB, "config.yaml"), os.path.join(wb, "config.yaml"))
    return tmp, wb


def line(seq, kind, actor="lead", tid="T-D", payload=None):
    return {"seq": seq, "kind": kind, "task": tid, "actor": actor,
            "payload": payload or {}}


def clean_rows(tid="T-D"):
    """MERGED 的完整形状，且 E32 由 human:yg 写（无历史偏离）。"""
    return [
        line(1, "state", payload={"task": tid, "from": None, "to": "draft"}),
        line(2, "acceptance_approved", actor="human:yg",
             payload={"task": tid, "acceptance_digest": "sha256:" + "e" * 64}),
        line(3, "state", payload={"task": tid, "from": "draft", "to": "pending"}),
        line(4, "human_approve", actor="human:yg", payload={"task": tid}),
        line(5, "state", payload={"task": tid, "from": "pending", "to": "claimed"}),
        line(6, "state", payload={"task": tid, "from": "claimed", "to": "in_progress"}),
        line(7, "state", payload={"task": tid, "from": "in_progress", "to": "submitted"}),
        line(8, "verify", actor="system:verifier",
             payload={"task": tid, "result": "pass", "exit_code": 0,
                      "duration_ms": 12, "evidence_path": "verify.log"}),
        line(9, "state", actor="system:verifier",
             payload={"task": tid, "from": "submitted", "to": "done"}),
        line(10, "merge", actor="system:merge_queue",
             payload={"task": tid, "merge_commit": "a" * 40, "head": "b" * 40,
                      "into": "master", "tree": "c" * 40}),
    ]


def deviation_rows(seq=2, actor="lead", tid="T-D"):
    """在 draft 上由 lead 写一条 E32（R-26 形状），seq 落在表边界内。"""
    rows = [line(1, "state", payload={"task": tid, "from": None, "to": "draft"})]
    rows.append(line(seq, "acceptance_approved", actor=actor, tid=tid,
                     payload={"task": tid,
                              "acceptance_digest": "sha256:" + "d" * 64}))
    return rows


def risk_text(rep):
    return " | ".join(rep.get("risks") or [])


def dev_doc(wb):
    return io.open(os.path.join(wb, "spec", "deviations.json"),
                   "r", encoding="utf-8", newline="").read()


def set_dev_doc(wb, doc):
    io.open(os.path.join(wb, "spec", "deviations.json"),
            "w", encoding="utf-8", newline="").write(
                json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def t_source_truth():
    s = io.open(os.path.join(WB, "scripts", "boss_report.py"),
                encoding="utf-8").read()
    ok("boss_report 真读 deviations.json", "spec/deviations.json" in s)
    ok("boss_report 有 _load_deviations", "def _load_deviations(" in s)
    ok("boss_report 有 _deviation_lines", "def _deviation_lines(" in s)
    ok("boss_report 有 event->kind 映射（读 spec 不硬编码）",
       "def _event_kind_map(" in s and "events.schema.json" in s)
    ok("boss_report 风险段真消费 dev_lines",
       "if dev_lines:" in s and "if dev_problem:" in s)
    ok("boss_report 不再硬编码条数/引用常数",
       "6 条历史行" not in s and "R-26" not in s)
    ok("表读不到是 fail-closed 风险", "暂不可确认" in s)
    ok("命中条件含 seq<=max_seq", 'int(r.get("seq") or 0) <= one["max_seq"]' in s)


def t_clean_has_no_deviation_risk():
    tmp, wb = fresh()
    try:
        rep = boss_report.render(FakeStore(wb, clean_rows()), "T-D")
        ok("干净任务判 MERGED", rep["verdict"] == "MERGED", rep["verdict"])
        ok("干净任务风险段不提历史偏离",
           "有界放行" not in risk_text(rep), risk_text(rep))
        ok("干净任务风险段不提边界不可读",
           "不可读" not in risk_text(rep), risk_text(rep))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_deviation_comes_from_table():
    tmp, wb = fresh()
    try:
        rep = boss_report.render(FakeStore(wb, deviation_rows()), "T-D")
        ok("历史偏离进风险段", "有界放行" in risk_text(rep), risk_text(rep))
        ok("风险段带引用（来自表）", "R-26" in risk_text(rep), risk_text(rep))
        ok("风险段带截止 seq（来自表）", "159" in risk_text(rep), risk_text(rep))
        ok("风险段写明条数", "1 条" in risk_text(rep), risk_text(rep))
        ok("风险段写明不判红也不改写", "不判红" in risk_text(rep)
           and "改写历史" in risk_text(rep), risk_text(rep))
        ok("偏离不改变 verdict（它不阻塞）", rep["verdict"] != "REVERTED",
           rep["verdict"])
        txt = boss_report.text(rep)
        ok("人读形态包含风险段", "§ 风险" in txt and "R-26" in txt)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_table_change_changes_report():
    tmp, wb = fresh()
    try:
        rows = deviation_rows()
        st = FakeStore(wb, rows)
        doc = json.loads(dev_doc(wb))
        doc["deviations"][0]["max_seq"] = 0          # 截止收窄 → 不再命中
        set_dev_doc(wb, doc)
        rep = boss_report.render(st, "T-D")
        ok("收窄表边界后不再报该偏离",
           "有界放行" not in risk_text(rep), risk_text(rep))

        doc["deviations"][0]["max_seq"] = 159
        doc["deviations"][0]["ref"] = "R-X9"        # 引用改了，汇报跟着改
        set_dev_doc(wb, doc)
        rep2 = boss_report.render(st, "T-D")
        ok("改 ref 汇报跟着变", "R-X9" in risk_text(rep2)
           and "R-26" not in risk_text(rep2), risk_text(rep2))

        doc["deviations"][0]["actor"] = "human:yg"  # actor 不匹配
        set_dev_doc(wb, doc)
        rep3 = boss_report.render(st, "T-D")
        ok("改 actor 后不命中", "有界放行" not in risk_text(rep3),
           risk_text(rep3))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_bad_table_is_risk_not_absence():
    tmp, wb = fresh()
    try:
        st = FakeStore(wb, clean_rows())
        p = os.path.join(wb, "spec", "deviations.json")
        os.remove(p)
        rep = boss_report.render(st, "T-D")
        ok("表缺失进风险段（不是无风险）",
           "不可读" in risk_text(rep) and "暂不可确认" in risk_text(rep),
           risk_text(rep))
        ok("表缺失不冒充「无已知风险」",
           "无已知风险" not in risk_text(rep), risk_text(rep))

        set_dev_doc(wb, {"criterion": "wrong", "deviations": [],
                         "counts": {"deviations": 0}})
        rep2 = boss_report.render(st, "T-D")
        ok("criterion 错进风险段", "criterion" in risk_text(rep2),
           risk_text(rep2))

        set_dev_doc(wb, {"criterion": "known_deviations_v1",
                         "deviations": [{"event": "E32", "actor": "lead",
                                         "ref": "R-26"}],
                         "counts": {"deviations": 1}})
        rep3 = boss_report.render(st, "T-D")
        ok("字段不全进风险段", "字段不全" in risk_text(rep3), risk_text(rep3))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_out_of_bound_not_counted():
    """seq > max_seq 的同类行不能混进「有界放行」。"""
    tmp, wb = fresh()
    try:
        rows = deviation_rows(seq=200, actor="lead")
        rep = boss_report.render(FakeStore(wb, rows), "T-D")
        ok("探针 seq 确实超过截止", rows[-1]["seq"] > 159, rows[-1]["seq"])
        ok("超截止不进有界放行", "有界放行" not in risk_text(rep),
           risk_text(rep))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_production_chain_note():
    """生产链上 lead 写的 E32 命中表，条数来自表不来自常数。"""
    st = wb_core.Store(WB, UUID)
    rows = [r for r in st._rows()
            if r.get("kind") == "acceptance_approved"
            and str(r.get("actor") or "") == "lead"]
    tasks = sorted(set(str(r.get("task") or "") for r in rows))
    hits, problem = [], None
    for tid in tasks:
        h, pr = boss_report._deviation_lines(st, tid, boss_report._rows_of(st, tid))
        hits.extend(h)
        if pr:
            problem = pr
    ok("生产链可复核且表可读", problem is None, problem)
    ok("生产链命中的条数 = 表里边界内的真实行数",
       len(hits) == len([r for r in rows if int(r.get("seq") or 0) <= 159]),
       (len(hits), len(rows)))
    ok("命中条数不是凭空数", len(hits) >= 1, len(hits))


def main():
    t_source_truth()
    t_clean_has_no_deviation_risk()
    t_deviation_comes_from_table()
    t_table_change_changes_report()
    t_bad_table_is_risk_not_absence()
    t_out_of_bound_not_counted()
    t_production_chain_note()
    if FAIL:
        print("test_boss_report_deviations: FAIL (%d failed)" % len(FAIL))
        return 1
    print("test_boss_report_deviations: PASS (%d ok, 0 failed)" % OK[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
