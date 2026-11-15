#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_state_sentinel.py — 「任务尚无状态」的哨兵名必须只有一个真相源。

背景
----
`from_states` 在 spec 里是 JSON 数组。`None` 在数组元素位置会被读成「不限制」
（与 `[]` 同义），于是「任务还没建」和「随便什么状态都行」变成同一个值。
所以引入字符串哨兵把它们分开，E02 draft_created 写成 `["(none)", "draft"]`。

问题
----
第三十四轮落地时，这个字面量在三个文件里各写了一份：

    gen_spec.py    ["(none)", "draft"]      ← 权威数据
    wb_core.py     NO_STATE = "(none)"      ← 私有副本
    spec_lint.py   _NO_STATE = "(none)"     ← 私有副本

只有第一份进 spec、进 manifest。改任一份副本的后果是：
**写入口用新值判、spec 里还是旧值 → 每次写 E02 都被拒，而 lint 一路绿。**
这正是本仓反复在打的「声明悬空」的镜像形态——三份字面量里两份没人校验。

判据分三层
----------
1. 行为层：写入口与 lint 读到的哨兵与 spec 里的一致（不是各自记着的那个）。
2. 真值层：改 spec 里的值，两边真的跟着变（证明是读，不是巧合相等）。
3. 结构层：源码里除 gen_spec.py 外不许再出现该字面量（证明没有第三份）。

第 3 层是这条测试真正的价值。前两层能过而第 3 层不过，说明"现在一致"是
因为恰好抄对了，下一次有人再抄一遍就还是同一个病。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.path.dirname(HERE)
SCRIPTS = os.path.join(WB, ".workbench", "scripts")
SPEC = os.path.join(WB, ".workbench", "spec")
sys.path.insert(0, SCRIPTS)

import wb_core  # noqa: E402
import spec_lint  # noqa: E402

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


FAILS = []


def ok(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s   %s" % (name, detail))
        FAILS.append(name)


def _read(path):
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _read_bytes(path):
    with io.open(path, "rb") as fh:
        return fh.read()


def _write_bytes(path, blob):
    """按字节还原。

    为什么不能「读文本再写回」：Windows 上 `io.open(..., "w")` 默认会把 \\n
    翻译成 \\r\\n，而这个文件是 gen_spec.py 用 `newline="\\n"` 写出来的纯 LF。
    于是「改一改再恢复」这一步本身就把 328 个换行全改了 —— 内容看起来一样，
    字节不一样，`sign_manifest --verify` 立刻报 `DRIFT spec/states.json`。
    这正是本仓反复踩的那类坑：**恢复不等于还原**。
    """
    with io.open(path, "wb") as fh:
        fh.write(blob)


def _spec_sentinel():
    with io.open(os.path.join(SPEC, "states.json"), "r", encoding="utf-8") as fh:
        return ((json.load(fh).get("sentinel") or {}).get("no_state") or "")


# ---------------------------------------------------------------- 1 行为层
def t_consumers_read_the_same_value():
    print("[1] 消费者读到的哨兵与 spec 一致")
    want = _spec_sentinel()
    ok("spec 里有 sentinel.no_state", bool(want), repr(want))
    ok("wb_core.no_state_sentinel() 等于 spec",
       wb_core.no_state_sentinel() == want,
       "%r != %r" % (wb_core.no_state_sentinel(), want))
    ok("_state_key(None) 就是哨兵",
       wb_core._state_key(None) == want, repr(wb_core._state_key(None)))
    ok("_state_key('draft') 原样返回",
       wb_core._state_key("draft") == "draft")
    ok("哨兵不出现在 states 列表里（它不是状态名）",
       want not in json.load(io.open(os.path.join(SPEC, "states.json"),
                                     encoding="utf-8"))["states"])


# ---------------------------------------------------------------- 2 真值层
def t_changing_spec_changes_consumers():
    """把 spec 里的哨兵改掉，wb_core 必须跟着变——证明是读，不是巧合。"""
    print("[2] 改 spec，消费者跟着变")
    path = os.path.join(SPEC, "states.json")
    raw = _read_bytes(path)
    orig = _read(path)
    fake = "%%changed-%d%%" % os.getpid()
    try:
        doc = json.loads(orig)
        doc["sentinel"]["no_state"] = fake
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True))
        # mtime 失效缓存按 ns 计时，某些文件系统分辨率下同一纳秒内会读不到变化。
        os.utime(path, None)
        got = wb_core.no_state_sentinel()
        ok("wb_core 读到了改后的值", got == fake, "%r != %r" % (got, fake))
        ok("_state_key(None) 跟着变", wb_core._state_key(None) == fake)
    finally:
        _write_bytes(path, raw)
        os.utime(path, None)
        wb_core._SENTINEL_CACHE["stamp"] = None
    ok("恢复后回到原值", wb_core.no_state_sentinel() == _spec_sentinel())
    ok("恢复后字节完全一致（恢复≠还原的防回归）", _read_bytes(path) == raw)


def t_missing_sentinel_is_loud():
    """spec 缺 sentinel 段时必须抛，不能回落内置字面量。"""
    print("[3] 缺 sentinel 段时拒绝静默回落")
    path = os.path.join(SPEC, "states.json")
    raw = _read_bytes(path)
    orig = _read(path)
    try:
        doc = json.loads(orig)
        doc.pop("sentinel", None)
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True))
        os.utime(path, None)
        wb_core._SENTINEL_CACHE["stamp"] = None
        raised = None
        try:
            wb_core.no_state_sentinel()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        ok("缺段时抛异常", raised is not None, repr(raised))
        ok("异常类型是 SpecViolation",
           isinstance(raised, wb_core.SpecViolation), type(raised).__name__)
        ok("异常信息点名 states.json",
           raised is not None and "states.json" in str(raised), str(raised))
    finally:
        _write_bytes(path, raw)
        os.utime(path, None)
        wb_core._SENTINEL_CACHE["stamp"] = None
    ok("恢复后仍可读", bool(wb_core.no_state_sentinel()))
    ok("恢复后字节完全一致（第二处）", _read_bytes(path) == raw)


# ---------------------------------------------------------------- 3 结构层
def t_no_extra_copies_in_source():
    """除 gen_spec.py 外，脚本源码里不许再出现该字面量。

    这一层才是「只有一个真相源」的判据。前两层即使不过，也可能只是抄错了；
    这一层如果不过，说明抄这件事本身还在被允许。
    """
    print("[4] 源码里没有第三份副本")
    want = _spec_sentinel()
    ok("哨兵值非空（否则下面的搜索无意义）", bool(want), repr(want))
    needle = '"%s"' % want
    offenders = []
    # 只扫**本源**。`.workbench/wt/**` 是任务 worktree，里面是一整份仓库副本：
    # 扫它们等于把「别人分支上的旧代码」当成本仓的第三份副本 —— T-R38 的
    # worktree 里就躺着一份 gen_spec.py 和一份本测试自身，于是这条检查
    # 在「工作树存在时红、不存在时绿」，与它要证明的事毫无关系。
    # 判据必须落在本源上的原因：守的是「本仓只有一个真相源」，
    # 不是「磁盘上任何角落都不许有那个字符串」。
    skip_dirs = {"__pycache__", "wt"}
    for root, dirs, files in os.walk(os.path.join(WB, ".workbench")):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, os.path.join(WB, ".workbench")).replace("\\", "/")
            if rel == "scripts/gen_spec.py":
                continue          # 权威源，允许出现
            text = _read(path)
            if needle in text:
                offenders.append(rel)
    ok("除了 gen_spec.py 没有别的文件硬写哨兵", not offenders, repr(offenders))

    # gen_spec.py 自己也必须只写一次（写两次等于内部又分叉了）
    gen = _read(os.path.join(SCRIPTS, "gen_spec.py"))
    ok("gen_spec.py 里有且只有一处字面量", gen.count(needle) == 1,
       "count=%d" % gen.count(needle))
    ok("gen_spec.py 用常量承载它", "STATE_SENTINEL = " in gen)
    ok("事件表引用常量而不是字面量",
       "[STATE_SENTINEL, \"draft\"]" in gen)


def t_lint_and_writer_agree():
    """把事件链真正拿来跑一遍：E02 必须能写，且 lint 不报 S023。

    行为层与结构层都过了也不代表两边接的是同一个值——这条做端到端确认。
    """
    print("[5] 端到端：E02 写得进去，lint 不报 S023")
    tmp = _mkdtemp(prefix="wb_sentinel_")
    wb = os.path.join(tmp, ".workbench")
    for sub in ("spec", "events", "state"):
        os.makedirs(os.path.join(wb, sub), exist_ok=True)
    for fn in os.listdir(SPEC):
        with io.open(os.path.join(SPEC, fn), "r", encoding="utf-8") as src:
            with io.open(os.path.join(wb, "spec", fn), "w", encoding="utf-8") as dst:
                dst.write(src.read())
    with io.open(os.path.join(wb, "config.yaml"), "w", encoding="utf-8") as fh:
        fh.write("project_uuid: %s\n" % ("0" * 32))

    st = wb_core.Store(wb, "0" * 32)
    raised = None
    try:
        e = st.append_event("draft_created", {"task": "T-S1"}, "lead", "T-S1")
    except Exception as exc:  # noqa: BLE001
        e, raised = None, exc
    ok("E02 在无状态任务上可写", raised is None, repr(raised))
    ok("E02 落了链", e is not None and e.get("seq") == 1, repr(e))
    # E02 的 to_state 是 None —— 它只记「草案被创建」这件事，不改状态；
    # 真正建状态的是 E08/T01。这条断言把这两件事分开，避免以后有人把
    # 「写了 draft_created」误当成「任务已经在 draft」。
    ok("E02 本身不推状态（建状态由 T01 承接）",
       st.current_state("T-S1") is None, repr(st.current_state("T-S1")))
    st.transition("T-S1", "draft", "lead")
    ok("T01 之后任务状态是 draft", st.current_state("T-S1") == "draft")
    # 哨兵的意义在这一步体现：T-S2 从没建过，claim(E04) 只允许
    # pending/rejected，所以必须被拒——哨兵不是"什么都能进"。
    raised2 = None
    try:
        st.append_event("claim", {"task": "T-S2", "epoch": 1}, "lead", "T-S2")
    except Exception as exc:  # noqa: BLE001
        raised2 = exc
    ok("无状态任务上 claim 仍被拒（哨兵不是万能通行证）",
       raised2 is not None and "E04" in str(raised2), repr(raised2))
    # 而 E02 对同一个无状态任务是放行的——两者的差别正是哨兵在做功。
    raised3 = None
    try:
        st.append_event("draft_created", {"task": "T-S3"}, "lead", "T-S3")
    except Exception as exc:  # noqa: BLE001
        raised3 = exc
    ok("同一无状态任务上 draft_created 放行（哨兵让它合法）",
       raised3 is None, repr(raised3))


def main():
    for fn in (t_consumers_read_the_same_value,
               t_changing_spec_changes_consumers,
               t_missing_sentinel_is_loud,
               t_no_extra_copies_in_source,
               t_lint_and_writer_agree):
        fn()
    print()
    if FAILS:
        print("test_state_sentinel: FAIL (%d)" % len(FAILS))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("test_state_sentinel: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
