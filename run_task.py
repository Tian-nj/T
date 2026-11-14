#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_task.py —— 一条命令，把一个任务从零推到合入主干。

## 它解决的是什么

前面每一环都单独能用，但把它们串起来需要十几次手工调用，而且**顺序与前提
散在人的记忆里**：先 task-new 再 approve，approve 前要 digest 自洽，claim
之后才能派发执行体，执行体写完才能 finish，finish 之后才能 verify，verify
之后还要过闸门，过完闸门还要有人签名，签完名才能真合。漏一步的报错都不
指向"你漏了哪一步"，而指向"当前这步为什么不行"。

`run_task.py` 把这条串起来，并且**按当前状态决定下一步**——它不是一份写死
的脚本，是一个只做"下一步该干什么"的驱动器。这让它能从任何中断点续跑：
执行体是外部 subagent 时，你跑一次到 `prepare` 就退出，subagent 干完活再
跑一次，它接着往下推。

## 为什么是"按状态驱动"而不是"照脚本念"

写死顺序的版本在第一次被打断时就废了：subagent 跑了 20 分钟，脚本那头早就
退出了。而状态在链上，链是持久的。**驱动器的记忆必须和系统同源**，否则
两个记忆一吵架，人就得去猜哪个是真的。

## 三个门槛它不替人跨

1. **执行体要真干活。** `--executor local_process` 需要一个入口脚本；
   `harness_subagent` 需要真的派一个 subagent。驱动器只负责准备与收尾，
   它不假装自己写完了代码。
2. **签名。** 闸门要单签/双签时默认**停下并打印签名命令**。加 `--sign-as`
   才会自动签，并且会在输出里明说这一步跳过了人工确认——把"谁按了那个
   按钮"写清楚，是让闸门保持可审计的最低要求。
3. **合并。** 只有签名到位、闸门放行才真合；其余情况一律 dry-run 收场。

## 用法

    python run_task.py --title "给 stats 加 spread" --scope src/stats.py \\
        --scope tests/ --cmd "python tests/test_stats_spread.py" \\
        --executor local_process --entrypoint tests/wb_demo_stats.py

    python run_task.py --resume                 # 接着上次未完成的任务往下推
    python run_task.py --task T-0007 --resume   # 指定任务续跑
    python run_task.py --title "..." --dry      # 只打印计划，不落任何事件

退出码：0 已落到主干（或无可做之事）／20 等待外部执行体／30 等待签名／
1 某一步真的失败。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.join(HERE, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
PY = sys.executable

EXIT_OK, EXIT_FAIL, EXIT_WAIT_EXECUTOR, EXIT_WAIT_SIGN = 0, 1, 20, 30
# 合入后被 smoke 挡回（E15 写了、E16 也写了）。必须与 EXIT_OK 分开：
# 「合入」和「合入又撤回」在退出码上同值，脚本化调用方就会把回滚当成功。
EXIT_REVERTED = 40
# 预演通过：「这一笔**可以**合」，还没合。与 EXIT_OK 分开是必须的 ——
# 同一个值既表示「能合」又表示「已合」，中间那次真正的合并就会被静默跳过。
EXIT_MERGE_READY = 21
# 这一笔早就在主干上了（merge_queue 的 SKIP already-merged）。
# 它与「预演通过」是两件事：一个还没合，一个是合过了。
EXIT_ALREADY_MERGED = 22

sys.path.insert(0, SCRIPTS)

# 过程行开关（第三十九轮）。`[True]` 而不是裸 bool：`main()` 里要改它，
# 而模块级赋值会让别的模块 import 到旧值的那份副本。
VERBOSE = [False]


def log(*parts, **kw):
    """搭者日志。只在 --verbose / --dry 下打。

    为什么不直接 `print`：过程行与结论行同源时，「哪一行是结果」要靠人判断。
    汇报的唯一出口是 `boss_report`；这里的每一条都是过程。
    """
    if VERBOSE[0]:
        print(*parts, **kw)


def warn(*parts, **kw):
    """真的要让人看见的过程提示（REFUSE 原因、等你动手的下一步）。

    这类行不是进度噪声 —— 它们是「现在轮到你做事了」，静默掉会让命令
    看起来"什么都没做"，比多打一行坏得多。
    """
    print(*parts, **kw)

# 每一步的名字 → 一句话说明。打印进度用，也是 --dry 的输出骨架。
STEP_DOC = {
    "new": "task-new：建草稿 + acceptance.draft.yaml",
    "approve": "task-approve：digest 自洽 → 转正进 pending",
    "dispatch": "dispatch：预算/依赖/scope 冲突三关过后选批",
    "claim": "task-claim：领租约，落到 claimed",
    "prepare": "agent_runner prepare：建 worktree + brief + job",
    "run": "agent_runner run：派发执行体（local_process）",
    "finish": "agent_runner finish：读 sentinel → 复核写域 → submit",
    "verify": "task-verify：跑验收命令，写 E10",
    "merge": "merge_queue：过闸门 → 合入",
    "sign": "human_cli：补签（仅在缺签名时）",
}


# ---------------------------------------------------------------- 基础设施

def env():
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    e.setdefault("WORKBENCH_ROOT_KEY", "dev-root-key-2026-please-replace")
    return e


def sh(*args, quiet=False):
    """跑一个仓内脚本，回 (rc, 输出)。不用 shell，路径里有空格也不怕。

    过程行（命令与回显）只在 `--verbose` 或 `--dry` 下打。理由不是"少打几行"：
    BOSS 看到的输出里混进十几行 `$ python lead.py ...` 之后，**结论那三行就
    不再是唯一显眼的东西**，而"哪一行才是结果"这件事一旦要靠人猜，
    下一次真出问题时也会被当成背景噪声划过去。结论由 `boss_report` 统一出口，
    过程留给 `--verbose`。
    """
    cmd = [PY] + list(args)
    if VERBOSE[0] and not quiet:
        print("\n$ %s" % " ".join(_short(x) for x in cmd))
    p = subprocess.run(cmd, cwd=HERE, env=env(),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", "replace")
    if VERBOSE[0] and not quiet:
        for line in out.rstrip().splitlines():
            print("  %s" % line)
    return p.returncode, out


def _short(x):
    return os.path.basename(x) if os.sep in x or "/" in x else x


def store():
    """与 lead.py 的入口同一口径：先读冻结标记，再用 manifest 的 uuid 开仓。

    这里没有直接 `Store(WB, uuid)` 的捷径可走：`Store.__init__` 要两个位置
    参数，而 uuid 的唯一来源是 `sign_manifest.read_uuid_from_config()`（它自己
    会去 config.yaml 找）。少一步就是 `TypeError`，而那个报错不会告诉你
    "你少读了冻结标记"。
    """
    import wb_core
    import sign_manifest
    wb_core.load_frozen(WB)
    return wb_core.Store(WB, sign_manifest.read_uuid_from_config())


def cfg():
    import wb_core
    return wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))


def state_of(st, tid):
    return (st.rebuild()["tasks"].get(tid) or {}).get("state")


def latest_task_id(st):
    """链上最近一次 draft_created 的任务号 —— `--resume` 不带 --task 时用它。"""
    hit = None
    for row in st._rows():
        if row.get("kind") == "draft_created":
            t = (row.get("payload") or {}).get("task") or row.get("task")
            if t:
                hit = t
    return hit


def next_task_id(st):
    """下一个任务号 = 已用过的最大号 + 1，**不是**第一个空号。

    这条差别是真踩过的：T-0002 / T-0003 早已合入主干并从 `rebuild()` 的
    tasks 里消失（它们现在只剩历史），于是"第一个空号"会重新发 T-0002 ——
    而 `task/T-0002` 这个分支还在，merge_queue 会把它当成"已合并"跳过，
    新任务则永远卡在 done 而不进主干。号一旦发出去就不能回收，
    所以取最大号 +1，让回收这件事在编号空间里根本不存在。
    """
    hi = 0
    for row in st._rows():
        for cand in ((row.get("payload") or {}).get("task"), row.get("task")):
            m = re.fullmatch(r"T-(\d+)", str(cand or ""))
            if m:
                hi = max(hi, int(m.group(1)))
    for name in _task_dir_names():
        m = re.fullmatch(r"T-(\d+)", name)
        if m:
            hi = max(hi, int(m.group(1)))
    return "T-%04d" % (hi + 1)


def _task_dir_names():
    """`tasks/` 下的目录名；目录不存在时给空集。

    这里刻意不 `os.makedirs`：编号分配是一个**问句**，它不该顺手创建东西。
    一个 `--dry` 的调用把 `.workbench/tasks` 建出来，是副作用泄漏到只读路径。
    """
    try:
        return os.listdir(os.path.join(WB, "tasks"))
    except OSError:
        return []


# ---------------------------------------------------------------- 各步

def do_new(a, st):
    args = [os.path.join(SCRIPTS, "lead.py"), "task-new",
            "--task", a.task, "--title", a.title,
            "--priority", a.priority]
    for s in (a.scope or []):
        args += ["--scope", s]
    args += ["--cmd", a.cmd]
    rc, out = sh(*args)
    return rc


def do_approve(a, st):
    """Lead 备料（digest / acceptance.yaml / E03 / T02）+ 人工转正（E32）。

    D-53：转正这一步不改变状态（T02 已把 draft→pending 走完），所以
    「按状态查表」的驱动器看不见它 —— 与 D-52 的 sentinel 同一类问题。
    若把备料和转正拆开，就会留下一笔「pending 却没有 E32」的半成品：
    cmd_task_verify 的 digest↔链比对必然判 ACCEPTANCE_INVALID，而
    resume 又回不到 approve。这两件事必须在同一步里一次做完。
    """
    rc, out = sh(os.path.join(SCRIPTS, "lead.py"), "task-approve", "--task", a.task)
    if rc != 0:
        return rc
    return ensure_acceptance_approved(a, st)


def do_acceptance_approve(a, st):
    """两段式写 E32：先 --sub sign 取签名，再带三参提交。

    args 只需 task —— digest 由 human_cli 从 acceptance.yaml 读出来核对，
    人显式带的值一律不采信（§6.2 / D-53，防「换个地方自己签」）。
    """
    args = json.dumps({"task": a.task}, ensure_ascii=False)
    rc, out = sh(os.path.join(SCRIPTS, "human_cli.py"), "--sub", "sign",
                 "--principal", a.sign_as, "--op", "acceptance_approve",
                 "--args", args)
    if rc != 0:
        return rc
    try:
        blob = json.loads(out[out.index("{"):])
    except (ValueError, IndexError):
        warn("  REFUSE：签名器没吐出可解析的 JSON")
        return EXIT_FAIL
    rc, _ = sh(os.path.join(SCRIPTS, "human_cli.py"),
               "--principal", a.sign_as, "--op", "acceptance_approve",
               "--args", args,
               "--nonce", blob["nonce"], "--ts_ms", str(blob["ts_ms"]),
               "--sig", blob["sig"])
    return rc


def ensure_acceptance_approved(a, st):
    """幂等确保链上有该任务的 E32（人工转正）。

    历史任务（E32 曾由 lead 写，R-26）按 kind 取值、不按 actor 过滤，
    一律视为已转正，重跑不翻脸。真正要补的是新流程里「备料完成、
    转正还没签」的窗口。
    """
    for r in st._rows():
        if r.get("kind") == "acceptance_approved" and r.get("task") == a.task:
            return EXIT_OK
    # 没有备料产物的假任务（测试构造的 claimed 之类）不在此补转正。
    acc_path = os.path.join(WB, "tasks", str(a.task), "acceptance.yaml")
    if not os.path.exists(acc_path):
        return EXIT_OK
    if not a.sign_as:
        hint = json.dumps({"task": a.task}, ensure_ascii=False)
        warn("\n⏸  验收标准未经人工转正（链上缺 E32）。加 --sign-as <principal> 自动转正，或手工：")
        warn("   python .workbench/scripts/human_cli.py --principal <p> "
             "--op acceptance_approve --args '" + hint + "'")
        warn("   （先 --sub sign 拿 nonce/ts_ms/sig，再带上三个参数提交；"
             "digest 由签名器读文件，不用手填）")
        return EXIT_WAIT_SIGN
    warn("\n  ⚠ --sign-as %s：本次验收转正由操作者显式授权自动签名，"
         "人工确认环节被跳过。这一行会留在输出里作为凭据。" % a.sign_as)
    return do_acceptance_approve(a, st)


def do_dispatch(a, st):
    """dispatch 是批选，不是点名单：它挑的是"所有够格的任务"。

    所以这里只做**能不能被选中**的预检，不去断言"一定选了它"——
    选了别人而没选它，可能是 scope 冲突或预算，这不是本次调用的失败。
    """
    rc, out = sh(os.path.join(SCRIPTS, "lead.py"), "dispatch", "--json")
    if rc != 0:
        return rc
    try:
        j = json.loads(out[out.index("{"):])
    except (ValueError, IndexError):
        return 0
    picked = [r["task"] for r in (j.get("chosen") or [])]
    if a.task in picked:
        log("  → %s 在本批计划内（plan=%s）" % (a.task, j.get("plan_id")))
        return 0
    why = [s.get("why") for s in (j.get("skipped") or []) if s.get("task") == a.task]
    warn("  ! %s 未被选入本批：%s" % (a.task, why[0] if why else "（不在候选里）"))
    return 0


def do_claim(a, st):
    rc, out = sh(os.path.join(SCRIPTS, "lead.py"), "task-claim", "--task", a.task)
    return rc


def _vp(args):
    """把 --verbose 透传给子脚本（第四十三轮）。

    不透传的话，`run_task.py --verbose` 只能看到自己发出的命令与整段回显，
    而 agent_runner 的 spawn/state 过程行被它自己的开关吞在子进程里 ——
    两个 --verbose 各管一半，用户以为开了一个开关。
    """
    return args + ["--verbose"] if VERBOSE[0] else args


def do_prepare(a, st):
    args = _vp([os.path.join(SCRIPTS, "agent_runner.py"), "prepare",
                "--task", a.task, "--role", a.role, "--executor", a.executor])
    if a.objective:
        args += ["--objective", a.objective]
    if a.base:
        args += ["--base", a.base]
    rc, out = sh(*args)
    if rc == 0:
        for line in out.splitlines():
            if line.strip().startswith("job "):
                a.job_path = line.split(None, 1)[1].strip()
    return rc


def do_run(a, st):
    if a.executor != "local_process":
        # D-51：这里原本漏了 `% a.executor`，真跑 harness_subagent 时把
        # `executor=%s` 原样打印出来 —— 用户看到的是一句带占位符的话，
        # 却无从知道驱动器到底停在哪个执行体上。占位符漏填和写错值不同：
        # 写错值会被怀疑，漏填却被当成「系统就是这么写的」。
        warn("  → executor=%s：驱动器只准备，不代跑。派发完再 --resume。"
             % a.executor)
        return EXIT_WAIT_EXECUTOR
    if not a.entrypoint:
        warn("  REFUSE：--executor local_process 需要 --entrypoint（执行体脚本）")
        return EXIT_FAIL
    args = _vp([os.path.join(SCRIPTS, "agent_runner.py"), "run",
                "--task", a.task, "--role", a.role,
                "--executor", "local_process", "--entrypoint", a.entrypoint,
                "--submit", "--wait", str(a.wait)])
    rc, out = sh(*args)
    return rc


def do_finish(a, st):
    """外部执行体收尾：sentinel 已在，让 runner 复核写域并 submit。"""
    args = _vp([os.path.join(SCRIPTS, "agent_runner.py"), "finish",
                "--task", a.task, "--role", a.role])
    if a.base:
        args += ["--base", a.base]
    args += ["--submit"]
    rc, out = sh(*args)
    return rc


def do_verify(a, st):
    wt = os.path.join(WB, "wt", a.task)
    rc, out = sh(os.path.join(SCRIPTS, "lead.py"), "task-verify",
                 "--task", a.task, "--worktree", wt)
    return rc


def _merge_reverted(st, tid):
    """这条任务最近一次 merge 是否已被 merge_reverted 撤回。

    为什么不能只看 merge_queue 的输出：它对「合入后 smoke 失败并撤销」
    打的是先 MERGED 再 FROZEN 两行，而 MERGED 那一行里带着任务名。
    只匹配 MERGED 的话，一次被回滚掉的合入会被读成成功 —— 于是驱动器
    打出「✅ 成了」，主干上却什么都没有。链是唯一权威：E16 的 seq 比
    E15 大，就说明那笔合入已经退回来了。
    """
    last_merge = 0
    last_revert = 0
    for r in st._rows():
        if r.get("task") != tid:
            continue
        k = r.get("kind")
        if k == "merge":
            last_merge = max(last_merge, int(r.get("seq") or 0))
        elif k == "merge_reverted":
            last_revert = max(last_revert, int(r.get("seq") or 0))
    return last_revert > last_merge > 0


def do_merge(a, st, dry_only=False):
    args = [os.path.join(SCRIPTS, "merge_queue.py")]
    if dry_only:
        args += ["--dry-run"]
    rc, out = sh(*args)
    merged = re.search(r"MERGED\s+%s\b" % re.escape(a.task), out)
    if merged:
        if dry_only:
            # ★ 预演里的 MERGED 意思是「这一笔**可以**合」，不是「已经在主干」。
            # 第三十七轮的真事：预演打出 MERGED，驱动器却把它读成「已经在主干」，
            # 于是直接 return OK 收工 —— 真正的合并一次都没发生，而输出说 ✅。
            # 「可以合」与「已合」必须分成两个返回值，否则中间那步签名+合并会被跳过。
            return EXIT_MERGE_READY
        if _merge_reverted(st, a.task):
            return EXIT_REVERTED
        return EXIT_OK
    if re.search(r"WAIT\s+%s\b" % re.escape(a.task), out):
        return EXIT_WAIT_SIGN
    if re.search(r"GATED\s+%s\b" % re.escape(a.task), out):
        return EXIT_WAIT_SIGN
    # 「已经在主干」是 merge_queue 用 SKIP 说的，不是用 MERGED 说的。
    # 三种结局必须分开：可以合（MERGED 预演）/ 已经在主干（SKIP）/ 要签名（WAIT|GATED）。
    if re.search(r"SKIP\s+%s\b" % re.escape(a.task), out) and "already merged" in out:
        return EXIT_ALREADY_MERGED
    return rc if rc != 0 else EXIT_OK


def _boss_report(a):
    """把这条任务渲染成给 BOSS 看的三段（结论/依据/风险）。

    为什么放在驱动器末尾而不是每步都打：中间态天天变，每次都汇报等于让 BOSS
    读日志；汇报应该发生在"这次驱动走到头了"的时候。读失败不吞 —— 渲染层是
    给人看的，它坏了就该看见，而不是静默回退成"没有汇报"。
    """
    try:
        import boss_report
        st = store()
        rep = boss_report.render(st, a.task)
    except Exception as exc:                                   # noqa: BLE001
        print("\n[汇报渲染失败] %s: %s（这是渲染层的 bug，任务本身状态不受影响）"
              % (type(exc).__name__, exc))
        return
    print("\n" + "=" * 66)
    print(boss_report.text(rep))
    print("=" * 66)


def do_sign(a, st):
    """补签：先问签名器要一份签名，再用它提交。

    这里刻意分成两次调用（`--sub sign` 与正式提交），因为签名器的契约就是
    "签名由调用方保管、由提交方校验"——把它合成一步，就等于允许"提交的
    那个人顺手把名也签了"，而闸门存在的意义正是这两件事不是同一个人做的。
    `--sign-as` 把这条界限明确交给了操作者本人。
    """
    args = json.dumps({"task": a.task}, ensure_ascii=False)
    rc, out = sh(os.path.join(SCRIPTS, "human_cli.py"), "--sub", "sign",
                 "--principal", a.sign_as, "--op", "human_approve",
                 "--args", args)
    if rc != 0:
        return rc
    try:
        blob = json.loads(out[out.index("{"):])
    except (ValueError, IndexError):
        warn("  REFUSE：签名器没吐出可解析的 JSON")
        return EXIT_FAIL
    rc, out2 = sh(os.path.join(SCRIPTS, "human_cli.py"),
                  "--principal", a.sign_as, "--op", "human_approve",
                  "--args", args,
                  "--nonce", blob["nonce"], "--ts_ms", str(blob["ts_ms"]),
                  "--sig", blob["sig"])
    return rc


# ---------------------------------------------------------------- 驱动器

NEXT = {
    None: "new",
    "draft": "approve",
    "pending": "dispatch",
    "rejected": "dispatch",
    "claimed": "prepare",
    "in_progress": "finish",
    "submitted": "verify",
    "done": "merge",
}


def drive(a, st):
    """按当前状态一路推到不能推为止。返回退出码。"""
    for hop in range(12):
        cur = state_of(st, a.task) if a.task in st.rebuild()["tasks"] else None
        step = NEXT.get(cur)
        log("\n──── 状态=%r → 下一步=%s（%s）" % (cur, step, STEP_DOC.get(step, "")))

        if step is None:
            warn("  %r 不是可自动推进的状态；需要人工判断。" % cur)
            return EXIT_FAIL

        if step == "new":
            if not a.title:
                warn("  REFUSE：建任务需要 --title")
                return EXIT_FAIL
            if a.dry:
                log("  [dry] 会建 %s" % a.task)
                return EXIT_OK
            rc = do_new(a, st)
            if rc != 0:
                return rc
            continue

        if a.dry:
            log("  [dry] 到此为止（--dry 不写任何事件）")
            return EXIT_OK

        if step == "approve":
            rc = do_approve(a, st)
            if rc != 0:
                return rc
            continue

        if step == "dispatch":
            do_dispatch(a, st)
            rc = do_claim(a, st)
            if rc != 0:
                return rc
            continue

        if step == "prepare":
            # D-52：外部执行体写完 sentinel 后，链上状态仍停在 claimed ——
            # prepare 不迁移状态，只有 finish 里的 announce_worker 才写
            # claimed→in_progress。签署落在 sidecar，不在链上，按状态查表
            # 看不见它；resume 若仍无条件 prepare，就会重写 job 再等一次，
            # 死循环，finish 永远选不中。先读签署：有签署就直接收尾。
            sentinel = os.path.join(WB, "state", "agent_done",
                                    "%s.%s.json" % (a.task, a.role))
            if a.executor != "local_process" and os.path.exists(sentinel):
                log("  → 已有执行体签署 %s，跳过 prepare 直接收尾（finish）"
                      % os.path.basename(sentinel))
                rc = do_finish(a, st)
                if rc != 0:
                    return rc
                continue
            # 先把"这个 executor 到底能不能跑"问清楚，再动 worktree。
            # 原版把这句留在 do_run 里，于是缺 entrypoint 时 prepare 已经
            # 建好了 worktree、写好了 brief 和 job —— 拒绝是对的，但代价是
            # 留下一套没人用的产物，而且 .workbench/wt 只会越攒越多。
            # 拒绝要发生在产生副作用之前。
            if a.executor == "local_process" and not a.entrypoint:
                warn("  REFUSE：--executor local_process 需要 --entrypoint（执行体脚本）")
                warn("  （已在 prepare 之前拦下，worktree 未创建）")
                return EXIT_FAIL
            rc = do_prepare(a, st)
            if rc != 0:
                return rc
            rc = do_run(a, st)
            if rc == EXIT_WAIT_EXECUTOR:
                warn("\n⏸  执行体已被指引（executor=%s）。等它写完后：" % a.executor)
                warn("   python run_task.py --task %s --resume" % a.task)
                return EXIT_WAIT_EXECUTOR
            if rc != 0:
                return rc
            continue

        if step == "finish":
            rc = do_finish(a, st)
            if rc != 0:
                return rc
            continue

        if step == "verify":
            rc = do_verify(a, st)
            if rc != 0:
                return rc
            continue

        if step == "merge":
            rc = do_merge(a, st, dry_only=True)
            if rc == EXIT_ALREADY_MERGED:
                log("\n✅ %s 已在主干（merge_queue 判为 already merged）。" % a.task)
                return EXIT_OK
            if rc == EXIT_MERGE_READY:
                log("\n  ✓ 预演通过：这一笔可以合（还没合）。")
            elif rc not in (EXIT_WAIT_SIGN, EXIT_OK):
                return rc
            if not a.sign_as:
                warn("\n⏸  闸门要签名。加 --sign-as <principal> 自动签，或手工：")
                warn("   python .workbench/scripts/human_cli.py "
                      "--principal <p> --op human_approve --args '{\"task\":\"%s\"}'"
                      % a.task)
                warn("   （先 --sub sign 拿 nonce/ts_ms/sig，再带上三个参数提交）")
                return EXIT_WAIT_SIGN
            warn("\n  ⚠ --sign-as %s：本次由操作者显式授权自动签名，"
                  "闸门的人工确认环节被跳过。这一行会留在输出里作为凭据。"
                  % a.sign_as)
            rc = do_sign(a, st)
            if rc != 0:
                return rc
            rc = do_merge(a, st, dry_only=False)
            if rc == EXIT_OK:
                log("\n✅ %s MERGED" % a.task)
                return EXIT_OK
            if rc == EXIT_REVERTED:
                warn("\n🔴 %s 合入后被 smoke 挡回，已撤回到合并前的主干。" % a.task)
                warn("   这不是成功：E15 是历史，E16 才是现状。看下面的风险段。")
                return EXIT_REVERTED
            return rc

    warn("\nREFUSE：推进超过 12 步仍未收敛，可能存在状态抖动。")
    return EXIT_FAIL


# ---------------------------------------------------------------- 多任务 DAG 驱动
#
# 单任务走 drive()/NEXT 表；--plan 走 plan_drive()。这里的三个真读者
# （audit_plan / validate_handoff / enums 契约）由 team_protocol 提供，
# team_protocol._driver_probe() 反过来读本文件源码确认接线，形成闭环：
# 契约声明不算数，源码里真有调用才算数。

def _plan_batch_max_parallel():
    """global_config.batch_max_parallel；读不到保守取 3（不依赖可选库）。"""
    try:
        import wb_core
        return int(wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))
                   ["global_config"]["batch_max_parallel"])
    except Exception:
        pass
    try:
        with open(os.path.join(WB, "config.yaml"), encoding="utf-8") as fh:
            m = re.search(r"^\s*batch_max_parallel:\s*(\d+)", fh.read(), re.M)
            if m:
                return int(m.group(1))
    except OSError:
        pass
    return 3


def _plan_read(path, label):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except (OSError, ValueError) as exc:
        return None, "%s 不可读: %s" % (label, exc)


def _plan_check_audit(pa):
    """plan_audit.json：result=PASS + 三权分离 + checks 覆盖 A1..A10。"""
    if not isinstance(pa, dict):
        return ["plan_audit 不是对象"]
    errs = []
    if pa.get("result") != "PASS":
        errs.append("plan_audit.result != PASS（实得 %r）" % pa.get("result"))
    auditor, producer, dispatcher = (pa.get("auditor"), pa.get("producer"),
                                     pa.get("dispatcher"))
    if not (auditor and producer and dispatcher):
        errs.append("plan_audit 缺 producer/auditor/dispatcher")
    elif len({auditor, producer, dispatcher}) != 3:
        errs.append("plan_audit 三权未分离: producer=%s auditor=%s dispatcher=%s"
                    % (producer, auditor, dispatcher))
    checks = pa.get("checks") or []
    keys = set(checks.keys()) if isinstance(checks, dict) else set(checks)
    missing = ["A%d" % i for i in range(1, 11) if "A%d" % i not in keys]
    if missing:
        errs.append("plan_audit.checks 未覆盖: %s" % missing)
    return errs


def _plan_handoffs(data):
    """handoff.json 可为单对象 / 对象含 handoffs[] / 直接数组。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("handoffs"), list):
            return data["handoffs"]
        return [data]
    return None


def _plan_dag(plan):
    """返回 (by_id, hard_deps, errs)。只按 depends_on / hard_dependencies 连边；
    soft_dependencies 显式列出、或目标是 soft_dependency 任务，都不阻塞主链。"""
    by_id, errs = {}, []
    for t in (plan.get("tasks") or []):
        tid = t.get("id")
        if not tid or tid in by_id:
            errs.append("任务 id 缺失或重复: %r" % tid)
            continue
        by_id[tid] = t
    soft_tasks = {tid for tid, t in by_id.items() if t.get("soft_dependency")}
    hard_deps = {}
    for tid, t in by_id.items():
        declared_soft = set(t.get("soft_dependencies") or [])
        deps = []
        for key in ("depends_on", "hard_dependencies"):
            for d in (t.get(key) or []):
                if d in declared_soft or d in soft_tasks:
                    continue  # 软依赖不进主链连边
                if d not in deps:
                    deps.append(d)
        hard_deps[tid] = deps
        for d in deps:
            if d not in by_id:
                errs.append("悬空依赖 %s -> %s" % (tid, d))
    return by_id, hard_deps, errs


def _plan_waves(by_id, hard_deps):
    """Kahn 分层：同 wave 内互不依赖，可并行。环 → 剩余无法入 wave → REFUSE。"""
    done, remaining, waves = set(), set(by_id), []
    while remaining:
        wave = sorted(tid for tid in remaining
                      if all(d in done for d in hard_deps.get(tid, [])))
        if not wave:
            return waves, ["依赖成环或悬空，剩余无法入 wave: %s"
                           % sorted(remaining)]
        waves.append(wave)
        done |= set(wave)
        remaining -= set(wave)
    return waves, []


def _plan_run_one(a, st, t):
    """把 plan 里的一个任务翻译成现有单任务 Namespace，再交给 drive()。"""
    na = argparse.Namespace(**vars(a))
    na.task = t.get("id")
    na.title = t.get("title") or t.get("id")
    na.scope = t.get("write_scope") or []
    na.cmd = (t.get("acceptance") or {}).get("cmd") or 'python -c "print(1)"'
    na.base = t.get("base") or None
    na.dry = False
    na.resume = False
    na.job_path = None
    return drive(na, st)


def plan_drive(a, st) -> int:
    """多任务 DAG 驱动：读 plan/plan_audit/handoff → 审计 → 分 wave → 调度。

    --plan 走这里；单任务仍走 drive()。--plan-dry / --dry 只打印 wave/任务/
    依赖/写域/验收要点，不写链、不创建真实任务。
    """
    import team_protocol

    base_dir = os.path.dirname(os.path.abspath(a.plan))
    audit_path = a.plan_audit or os.path.join(base_dir, "plan_audit.json")
    handoff_path = a.plan_handoff or os.path.join(base_dir, "handoff.json")
    dry = bool(getattr(a, "plan_dry", False) or getattr(a, "dry", False))

    plan, err = _plan_read(a.plan, "plan.json")
    if err:
        print("PLAN_DRIVE REFUSE: %s" % err)
        return EXIT_FAIL
    pa, err = _plan_read(audit_path, "plan_audit.json")
    if err:
        print("PLAN_DRIVE REFUSE: %s" % err)
        return EXIT_FAIL
    hand, err = _plan_read(handoff_path, "handoff.json")
    if err:
        print("PLAN_DRIVE REFUSE: %s" % err)
        return EXIT_FAIL
    enums, err = _plan_read(os.path.join(WB, "spec", "enums.json"),
                            "spec/enums.json")
    if err:
        print("PLAN_DRIVE REFUSE: %s" % err)
        return EXIT_FAIL
    contract = (enums or {}).get("team_protocol")
    if not isinstance(contract, dict):
        print("PLAN_DRIVE REFUSE: enums.json 缺 team_protocol")
        return EXIT_FAIL

    # 2) 方案审计 A1–A10 —— 真读者进 DAG 驱动
    findings = team_protocol.audit_plan(plan, contract)
    if findings:
        print("PLAN_DRIVE REFUSE: team_protocol.audit_plan findings:")
        for f in findings:
            print("  - %s" % f)
        return EXIT_FAIL
    print("ran: team_protocol.audit_plan(plan, contract) -> PASS (0 findings)")

    # 3) 独立审计产物：PASS / 三权分离 / A1..A10 覆盖
    audit_errs = _plan_check_audit(pa)
    if audit_errs:
        print("PLAN_DRIVE REFUSE: plan_audit.json 不合规:")
        for e in audit_errs:
            print("  - %s" % e)
        return EXIT_FAIL
    print("ran: plan_audit.json PASS / 三权分离 / A1..A10 covered")

    # 4) 每个接缝：evidence 必须指向真实非空文件
    handoffs = _plan_handoffs(hand)
    if handoffs is None:
        print("PLAN_DRIVE REFUSE: handoff.json 结构非法")
        return EXIT_FAIL
    for i, h in enumerate(handoffs):
        hf = team_protocol.validate_handoff(h, contract, wb=WB)
        if hf:
            print("PLAN_DRIVE REFUSE: handoff[%d] 不合规:" % i)
            for f in hf:
                print("  - %s" % f)
            return EXIT_FAIL
    print("ran: team_protocol.validate_handoff(...) -> PASS (%d handoff)"
          % len(handoffs))

    # 5) DAG：只按硬依赖连边，软依赖不阻塞主链；环/悬空在此收敛
    by_id, hard_deps, dag_errs = _plan_dag(plan)
    if dag_errs:
        print("PLAN_DRIVE REFUSE: DAG 依赖非法:")
        for e in dag_errs:
            print("  - %s" % e)
        return EXIT_FAIL
    waves, wave_errs = _plan_waves(by_id, hard_deps)
    if wave_errs:
        print("PLAN_DRIVE REFUSE: %s" % "; ".join(wave_errs))
        return EXIT_FAIL

    # 6/7) 逐 wave 打印 ran: 行；同 wave 用 ThreadPoolExecutor 并行
    cap = _plan_batch_max_parallel()
    print("\nDAG: %d waves / %d tasks (soft 不阻塞主链, batch_max_parallel=%d)"
          % (len(waves), len(by_id), cap))
    for wi, wave in enumerate(waves, 1):
        n_workers = min(cap, len(wave))
        print("\n── wave %d/%d  tasks=%d  max_workers=%d"
              % (wi, len(waves), len(wave), n_workers))
        for tid in wave:
            t = by_id[tid]
            acc = t.get("acceptance") or {}
            print("  ran: %s  depends_on=%s  soft=%s  write_scope=%s  "
                  "acceptance=%r  evidence=%r"
                  % (tid, hard_deps.get(tid, []),
                     sorted(set(t.get("soft_dependencies") or [])),
                     t.get("write_scope") or [], acc.get("cmd"),
                     acc.get("evidence")))
        if dry:
            continue
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = {ex.submit(_plan_run_one, a, st, by_id[tid]): tid
                    for tid in wave}
            for fut in as_completed(futs):
                rc = fut.result()
                if rc not in (EXIT_OK, EXIT_WAIT_EXECUTOR, EXIT_WAIT_SIGN):
                    print("PLAN_DRIVE REFUSE: %s 单任务 drive 返回 %s"
                          % (futs[fut], rc))
                    return EXIT_FAIL

    print("\nPLAN_DRIVE PASS  waves=%d tasks=%d%s"
          % (len(waves), len(by_id),
             "  [dry: 未写链/未建任务]" if dry else ""))
    return EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="一条命令：建任务 → 审批 → 派发 → 执行 → 验收 → 闸门 → 合并")
    ap.add_argument("--task", default=None, help="任务号；新任务可省略（自动取 T-XXXX）")
    ap.add_argument("--title", default=None, help="任务标题（建任务用）")
    ap.add_argument("--scope", action="append", help="写域前缀，可重复，如 src/stats.py")
    ap.add_argument("--priority", default="P2")
    ap.add_argument("--cmd", default=None, help="验收命令（写进 acceptance.draft.yaml A1）")
    ap.add_argument("--role", default="coder",
                    choices=["coder", "architect", "lead"],
                    help="执行角色；第四十轮起 lead 可真派活（写 E05 / 取租约 / 进 in_progress）")
    ap.add_argument("--objective", default="", help="写进 brief 的目标描述")
    ap.add_argument("--executor", default="harness_subagent",
                    choices=["harness_subagent", "local_process", "human_cli"])
    ap.add_argument("--entrypoint", default=None, help="local_process 的执行体脚本")
    ap.add_argument("--base", default=None, help="任务分支的基点 commit")
    ap.add_argument("--wait", type=int, default=1800)
    ap.add_argument("--sign-as", dest="sign_as", default=None,
                    help="缺签名时以此 principal 自动签（需要 WORKBENCH_ROOT_KEY）")
    ap.add_argument("--resume", action="store_true", help="从链上当前状态续跑")
    ap.add_argument("--dry", action="store_true", help="只打印计划（单任务）；plan 模式亦作 dry")
    ap.add_argument("--no-report", dest="no_report", action="store_true",
                    help="跳过末尾的对外汇报渲染（只留搭建者日志）")
    ap.add_argument("--verbose", action="store_true",
                    help="打过程行（命令与子进程回显）；默认只打结论与等你动手的提示")
    ap.add_argument("--plan", default=None,
                    help="多任务 plan.json 路径；给了就走 plan_drive() DAG 驱动")
    ap.add_argument("--plan-audit", dest="plan_audit", default=None,
                    help="plan_audit.json 路径（默认取 --plan 同目录）")
    ap.add_argument("--plan-handoff", dest="plan_handoff", default=None,
                    help="handoff.json 路径（默认取 --plan 同目录）")
    ap.add_argument("--plan-dry", dest="plan_dry", action="store_true",
                    help="plan 模式只打印 wave/任务，不写链、不建真实任务")
    a = ap.parse_args(argv)
    VERBOSE[0] = bool(a.verbose or a.dry)

    if a.plan:
        st = store()
        return plan_drive(a, st)

    st = store()

    if not a.task:
        if a.resume:
            a.task = latest_task_id(st)
            if not a.task:
                warn("REFUSE：链上没有 draft_created，无从续跑")
                return EXIT_FAIL
        else:
            a.task = next_task_id(st)

    if not a.cmd and a.title:
        a.cmd = 'python -c "print(1)"'
    a.job_path = None

    log("任务 %s  当前状态=%r  executor=%s"
          % (a.task, state_of(st, a.task), a.executor))
    rc = drive(a, st)
    if not getattr(a, "no_report", False):
        _boss_report(a)
    return rc


if __name__ == "__main__":
    sys.exit(main())
