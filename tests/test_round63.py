#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第六十三轮防回归：这一轮收的不是功能，是**尺子**。

七个探针，每个都对着一个「改了之后看起来更健康、其实只是没人量」的位置：

  [1] metrics 走 sidecar：自动快照不再往主链写 kind=snapshot，
      且 E41 metrics_snapshot 已退役（append_event 会当场拒收）。
  [2] read_sidecar 三态：坏行计数不为 0、channel 拒绝路径穿越、
      按 ts_ms/task 物理过滤。
  [3] audit_static 判红：新增死函数 → 退出码 1；基线为空 → PASS。
  [4] revert_to 边界：docstring 宣称的 `kept N unrelated path(s)` 真的存在
      （此前这个串只出现在注释里）。
  [5] doc_numbers 量到「校验」位：rail_items/rail_detail/rail_header 都在
      CLAIMS 或 RAIL_HEAD 里，且 --check 八处一致。
  [6] dream 四态可分：`stats()` 逐日给 done / recorded_empty / empty / pending，
      且 `run_backlog` 带 `window` —— 此前「全做完」「全零素材」「没跑」
      三种现实共用同一个 `{"dates": [], "ran": 0}`。
  [7] boss_report 系统段：三块各自独立取值，任一块读不到不影响其他两块。

反向验证过的三条（写在这里是为了让后来的人知道它们不是恒真）：
  · 往 gate_timer.py 塞空函数 → audit_static 退出码 1；
  · 把 text() 的 join 改回字面量 → test_round63 的 [7] 变红；
  · 把 rail_items 正则改回旧版 → doc_numbers --check 报 5 处不一致（原 8 处）。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
sys.path.insert(0, SCRIPTS)

FAILED = []


def ok(name, got, want=True, detail=None):
    g = bool(got) if isinstance(want, bool) else got
    if g != want:
        FAILED.append("FAIL %s (got=%r want=%r %s)"
                      % (name, got, want, detail if detail is not None else ""))


def _src(rel):
    with io.open(os.path.join(ROOT, rel), "r", encoding="utf-8",
                 newline="") as fh:
        return fh.read()


def _code(rel):
    """去掉注释与 docstring 的源码。

    为什么要这个：`metrics_collector.emit()` 的 docstring 里写着
    「历史：这里原本 `append_event("metrics_snapshot", ...)`」—— 直接 grep
    会命中这段历史说明，于是「不再写主链」这条断言永远为 False。
    断言必须量行为，不能量注释；反过来，注释里提到旧写法是好事，不该被罚。
    """
    out = []
    for ln in _src(rel).splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue
        out.append(ln)
    txt = "\n".join(out)
    # 剥三引号块（本项目所有 docstring 都是 """ 开头）
    parts = txt.split('"""')
    if len(parts) >= 3:
        kept = [parts[0]]
        for i in range(1, len(parts), 2):
            if i + 1 < len(parts):
                kept.append(parts[i + 1])
        txt = "\n".join(kept)
    return txt


# ------------------------------------------- [1] metrics 走 sidecar / E41 退役
def t_metrics_sidecar():
    mc = _src(".workbench/scripts/metrics_collector.py")
    code = _code(".workbench/scripts/metrics_collector.py")
    ok("emit 改写 sidecar", 'st.append_sidecar("metrics"' in mc, True)
    ok("emit 不再写 metrics_snapshot 主链事件",
       'append_event("metrics_snapshot"' not in code, True)
    ok("自动快照登记走 sidecar", 'st.append_sidecar("snapshot"' in mc, True)
    ok("自动快照不再写 snapshot 主链事件",
       'append_event("snapshot"' not in code, True)
    ok("幂等基准改读 sidecar", 'read_sidecar("snapshot")' in mc, True)
    # 注释里保留旧写法说明是好事（后来人要知道为什么改），别被误罚
    ok("docstring 仍记录旧写法（可追溯）",
       'append_event("metrics_snapshot"' in mc, True)

    gs = _src(".workbench/scripts/gen_spec.py")
    line = [ln for ln in gs.splitlines() if ln.startswith("RETIRED = ")]
    ok("RETIRED 行唯一", len(line), 1)
    ok("E41 进 RETIRED", '"E41"' in (line[0] if line else ""), True,
       line[0] if line else "")
    ok("E47 仍在 RETIRED（没被覆盖）", '"E47"' in (line[0] if line else ""), True)

    # 事件的权威表也必须认：退役后 append_event 会拒收，这是从自律变成硬约束
    ev = json.load(io.open(os.path.join(WB, "spec", "events.schema.json"),
                           encoding="utf-8"))
    retired = set(ev.get("retired_ids") or [])
    ok("schema retired_ids 含 E41", "E41" in retired, True,
       sorted(retired))
    ok("schema retired_ids 含 E47", "E47" in retired, True, sorted(retired))


# ------------------------------------------- [2] read_sidecar 三态
def t_read_sidecar():
    import wb_core
    import sign_manifest

    base = tempfile.mkdtemp(prefix="r63-sidecar-")
    wb = os.path.join(base, ".workbench")
    os.makedirs(os.path.join(wb, "events", "sidecar"), exist_ok=True)
    with io.open(os.path.join(wb, "config.yaml"), "w",
                 encoding="utf-8", newline="\n") as fh:
        fh.write("uuid: 00000000000000000000000000000000\n")
    p = os.path.join(wb, "events", "sidecar", "probe.jsonl")
    with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"ts_ms": 100, "channel": "probe",
                             "task": "T-A", "payload": {"n": 1}}) + "\n")
        fh.write("{this is not json\n")          # 坏行
        fh.write(json.dumps({"ts_ms": 300, "channel": "probe",
                             "task": "T-B", "payload": {"n": 2}}) + "\n")

    st = wb_core.Store(wb, "0" * 32)
    r = st.read_sidecar("probe")
    ok("读到 2 条好行", len(r["rows"]), 2)
    ok("坏行被计数而不是静默丢", r["corrupt"], 1)
    ok("坏行不阻断好行", [x["payload"]["n"] for x in r["rows"]], [1, 2])

    ok("按 task 过滤", len(st.read_sidecar("probe", task="T-A")["rows"]), 1)
    ok("按 ts_ms 下界过滤",
       [x["payload"]["n"] for x in st.read_sidecar("probe", since_ms=200)["rows"]],
       [2])

    # 路径穿越必须被拒（这不是「防攻击」，是防自己写错：channel 是代码里的
    # 字面量，拼错了应当当场炸，而不是在 events/ 外面建一个文件）
    for bad in ("../x", "a/b", "a\\b", ".."):
        try:
            st.read_sidecar(bad)
            ok("拒绝非法 channel %r" % bad, False, True, "没抛异常")
        except Exception:
            ok("拒绝非法 channel %r" % bad, True, True)

    # 通道不存在 → 空，不是异常
    r2 = st.read_sidecar("nope")
    ok("不存在的通道返回空", (r2["rows"], r2["corrupt"]), ([], 0))


# ------------------------------------------- [3] audit_static 判红
def t_audit_static_red():
    py = sys.executable
    # 干净仓：基线为空（当前真值），PASS
    p = subprocess.run([py, os.path.join(ROOT, "audit_static.py")],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    ok("当前仓 audit_static 退出码 0", p.returncode, 0,
       (p.stdout or "")[-300:])
    ok("输出含 RESULT: PASS", "RESULT: PASS" in (p.stdout or ""), True)

    src = _src("audit_static.py")
    ok("不再裸 return 0", "    return 0\n\n\nif __name__" not in src, True)
    ok("有 judge()", "def judge(" in src, True)
    ok("有基线文件机制", "audit_static_baseline.json" in src, True)
    ok("协议方法不误报（do_GET 等进 PROTOCOL_ATTRS）",
       '"do_GET"' in src, True)
    bl = os.path.join(ROOT, "audit_static_baseline.json")
    ok("基线文件在盘", os.path.isfile(bl), True)
    if os.path.isfile(bl):
        doc = json.load(io.open(bl, encoding="utf-8"))
        ok("基线是 dict", isinstance(doc.get("dead"), dict), True)
        ok("基线当前为空（= 真的一个死函数都没有）", doc.get("dead"), {})


# ------------------------------------------- [4] revert_to 边界真的存在
def t_revert_boundary():
    mq = _src(".workbench/scripts/merge_queue.py")
    ok("revert_to 真的调 dirty_paths", "dirty_paths()" in mq, True)
    ok("dirty_paths 有定义", "def dirty_paths(" in mq, True)
    ok("outside 有定义（不是只有使用）",
       "outside = sorted(set(dirty_paths()) - set(touched))" in mq, True)
    ok("outside 在回滚前取快照（顺序可证）",
       mq.index("outside = sorted(set(dirty_paths())")
       < mq.index('p = git("update-ref", "refs/heads/%s" % branch, base, cur)'),
       True)
    ok("outside_dirty 进了 tried", '"outside_dirty=%d"' in mq, True)
    # 关键：这句话此前只出现在 docstring 里
    code = [ln for ln in mq.splitlines()
            if "kept %d unrelated path(s)" in ln
            and not ln.lstrip().startswith("#")]
    ok("kept N unrelated path(s) 是代码不是注释", len(code) >= 1, True, code)
    ok("越界抹掉时判失败", "REVERT_FAILED: 回滚越过边界" in mq, True)

    # 真跑：造一个「边界内有 a.txt、边界外有 b.txt 未提交」的假仓，
    # 回滚只能动 a.txt。**必须把 git 换成假的**：merge_queue.git() 的默认
    # cwd 是真实仓根（ROOT），真跑会在本仓上动 update-ref —— 那是拿生产
    # 仓库当试验台，比不测更坏。
    import merge_queue as mq_mod

    base_dir = tempfile.mkdtemp(prefix="r63-revert-")
    io.open(os.path.join(base_dir, "a.txt"), "w", encoding="utf-8").write("A0\n")
    io.open(os.path.join(base_dir, "b.txt"), "w", encoding="utf-8").write("B-EDIT\n")

    BASE = "b" * 40
    CUR = "c" * 40
    state = {"head": CUR}

    class _P(object):
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_git(*args, **kw):
        a = list(args)
        if a[:2] == ["rev-parse", "--verify"]:
            return _P(0, state["head"] + "\n")
        if a[:1] == ["symbolic-ref"]:
            return _P(0, "master\n")
        if a[:1] == ["status"]:
            # --untracked-files=no：全工作区；带路径：只报该路径
            if "--untracked-files=no" in a:
                return _P(0, " M b.txt\n")
            # 逐路径查询：b.txt 仍在（没被抹）-> 有痕迹
            if any(x.endswith("b.txt") for x in a):
                return _P(0, " M b.txt\n")
            return _P(0, "")          # a.txt 残留应为 0
        if a[:1] == ["update-ref"]:
            state["head"] = a[2]
            return _P(0, "")
        if a[:1] == ["cat-file"]:
            return _P(0 if a[-1].endswith(":a.txt") else 1, "")
        return _P(0, "")

    saved = (mq_mod.git, mq_mod.rev, mq_mod.def_branch, mq_mod.ROOT)
    mq_mod.git = fake_git
    mq_mod.rev = lambda branch="main", cwd=None: state["head"]
    mq_mod.def_branch = lambda: "master"
    mq_mod.ROOT = base_dir
    try:
        okk, detail = mq_mod.revert_to(object(), "T-1", BASE,
                                       {"branch": "master"}, touched=["a.txt"],
                                       merge_commit="")
    finally:
        mq_mod.git, mq_mod.rev, mq_mod.def_branch, mq_mod.ROOT = saved

    ok("revert_to 成功", okk, True, detail)
    ok("边界外的 b.txt 仍在（没被抹）",
       io.open(os.path.join(base_dir, "b.txt"), encoding="utf-8").read().strip(),
       "B-EDIT")
    ok("detail 里报 kept 1", "kept 1 unrelated path(s)" in detail, True, detail)
    ok("tried 里报了 outside_dirty=1", "outside_dirty=1" in detail, True, detail)
    ok("HEAD 真回到 base", state["head"], BASE)


def t_revert_boundary_crossed():
    """越界时**必须判失败**，不能照旧报成功。

    这是「声明悬空」的反面验证：`kept N` 那句话如果只是打印，越界就永远
    看不出来。这里让 b.txt 在回滚后消失（模拟被别人抹掉），要求 revert_to
    返回 False 且 reason 里点名 b.txt。
    """
    import merge_queue as mq_mod

    base_dir = tempfile.mkdtemp(prefix="r63-revert-x-")
    io.open(os.path.join(base_dir, "a.txt"), "w", encoding="utf-8").write("A0\n")
    io.open(os.path.join(base_dir, "b.txt"), "w", encoding="utf-8").write("B-EDIT\n")
    BASE, CUR = "b" * 40, "c" * 40
    state = {"head": CUR}

    class _P(object):
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_git(*args, **kw):
        a = list(args)
        if a[:2] == ["rev-parse", "--verify"]:
            return _P(0, state["head"] + "\n")
        if a[:1] == ["symbolic-ref"]:
            return _P(0, "master\n")
        if a[:1] == ["status"]:
            if "--untracked-files=no" in a:
                return _P(0, " M b.txt\n")
            # 回滚后 b.txt 的改动不在了 → 越界
            return _P(0, "")
        if a[:1] == ["update-ref"]:
            state["head"] = a[2]
            return _P(0, "")
        if a[:1] == ["cat-file"]:
            return _P(0 if a[-1].endswith(":a.txt") else 1, "")
        return _P(0, "")

    saved = (mq_mod.git, mq_mod.rev, mq_mod.def_branch, mq_mod.ROOT)
    mq_mod.git = fake_git
    mq_mod.rev = lambda branch="main", cwd=None: state["head"]
    mq_mod.def_branch = lambda: "master"
    mq_mod.ROOT = base_dir
    # 关键：模拟「回滚把 b.txt 抹掉了」—— 让 outside 里的 b.txt 查不到痕迹
    real_join = os.path.join
    try:
        okk, detail = mq_mod.revert_to(object(), "T-1", BASE,
                                       {"branch": "master"}, touched=["a.txt"],
                                       merge_commit="")
    finally:
        mq_mod.git, mq_mod.rev, mq_mod.def_branch, mq_mod.ROOT = saved
        os.path.join = real_join

    ok("越界时判失败", okk, False, detail)
    ok("失败原因点名 REVERT_FAILED", "REVERT_FAILED" in detail, True, detail)


# ------------------------------------------- [5] doc_numbers 量到校验位
def t_doc_numbers_verify():
    dn = _src(".workbench/scripts/doc_numbers.py")
    ok("CLAIMS 有 rail_items 含校验位",
       '"# {v} 项：{tests} 测试 + {audit} 审计 + {verify} 校验"' in dn, True)
    ok("有 rail_detail", '"rail_detail"' in dn, True)
    ok("有 RAIL_HEAD", "RAIL_HEAD = [" in dn, True)
    ok("RAIL 指向仓根（两级 dirname）",
       'os.path.dirname(os.path.dirname(HERE))' in dn, True)
    ok("check 接头注释核对", "out.extend(check_rail_header(truth))" in dn, True)
    ok("fix 接头注释修正", "fix_rail_header(truth)" in dn, True)

    sys.path.insert(0, SCRIPTS)
    import doc_numbers as dn_mod
    t = dn_mod.compute()
    ok("verify_count = check + contract + sign",
       t["verify_count"],
       t["check_only"] + t["contract_only"] + t["sign_only"])
    ok("checks_total = test + audit + verify + e2e",
       t["checks_total"],
       t["test_count"] + t["audit_count"] + t["verify_count"] + t["e2e_count"],
       {k: t[k] for k in ("checks_total", "test_count", "audit_count",
                          "verify_count", "e2e_count")})
    findings = dn_mod.check(_src("README.md"), t)
    findings += dn_mod.check_rail_header(t)
    ok("README + 头注释八处声明全一致", findings, [], findings)


# ------------------------------------------- [6] dream 三态不同形
def t_dream_tristate():
    dc = _src(".workbench/scripts/dream_consolidation.py")
    ok("gather 存在", "def gather(" in dc, True)
    ok("pending_dates 会排掉零素材日",
       "if rows is not None and not gather(rows, int(now_ms), d):" in dc, True)
    ok("_really_done 不把 skipped 当完成",
       'summary.skipped' in dc or "skipped" in dc, True)

    import dream_consolidation as dc_mod
    # run_backlog 的四键必须区分「没跑」与「没素材」：
    #   dates 空 + ran=0 + skipped 空 = 没有待办日（都已完成）
    #   dates 空 + skipped 有值       = 有日但素材不足
    # 生产当前应为前者（11-10..11-12 done，11-13 skipped 已记账）。
    import time
    now = int(time.time() * 1000)
    rep = dc_mod.pending_dates(now)
    ok("pending_dates 返回 list", isinstance(rep, list), True, type(rep).__name__)
    # 已完成的日子不能重新出现在待办里
    state_dir = os.path.join(WB, "state", "dream")
    done_days = []
    if os.path.isdir(state_dir):
        for fn in sorted(os.listdir(state_dir)):
            if not fn.endswith(".json"):
                continue
            d = json.load(io.open(os.path.join(state_dir, fn), encoding="utf-8"))
            if d.get("status") == "done" and not (d.get("summary") or {}).get("skipped"):
                done_days.append(fn[:-5])
    ok("已完成日不再进待办", [d for d in rep if d in done_days], [], done_days)

    # ---- stats：四态必须可分（G4 的正解）----
    ok("有 stats()", "def stats(" in dc, True)
    ok("stats 有 CLI 子命令", 'sub.add_parser("stats",' in dc, True)
    st = dc_mod._open_store()
    import time as _t
    srep = dc_mod.stats(int(_t.time() * 1000), 3, st)
    ok("stats 返回 days 明细", len(srep["days"]) == 3, True, len(srep["days"]))
    ok("stats 计数之和 = 窗口天数",
       sum(srep["counts"].values()), 3, srep["counts"])
    # 四态的名字必须都是登记过的，不许出现临时新词
    allowed = {"done", "recorded_empty", "empty", "pending", "unknown"}
    bad = [x["state"] for x in srep["days"] if x["state"] not in allowed]
    ok("四态取值封闭", bad, [], bad)
    # 关键：recorded_empty 与 done 不能混为一谈（11-13 是前者，11-12 是后者）
    by = {x["date"]: x["state"] for x in srep["days"]}
    ok("11-13 记为 recorded_empty（跑过但零素材）",
       by.get("2026-11-13"), "recorded_empty", by)
    ok("11-12 记为 done（真整理过）", by.get("2026-11-12"), "done", by)
    ok("零素材日 usable=0 而不是 None",
       [x["usable"] for x in srep["days"] if x["date"] == "2026-11-13"], [0],
       [x for x in srep["days"] if x["date"] == "2026-11-13"])
    # run_backlog 现在带 window，让「全做完」与「没跑」不再同形
    r = dc_mod.run_backlog(st, int(_t.time() * 1000), 3, True)
    ok("run_backlog 带 window", "window" in r, True, sorted(r))
    ok("window 里有 counts", "counts" in (r.get("window") or {}), True,
       r.get("window"))
    # 一把尺子：gather 与 stats 必须共用 _window，不许各抄一份
    ok("有 _window 单一窗口函数", "def _window(" in dc, True)
    ok("gather 走 _window", "lo, hi = _window(date, now_ms)" in dc, True)
    ok("_day_state 走 _window", "lo, hi = _window(date, now_ms)" in dc, True)
    # selftest 里有一处 `now - window_hours()*3600000` 是构造旧口径的测试数据，
    # 不是窗口实现；真窗口只允许 `_window` 里那一句 keep = window_hours()。
    ok("窗口口径只剩 _window 一处实现",
       dc.count("keep = window_hours()"), 1, dc.count("keep = window_hours()"))


# ------------------------------------------- [7] boss_report 系统段
def t_boss_system():
    src = _src(".workbench/scripts/boss_report.py")
    ok("有 _system_evidence", "def _system_evidence(" in src, True)
    ok("render 返回 system 键", '"system": _system_evidence(st),' in src, True)
    ok("系统状态不进 evidence",
       src.index('"system": _system_evidence(st),')
       > src.index("evidence.append(_provenance_evidence(st))"), True)
    ok("text() 有 § 系统 段", 'L.append("§ 系统")' in src, True)
    ok("缺段时报 bug 而不是省略", "渲染层没生成系统段" in src, True)
    ok("噪声口径自报家门", "NOISE_SCOPE" in src, True)
    ok("显式声明两把尺子不同", "dream_consolidation.IGNORE_KINDS" in src, True)
    # 生成器必须先落 list，否则 len() TypeError / 二次遍历得 0
    ok("_rows() 先落 list", "rows = list(st._rows())" in src, True)

    import boss_report
    import wb_core
    import sign_manifest
    os.environ.setdefault("WORKBENCH_ROOT_KEY", "dev-root-key-2026-please-replace")
    wb_core.load_frozen(WB)
    st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
    rep = boss_report.render(st, "T-0001")
    sysrec = rep.get("system") or {}
    ok("system 是 dict 且有 value", isinstance(sysrec, dict)
       and bool(sysrec.get("value")), True, sysrec)
    val = sysrec.get("value") or ""
    ok("系统段含调度器", "调度器：" in val, True, val[:120])
    ok("系统段含链", "链：" in val, True, val[:120])
    ok("系统段含识海", "识海：" in val, True, val[:120])
    ok("三段都没有未取值（本机三块都读得到）", "未取值" in val, False, val)

    txt = boss_report.text(rep)
    ok("text 用真实换行（不是字面量 \\n）", "\\n" in txt, False,
       repr(txt[-80:]))
    ok("text 至少 8 行", len(txt.splitlines()) >= 8, True, len(txt.splitlines()))
    ok("text 含 § 系统 段落", "§ 系统" in txt, True)


# ------------------------------------------- [8] 噪声口径登记制
def t_noise_registry():
    """G3 的结论是「不许合并，要登记」。这组锁的是登记制本身。"""
    core = _code(".workbench/scripts/wb_core.py")
    ok("有 NOISE_REGISTRY", "NOISE_REGISTRY = {" in core, True)
    ok("有 NOISE_PURPOSES", "NOISE_PURPOSES = {" in core, True)
    ok("有 NOISE_EXPECTED_DIFF", "NOISE_EXPECTED_DIFF = {" in core, True)
    ok("有 noise_kinds()", "def noise_kinds(scope" in core, True)
    ok("有 noise_diff()", "def noise_diff()" in core, True)
    # 未登记的口径必须抛，绝不返回空元组 —— 返回空等于「没有噪声」
    ok("未登记口径抛 KeyError 而不是返回空",
       "raise KeyError(" in core and "NOISE_REGISTRY[scope]" in core, True)

    import wb_core as c
    m, d = c.noise_kinds("material"), c.noise_kinds("display")
    ok("material 11 种", len(m), 11, len(m))
    ok("display 8 种", len(d), 8, len(d))
    ok("两份口径不相等（不许合并）", m == d, False)
    # 差集本身是判据：谁顺手动了口径而没动这里，当场红
    got = c.noise_diff()
    want = {k: tuple(v) for k, v in c.NOISE_EXPECTED_DIFF.items()}
    ok("差集与登记常量一致", got, want, got)
    # 两个方向的关键词必须落在正确的一侧
    ok("verify 只在素材口径里被排除",
       ("verify" in got["only_material"], "verify" in got["only_display"]),
       (True, False), got)
    ok("plan 只在展示口径里被排除",
       ("plan" in got["only_display"], "plan" in got["only_material"]),
       (True, False), got)
    try:
        c.noise_kinds("nope")
        raised = False
    except KeyError:
        raised = True
    ok("未登记口径真的抛", raised, True)

    # 两个使用方都必须从登记表取，不许再写字面量
    dc = _code(".workbench/scripts/dream_consolidation.py")
    br = _code(".workbench/scripts/boss_report.py")
    ok("dream 从登记表取 material", 'IGNORE_KINDS = _noise("material")' in dc, True)
    ok("boss 从登记表取 display", '_NOISE_KINDS = _noise("display")' in br, True)
    ok("dream 不再写字面量元组", 'IGNORE_KINDS = ("' in dc, False)
    ok("boss 不再写字面量元组", '_NOISE_KINDS = ("' in br, False)
    import dream_consolidation as dm, boss_report as bm
    ok("运行时 dream 口径 == material", dm.IGNORE_KINDS, m, dm.IGNORE_KINDS)
    ok("运行时 boss 口径 == display", bm._NOISE_KINDS, d, bm._NOISE_KINDS)


# ------------------------------------------- [9] 调度器双档判据
def t_sched_split():
    """G8：执行点存在性判红、新鲜度只 warn。分档依据是误报来源。"""
    si = _code(".workbench/scripts/sched_install.py")
    ok("有 cmd_registered", "def cmd_registered(a)" in si, True)
    ok("registered 子命令已挂", 'sub.add_parser("registered"' in si, True)
    ok("registered 失败返回 1", "return 0 if ok else 1" in si, True)
    road = _src("run_all_checks.sh")
    ok("回归里 registered 用 run（判红）",
       'run "check scheduler registered"' in road, True)
    ok("回归里 status 仍用 run_warn（只提示）",
       'run_warn "check scheduler status"' in road, True)
    # 真跑：registered 现在必须通过（执行点确实在）
    import subprocess as sp
    r = sp.run([sys.executable, os.path.join(SCRIPTS, "sched_install.py"),
                "registered"], stdout=sp.PIPE, stderr=sp.PIPE)
    out = (r.stdout or b"").decode("utf-8", "replace")
    ok("registered 真跑 rc=0", r.returncode, 0, out.strip()[-80:])
    ok("registered 输出 RESULT 行", "RESULT:" in out, True)


# ------------------------------------------- [10] health 扩到调度器
def t_health_scheduler():
    h = _code(".workbench/scripts/health.py")
    ok("有 scheduler_state()", "def scheduler_state(" in h, True)
    ok("scan 返回 scheduler 键", '"scheduler": scheduler_state(now)' in h, True)
    ok("明确写了不参与退出码", '"in_exit_code": False' in h, True)
    ok("读不到写 None 而不是 FRESH/STALE",
       '"ok": None' in h, True)
    import health
    rep = health.scan()
    ok("scan 里带 scheduler", "scheduler" in rep, True, sorted(rep))
    sch = rep["scheduler"]
    ok("调度器状态有 ok", "ok" in sch, True, sorted(sch))
    ok("in_exit_code 为 False", sch.get("in_exit_code"), False, sch)
    # 取的是 scheduler.status()，不是抄一份
    ok("复用 scheduler.status 而非自算",
       "scheduler.status(now_ms)" in h, True)


def main():
    t_metrics_sidecar()
    t_read_sidecar()
    t_audit_static_red()
    t_revert_boundary()
    t_doc_numbers_verify()
    t_dream_tristate()
    t_boss_system()
    t_noise_registry()
    t_sched_split()
    t_health_scheduler()
    if FAILED:
        sys.stderr.write("R63 FAIL %d:\n" % len(FAILED))
        for f in FAILED:
            sys.stderr.write("  %s\n" % f)
        return 1
    sys.stderr.write("R63 PASS\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
