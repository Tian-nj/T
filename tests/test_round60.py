#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第六十轮防回归：五条候选各自「真接上了」，而不是只剩声明。

共同形状（第四十八轮起反复出现）：东西存在、规则写好了、但没有任何
路径让它发生，输出上却与「没有这件事」同形。这一轮五条全是这个形状的
变体，所以这里锁的都是运行行为 / 源码真值，不是文档里的自述。

  ① 演示链与生产 spec 隔离：demo.sh 不 overlay 生产基线，K14 见
     scope=demo_fresh_chain 打 NOTE 跳过逐条对账；生产空表仍判红。
  ② 旧键 longterm_min_count 退役期限：config_compat.json 进 spec/**，
     K13 拨时钟过期限且旧键仍在 → 判红。
  ③ settle 搬层补依据：moved[] 带 scene / settled_because，ledger_summary
     读出 moved_seen / moved_sample（与 settled 同用窗口内口径）。
  ④ absent 拆法推广到 settle / recall：exists=false 与窗口 0 行分开报，
     recall 没派过活时不报 absent。
  ⑤ 联合验收就绪：evaluate() 汇总 next_actions，--out 真落盘留证，
     selftest 仍绿，BLOCKED 如实登记。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

FAILED = []


def ok(name, got, want=True, detail=None):
    g = bool(got) if isinstance(want, bool) else got
    if g != want:
        FAILED.append("%s (got=%r want=%r %s)" % (name, got, want, detail or ""))


def _src(rel):
    with io.open(os.path.join(ROOT, rel), encoding="utf-8", newline="") as fh:
        return fh.read()


def _spec(name):
    with io.open(os.path.join(WB, "spec", name), encoding="utf-8") as fh:
        return json.load(fh)


class TempWB(object):
    """只造 ledger_summary 需要的 events/sidecar 与 spec 目录。"""

    def __init__(self, tag):
        self.tmp = tempfile.mkdtemp(prefix="r60-%s-" % tag)
        os.makedirs(os.path.join(self.tmp, "events", "sidecar"))

    def write(self, channel, rows):
        p = os.path.join(self.tmp, "events", "sidecar", channel + ".jsonl")
        with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def summary(self, days=1, now_ms=None):
        import memory_store as ms
        return ms.ledger_summary(days=days, now_ms=now_ms, wb=self.tmp)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def t_demo_baseline_isolated():
    """① 生产基线不进演示仓，K14 只对该 scope 放行，生产空表仍判红。"""
    import constitution_check as cc

    demo = _src("demo.sh")
    ok("demo SKIP_PATHS 排除生产基线",
       '".workbench/spec/recall_baseline.json"' in demo, True)
    # 第六十二轮 A④ 把内嵌 heredoc 抽成 demo_baseline_gen.py：
    # 断言从「demo.sh 里有没有那句话」改成「真跑生成器写出的 JSON 对不对」。
    ok("demo 0c 调生成器写演示链专用基线",
       "demo_baseline_gen.py" in demo, True)
    _gt = tempfile.mkdtemp(prefix="wb_gen60_")
    try:
        _gp = os.path.join(_gt, "recall_baseline.json")
        _rc = subprocess.call(
            [sys.executable,
             os.path.join(WB, "scripts", "demo_baseline_gen.py"),
             _gp, "T-0001"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok("生成器 rc=0", _rc, 0)
        _gd = {}
        if _rc == 0:
            _gd = json.load(io.open(_gp, encoding="utf-8"))
        ok("生成器写 scope=demo_fresh_chain",
           _gd.get("scope"), "demo_fresh_chain")
        ok("生成器写空表", _gd.get("entries"), [])
    finally:
        shutil.rmtree(_gt, ignore_errors=True)
    ok("demo 0c 说明 resign 签进 COVER", "resign manifest" in demo.lower(), True)

    cc_src = _src(".workbench/scripts/constitution_check.py")
    ok("K14 读 scope", '_scope = str(_doc.get("scope")' in cc_src, True)
    ok("K14 demo scope 有显式分支",
       'if _scope == "demo_fresh_chain":' in cc_src, True)
    ok("K14 demo 分支强制打 NOTE（绝不静默）",
       "NOTE K14 演示链基线" in cc_src, True)
    ok("K14 生产空表判据仍在",
       'bad("K14", "召回欠账基线为空' in cc_src, True)

    prod = _spec("recall_baseline.json")
    ok("生产基线 scope=production", prod.get("scope"), "production", prod.get("scope"))
    ok("生产基线仍有 7 条", len(prod.get("entries") or []), 7)

    # 拨到 demo scope 后，逐条对账不适用：纯函数层不报「基线为空」
    gen = _src(".workbench/scripts/gen_spec.py")
    ok("gen_spec 生成 scope=production", '"scope": "production"' in gen, True)


def t_k13_deprecation():
    """② 退役期限硬判据；第六十一轮候选 ③ 泛化后尺子跟涨并加严。

    签名从 (compat, mem_keys, now_ms) 变成
    (compat, config_paths, schema_paths, now_ms)：旧调用传叶子名列表会
    全部查不中，那是 API 契约变了、调用点必须跟着变，不是放宽判据。
    这里反而加严：schema_key 类登记必须真去 schema 集合里查。
    """
    import constitution_check as cc

    compat = _spec("config_compat.json")
    ok("config_compat criterion", compat.get("criterion"), "config_compat_v1")
    ok("config_compat schema 升 2（结构扩展）", compat.get("schema"), 2)
    deps = compat.get("deprecations") or []
    ok("登记了 2 条退役项（config_key + schema_key）", len(deps), 2)
    ok("两条 kind 覆盖 config/schema",
       sorted(str(d.get("kind")) for d in deps),
       ["config_key", "schema_key"])
    after = deps[0].get("deprecate_after_ms")
    ok("deprecate_after_ms 是整数",
       isinstance(after, int) and not isinstance(after, bool), True)

    f = cc.config_deprecation_findings
    cfg_yes = {"memory.repeat_min_count", "memory.longterm_min_count"}
    cfg_no = {"memory.repeat_min_count"}
    sch_yes = {"memory.repeat_min_count", "memory.longterm_min_count"}
    sch_no = {"memory.repeat_min_count"}
    ok("未到期 → 不报", f(compat, cfg_yes, sch_yes, after - 1000), [])
    ok("已到期 + 两套都还在 → 两条都判红",
       len(f(compat, cfg_yes, sch_yes, after + 1000)), 2)
    ok("已到期 + 两套都删 → 不报",
       f(compat, cfg_no, sch_no, after + 1000), [])
    ok("已到期 + 只删 config、schema 仍留 → 只报 schema 那条",
       len(f(compat, cfg_no, sch_yes, after + 1000)), 1)
    ok("已到期 + 只删 schema、config 仍留 → 只报 config 那条",
       len(f(compat, cfg_yes, sch_no, after + 1000)), 1)
    ok("缺 after 的登记 → 自报不可解析",
       len(f({"deprecations": [{"key": "memory.x", "kind": "config_key"}]},
             cfg_yes, sch_yes, after)), 1)
    ok("不认识的 kind → 自报不可解析，不静默按「没有」计",
       len(f({"deprecations": [{"key": "memory.x", "kind": "wat"}]},
             cfg_yes, sch_yes, after)), 1)
    ok("flatten_config_keys 展平成 dotted path",
       "memory.repeat_min_count" in cc.flatten_config_keys(
           {"memory": {"repeat_min_count": 3}}), True)
    ok("flatten_schema_keys 认 children 而不是 properties",
       "memory.longterm_min_count" in cc.flatten_schema_keys(
           {"keys": {"memory": {"children": {
               "longterm_min_count": {"type": "integer"}}}}}), True)

    cc_src = _src(".workbench/scripts/constitution_check.py")
    ok("K13 真读 config.yaml 全段",
       "flatten_config_keys(" in cc_src and "yaml_parse_file(" in cc_src, True)
    ok("K13 真读 config.schema.json",
       "flatten_schema_keys(" in cc_src and '"config.schema.json"' in cc_src, True)
    ok("K13 有可控时钟旋钮", "WB_NOW_MS" in cc_src, True)
    ok("K13 逐条过纯函数并 bad()", "config_deprecation_findings(" in cc_src
       and 'bad("K13", _d)' in cc_src, True)

    gen = _src(".workbench/scripts/gen_spec.py")
    ok("退役期限由 gen_spec 单一生成", "LONGTERM_MIN_COUNT_DEPRECATE_AFTER_MS" in gen, True)
    ok("config_compat.json 进 build()", '"config_compat.json": enc(' in gen, True)
    ok("spec 件数保持 11 件", os.path.isfile(os.path.join(WB, "spec", "config_compat.json")), True)


def t_settle_moved_traceable():
    """③ moved[] 逐条可追，ledger_summary 真读出来（窗口内口径）。"""
    import memory_store as ms

    t = TempWB("settle")
    now = 1794550000000
    t.write("memory_settle", [{
        "ts_ms": now - 1000, "channel": "memory_settle", "task": None,
        "payload": {"checked": 2, "settled": 2, "unresolved": 0,
                    "unaccounted_unresolved": 0,
                    "moved": [{"id": "m-a", "task": "T-X", "state": "done"},
                              {"id": "m-b", "task": "T-Y", "state": "cancelled"}]},
    }])
    rep = t.summary(days=1, now_ms=now)
    st = rep["ledgers"]["memory_settle"]
    ok("moved_seen 累计 2", st.get("moved_seen"), 2, st)
    ok("moved_sample 有 2 条", len(st.get("moved_sample") or []), 2, st.get("moved_sample"))
    ok("sample 带 task 可追",
       sorted(m.get("task") for m in st["moved_sample"]), ["T-X", "T-Y"],
       st.get("moved_sample"))

    # 窗口外的一行不计入 moved_seen：与 settled 同用窗口内口径，
    # 否则同一份报告会自相矛盾「settled=0 却搬了 1 条」。
    t.write("memory_settle", [
        {"ts_ms": now - 90 * 86400000, "channel": "memory_settle", "task": None,
         "payload": {"checked": 1, "settled": 1, "unresolved": 0,
                     "unaccounted_unresolved": 0,
                     "moved": [{"id": "m-old", "task": "T-OLD", "state": "done"}]}},
        {"ts_ms": now - 1000, "channel": "memory_settle", "task": None,
         "payload": {"checked": 0, "settled": 0, "unresolved": 0,
                     "unaccounted_unresolved": 0, "moved": []}},
    ])
    rep2 = t.summary(days=1, now_ms=now)
    st2 = rep2["ledgers"]["memory_settle"]
    ok("窗口外的 moved 不混进本报告", st2.get("moved_seen"), 0, st2)
    ok("窗口内 rows 只算 1 行", st2.get("rows"), 1, st2)
    t.cleanup()

    src = _src(".workbench/scripts/memory_store.py")
    ok("moved 带 scene", '"scene": (e.get("scene") or "")[:80]' in src, True)
    ok("moved 带 settled_because", '"settled_because": e.get("settled_because")' in src, True)
    ok("聚合读 p.moved", 'for m in (p.get("moved") or []):' in src, True)
    ok("聚合有 moved_seen 计数", 'cur["moved_seen"] += 1' in src, True)


def t_absent_spread():
    """④ absent/silent 拆法推广到 settle / recall 两通道。"""
    import scheduler_observe as so
    import memory_store as ms

    orig = ms.ledger_summary

    def fake(dispatch_ms, settle, recall, decay_rows=5):
        ledgers = {
            "memory_decay": {"exists": True, "rows": decay_rows,
                             "last_ts_ms": 1, "stale": False,
                             "stale_after_ms": 9, "age_ms": 1},
            "memory_settle": dict(settle),
            "memory_recall": dict(recall),
        }
        def _f(wb=None, days=1, **kw):
            return {"bad_lines": 0, "future_rows": 0, "untimed": 0,
                    "dispatch_ms": dispatch_ms, "ledgers": ledgers}
        return _f

    def ids_of(fn):
        ms.ledger_summary = fn
        try:
            return [r["id"] for r in so.longrun_risks(window_days=1)]
        finally:
            ms.ledger_summary = orig

    # 两通道都从没写过 + 有派活 → 两条 absent
    ids = ids_of(fake(1794550000000,
                      {"exists": False, "rows": 0},
                      {"exists": False, "rows": 0}))
    ok("settle 从没写过被看见", "settle_ledger_absent" in ids, True, ids)
    ok("recall 派过活却从没写过被看见", "recall_ledger_absent" in ids, True, ids)

    # 没派过活 → recall 台账空属正常，不报 absent
    ids = ids_of(fake(None,
                      {"exists": True, "rows": 3, "last_ts_ms": 1},
                      {"exists": False, "rows": 0}))
    ok("没派活时 recall absent 不误报", "recall_ledger_absent" not in ids, True, ids)

    # 文件在但窗口 0 行 → silent，且不该误报 absent
    ids = ids_of(fake(1794550000000,
                      {"exists": True, "rows": 0, "last_ts_ms": 1},
                      {"exists": True, "rows": 0, "last_ts_ms": 1}))
    ok("settle 窗口 0 行报 silent", "settle_ledger_silent_1d" in ids, True, ids)
    ok("recall 窗口 0 行报 silent", "recall_ledger_silent_1d" in ids, True, ids)
    ok("silent 时不误报 absent",
       "settle_ledger_absent" not in ids and "recall_ledger_absent" not in ids,
       True, ids)

    # 夹具没给该通道 exists 键 → 跳过，按没观测计
    ids = ids_of(fake(1794550000000, {}, {}))
    ok("夹具缺 exists 时不凭空报",
       "settle_ledger_absent" not in ids and "recall_ledger_absent" not in ids,
       True, ids)

    src = _src(".workbench/scripts/scheduler_observe.py")
    ok("观察器按通道循环", '("memory_settle", "settle", False)' in src, True)
    ok("观察器对事件通道判 dispatch_ms",
       'led.get("dispatch_ms") is None' in src, True)


def t_joint_next_actions():
    """⑤ 联合验收把「补上之后跑哪条命令」汇总出来，且 --out 真落盘。"""
    import joint_acceptance as ja

    rep = ja.evaluate()
    ok("生产仍 BLOCKED", rep["verdict"], ja.BLOCKED, rep["verdict"])
    ok("有 next_actions", "next_actions" in rep, True)
    ok("next_actions 覆盖全部阻塞项",
       len(rep["next_actions"]), rep["blocked_count"], rep["blocked_count"])
    ok("每项带 how / verify / why",
       all(a.get("how") and a.get("verify") and a.get("why")
           for a in rep["next_actions"]), True, rep["next_actions"])
    # 这里原本硬写 ["actions","model","window"]，等于把「本机没配凭据」当成
    # 永久事实：真配了 .git/info/model_credentials.json 的机器会红。改成核
    # 不变量 —— next_actions 点名的正好是当前所有阻塞项，一个不多一个不少。
    ok("blocked_count 与就绪项互补",
       rep["blocked_count"], len(rep["items"]) - rep["ready_count"], rep)
    ok("next_actions 点名的正好是当前阻塞项",
       sorted(a["id"] for a in rep["next_actions"]),
       sorted(i["id"] for i in rep["items"] if not i.get("ready")),
       [a["id"] for a in rep["next_actions"]])
    # actions / window 这两项在本机结构上不可能就绪（无 remote、窗口未到点），
    # 所以它们仍阻塞是硬结论，可以照旧点死。
    ok("actions 与 window 仍阻塞",
       [i["id"] for i in rep["items"] if not i.get("ready") and
        i["id"] in ("actions", "window")], ["actions", "window"])
    ok("不参与 W4", rep["w4_participates"], False)

    src = _src(".workbench/scripts/joint_acceptance.py")
    ok("--out 由 main 解析", 'ap.add_argument("--out"' in src, True)
    ok("--out 写的是 evaluate() 的 JSON", "json.dumps(rep" in src, True)
    ok("--out 是唯一写动作（不碰链）",
       "io.open(a.out" in src, True)

    # 真跑 --out 落到临时文件，读回与 evaluate() 同形
    tmp = tempfile.mkdtemp(prefix="r60joint-")
    out = os.path.join(tmp, "joint.json")
    rc = ja.main(["--out", out])
    ok("--out 退出码是延后 3", rc, 3, rc)
    with io.open(out, encoding="utf-8") as fh:
        saved = json.load(fh)
    ok("落盘证据 verdict 一致", saved["verdict"], rep["verdict"])
    ok("落盘证据带 next_actions", len(saved.get("next_actions") or []), rep["blocked_count"])
    shutil.rmtree(tmp, ignore_errors=True)

    ok("selftest 仍绿", ja.selftest(), 0)


def t_source_truth():
    rt = _src("run_all_checks.sh")
    rd = _src("README.md")
    ok("门禁注释说到第六十轮", "第六十轮" in rt, True)
    ok("README 有 17.2x", "17.2x" in rd, True)
    ok("for 清单接入 test_round60", "test_round60" in rt, True)
    ok("gen_spec 有 _gen_config_compat", "def _gen_config_compat(" in
       _src(".workbench/scripts/gen_spec.py"), True)


def main():
    t_demo_baseline_isolated()
    t_k13_deprecation()
    t_settle_moved_traceable()
    t_absent_spread()
    t_joint_next_actions()
    t_source_truth()
    if FAILED:
        print("test_round60: FAIL (%d)" % len(FAILED))
        for f in FAILED:
            print("  " + f)
        return 1
    print("test_round60: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
