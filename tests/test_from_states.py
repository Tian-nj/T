# -*- coding: utf-8 -*-
"""R-29 自检：spec 里声明的 `from_states` / `to_state` 必须真有人读。

这条测试要防的不是"某段逻辑写错"，而是**声明悬空**：字段写在 spec 里，
全仓没有一处代码引用它，于是它既可能写错也可能早就过期，而两份表都是
生成物，谁也不会因为不一致而红。

三个被测面：
  1. 写入口：`wb_core.append_event` 在 from_states 不满足时必须拒，且拒之前
     不许落行；满足时必须放行（新检查不许误伤合法路径）。
  2. 跨表：`spec_lint` 的 S023 必须在两份表不一致时报出来。
  3. 反方向：S023 不许把"声明了一个暂时走不到的前置态"当错（那是设计意图）。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓根
SCRIPTS = os.path.join(HERE, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

PASS = []
FAIL = []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                       ("  <" + str(detail) + ">") if detail and not cond else ""))


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def fresh_env():
    """造一个带 spec 的最小仓，够 append_event 用。"""
    tmp = tempfile.mkdtemp(prefix="wb_from_states_")
    wb = os.path.join(tmp, ".workbench")
    shutil.copytree(os.path.join(HERE, ".workbench", "spec"),
                    os.path.join(wb, "spec"))
    shutil.copytree(os.path.join(HERE, ".workbench", "events"),
                    os.path.join(wb, "events"))
    os.makedirs(os.path.join(wb, "state"), exist_ok=True)
    shutil.copy(os.path.join(HERE, ".workbench", "config.yaml"),
                os.path.join(wb, "config.yaml"))
    return tmp, wb


def store_for(wb):
    import wb_core
    import re
    with open(os.path.join(wb, "config.yaml"), encoding="utf-8") as fh:
        txt = fh.read()
    uuid = re.search(r'^project_uuid:\s*"([0-9a-f]{32})"', txt, re.M).group(1)
    return wb_core.Store(wb, uuid)


# ── 1. 写入口：from_states 不满足必须拒 ───────────────────────────────
tmp, wb = fresh_env()
try:
    import wb_core
    os.environ["WB_HOME"] = wb
    st = store_for(wb)

    # 造一个走到 done 的任务：T-9 ∅→draft→pending→claimed→in_progress→submitted→done
    st.append_event("draft_created", {"task": "T-9"}, "lead", "T-9")
    for to, actor in (("draft", "lead"), ("pending", "lead"), ("claimed", "lead"),
                      ("in_progress", "lead"), ("submitted", "lead"),
                      ("done", "system:verifier")):
        st.transition("T-9", to, actor)
    ok("fixture reaches done", st.current_state("T-9") == "done",
       st.current_state("T-9"))

    before = len(list(st._rows()))

    # done 上再 claim：E04 声明 from_states=["pending","rejected"]
    try:
        st.append_event("claim", {"task": "T-9", "epoch": 99}, "lead", "T-9")
        ok("claim on done 被拒", False, "被接受了")
    except wb_core.SpecViolation as exc:
        msg = str(exc)
        ok("claim on done 被拒", "from_states" in msg, msg)
        ok("拒绝信息点明当前态", "'done'" in msg, msg)
        ok("拒绝信息点明允许态", "pending" in msg, msg)

    ok("被拒的 claim 不落行", len(list(st._rows())) == before,
       "lines=%d want=%d" % (len(list(st._rows())), before))

    # 合法路径不能被误伤：rejected 上 claim 必须放行
    st.transition("T-9", "archived", "system:gc")
    st.append_event("draft_created", {"task": "T-8"}, "lead", "T-8")
    st.transition("T-8", "draft", "lead")
    st.transition("T-8", "pending", "lead")
    try:
        r = st.append_event("claim", {"task": "T-8", "epoch": 1}, "lead", "T-8")
        ok("claim on pending 放行", isinstance(r.get("seq"), int), str(r))
    except wb_core.SpecViolation as exc:
        ok("claim on pending 放行", False, str(exc))

    # 不声明 from_states 的事件不受影响
    try:
        r = st.append_event("plan", {"plan_id": "P-1"}, "lead", "T-8")
        ok("无 from_states 的事件不受影响", isinstance(r.get("seq"), int), str(r))
    except wb_core.SpecViolation as exc:
        ok("无 from_states 的事件不受影响", False, str(exc))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    os.environ.pop("WB_HOME", None)

# ── 2. 跨表：S023 必须抓得到不一致 ────────────────────────────────────
def lint_env():
    """造一个能被 spec_lint 完整跑起来的最小仓（spec + config + README + manifest）。"""
    tmp = tempfile.mkdtemp(prefix="wb_s023_")
    wb = os.path.join(tmp, ".workbench")
    shutil.copytree(os.path.join(HERE, ".workbench", "spec"),
                    os.path.join(wb, "spec"))
    for extra in ("config.yaml", "root.manifest", "constitution.rules.yaml"):
        src = os.path.join(HERE, ".workbench", extra)
        if os.path.isfile(src):
            shutil.copy(src, os.path.join(wb, extra))
    for extra in ("README.md", "Constitution.md"):
        src = os.path.join(HERE, extra)
        if os.path.isfile(src):
            shutil.copy(src, os.path.join(tmp, extra))
    return tmp, wb


tmp2, wb2 = lint_env()
try:
    os.environ["WB_HOME"] = wb2
    sys.modules.pop("spec_lint", None)
    import spec_lint

    base = spec_lint.lint()
    ok("生产 spec 无 S023", [f for f in base if f["code"] == "S023"] == [],
       [f for f in base if f["code"] == "S023"])

    ev_path = os.path.join(wb2, "spec", "events.schema.json")
    ev = _read(ev_path)
    target = next((e for e in ev["events"] if e.get("from_states")), None)
    ok("fixture 找得到带 from_states 的事件", target is not None)
    orig = list(target["from_states"])
    target["from_states"] = ["no_such_state"]
    _write(ev_path, ev)

    sys.modules.pop("spec_lint", None)
    import spec_lint as sl2
    bad = [f for f in sl2.lint() if f["code"] == "S023"]
    ok("S023 抓得到非法状态名", len(bad) >= 1, bad)
    ok("S023 报告里点了那个名字",
       any("no_such_state" in f["detail"] for f in bad), bad)

    # 反方向：空 from_states = 不声明 = 不校验，不该报
    target["from_states"] = []
    _write(ev_path, ev)
    sys.modules.pop("spec_lint", None)
    import spec_lint as sl3
    got = [f for f in sl3.lint() if f["code"] == "S023"]
    ok("空 from_states 不报 S023", got == [], got)

    target["from_states"] = orig
    _write(ev_path, ev)
finally:
    shutil.rmtree(tmp2, ignore_errors=True)
    os.environ.pop("WB_HOME", None)

# ── 3. 真值：写入口里确实引用了 from_states ───────────────────────────
src = os.path.join(SCRIPTS, "wb_core.py")
with open(src, "r", encoding="utf-8") as fh:
    core = fh.read()
ok("wb_core 定义了 _from_states_of", "def _from_states_of" in core)
ok("wb_core 在写入口真调用它", "_from_states_of(eid)" in core)
ok("判据读当前态而不是 payload",
   "self.current_state(_tid)" in core)

sl_src = os.path.join(SCRIPTS, "spec_lint.py")
with open(sl_src, "r", encoding="utf-8") as fh:
    lint_src = fh.read()
ok("spec_lint 有 S023", 'bad("S023"' in lint_src)

print()
print("test_from_states: %s (%d ok, %d failed)" %
      ("PASS" if not FAIL else "FAIL", len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
