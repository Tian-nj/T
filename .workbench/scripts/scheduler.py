#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scheduler.py —— 让定时器脚本真正跑起来的那个循环。

## 为什么需要这个文件

`health.py`、`gate_timer.py`、`metrics_collector.py`、`gc_helper.py` 四个脚本
的逻辑都是对的，测试也都过了，`README.md` §5 的「触发者」一栏还郑重地写着
`timer`。但全仓唯一的引用是 `agent_runner.py:66 import health`（而且只用了
`health.write_hb`）。于是：

  * `gate_timer` 管三条闸门的 240 分钟 SLA —— 它不跑，闸门**永不超时**；
  * `health` 管 600 秒看门狗 —— 它不跑，死掉的 worker **永不被判死**；
  * `metrics_collector` 每 5 分钟采一次指标 —— 它不跑，`metrics/<月>.jsonl` 不增长；
  * `gc_helper` 回收 worktree 与归档 —— 它不跑，`.workbench/wt` 只增不减。

四条规则都写在代码里、跑在文档里、从没跑在机器上。这与 D-48（per-event
`actors` 声明存在但从不执行）是同一种病：**声明悬空**。

第五、第六个 job 是后来补的：`dream`（每日梦境整合）与 `observe`（第三十一轮
候选 3，长期自运转观察）。`observe` 的存在理由和上面四条同源 —— 观察器写好
之后如果只在人手工敲 `record` 时才追加趋势点，那么"7 天自运转窗口"这件事
就永远停在 0.3 小时，等于用一条人工命令假装机器在看着自己。

## 三条设计决定

1. **调度器不写链。** 它没有 `task`，不属于任何一个任务的语义场；把
   「今天 14:32 的 health 返回 1」写进 45 个事件的链里，等于给审计日志
   加一种没有归属的噪声。失败落 `state/scheduler.json` 的 sidecar，
   由 `--status` 的退出码和 `lead.py check` 的 SCHED 行对外暴露。
   *判据*：可重算的瞬时状态不落链（与 merge_queue 的 `gate_wait` 同一理由）。

2. **每个 job 的到期判定用「上次成功」而不是「上次尝试」。** 一个一直失败
   的 job 如果按「上次尝试」算，会每 60 秒准时重试、永远新鲜、永远坏着。
   按「上次成功」算，它会在 `interval_sec * stale_factor` 之后变成 `overdue`，
   于是坏掉这件事本身有一个可观测的时间点。

3. **调度器自己的存活不能由调度器证明。** `--status` 只能回答「上一次 tick
   是什么时候」，回答不了「现在还有没有人在 tick」——如果没人跑了，也就没人
   来读这个文件。所以真正的存活检测必须在调度器之外：`sched_install.py install`
   注册 Windows 计划任务（`DSH-Workbench-Scheduler`，每 1 分钟拉起一次短进程
   `--once`），`sched_install.py status` 同时查「任务是否已注册」与「sidecar
   是否新鲜」两件事。这一条写进 README §17，不假装它是自证的。

用法：
    python scheduler.py --once              # 全部 job 各跑一次（验证用）
    python scheduler.py --forever           # 常驻循环
    python scheduler.py --status            # 新鲜度自检，stale 时退出码 1
    python scheduler.py --status --json     # 同上，机器可读
    python scheduler.py --job health --once  # 只跑一个
"""

import io
import json
import os
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
STATE = os.path.join(WB, "state")
SCHED_FILE = os.path.join(STATE, "scheduler.json")

sys.path.insert(0, HERE)
import wb_core  # noqa: E402  （与其余脚本同一入口约定）

# job 名 → (模块名, argv, 说明)
# argv 里不能带 --json：调度器自己要把输出收进 sidecar，job 再吐 JSON 只会打架。
JOBS = {
    "health": ("health", ["--emit"],
               "看门狗：扫心跳，超窗报 E06 heartbeat_lost，恢复报 E43"),
    "gate_timer": ("gate_timer", [],
                   "闸门 SLA：超 240 分钟报 E25 human_timeout，必要时迁 blocked"),
    "metrics": ("metrics_collector", ["--guarded"],
                "指标采集：按 collector_sec 自我节流，落 metrics/<月>.jsonl"),
    "gc": ("gc_helper", [],
           "回收：worktree / 归档 / 快照，按 ledger 幂等"),
    # dream（W4）：梦境整合。它是唯一「按天」而不是「按间隔」跑的 job，
    # 所以 due 判定不看 interval_sec 而看 run_time（见 due）。
    # 第四十八轮把 argv 从 ["run"] 改成 ["run", "--backlog"]：这个 job 每天只有
    # 一次机会，某天素材不足或机器停过，那一天就永久空着（`state/dream/` 里
    # 永远写着 skipped）。补账让它每次点一下就把欠的几天一起补掉。
    "dream": ("dream_consolidation", ["run", "--backlog"],
              "梦境整合：补整理最近几天 → 写日记 → 沉淀记忆 → 落成长候选"),
    # 第三十一轮候选 3：把「长期自运转观察」从人工 record 变成调度器自己的活。
    # 之前 trend 只有 3~5 个点，全靠人手工敲 record；7 天窗口因此永远是 0.3h。
    # 注意 `--allow-missing`：一次全新安装的第一次 tick 里本 job 先于
    # save_state() 跑，sidecar 还不存在——那是「还没有可观测的东西」，
    # 不是「观测通道坏了」，不该让调度器因为自己的第一轮而红。
    "observe": ("scheduler_observe", ["record", "--allow-missing", "--quiet"],
                "长期观察：每轮追加一个趋势点，7 天窗口靠它自己长出来"),
    # 第五十一轮：记忆的降档与衰减。
    # `memory_store.decay()` 从第六轮起就在，有 selftest、有 CLI 子命令，
    # 但**没有任何调度入口** —— forget_after_days / fuzzy_after_days /
    # 一天最多降一档 / 超期归档，四条规则全部永不执行，记忆只增不减。
    # 这不是「还没做」，是「做了但没人调」，与前一轮修的「有函数没人传参」
    # 是同一个形状：不报错、不告警，只是让每条记忆永远停在「刚记住」。
    # 不给 --dry-run：dry 只打印不落盘，挂进调度等于每轮白跑。
    "decay": ("memory_store", ["decay", "--emit-sidecar"],
              "记忆衰减：按 idle_days 降档，超 forget_after_days 且 importance<50 归档"),
    # 第五十二轮：给 working 层一个出口。
    # 上一轮给了 working 写入者，却没给出口 —— 于是任务做完之后那条记忆仍然
    # 挂在「最近在做什么」里，这一层会无限增长，"最近"这个词随时间失去意义。
    # 只进不出的队列不叫队列。结算判据只看链上状态机（done/cancelled/archived），
    # 不看"多久没动"：按时间推断"大概做完了吧"会在长任务上把活着的记忆搬走。
    "settle": ("memory_store", ["settle", "--emit-sidecar"],
               "记忆结算：已完成任务的 working 条目搬进 task 层（只搬层，不动分）"),
}

DEFAULT_TICK_SEC = 30
DEFAULT_STALE_FACTOR = 4
# job 用这个退出码表示「本轮延后，下轮再来」。
# 它不是成功（没做完），也不是失败（不是这条路走不通）。
# 调度器对它的处理必须与两者都不同，见 tick()。
DEFER_RC = 3
# job 用这个退出码表示「跑超时了，已被本轮掐断」。
# 124 与 POSIX timeout(1) 同码 —— 运维看到 124 就知道是被上限掐掉的，
# 不是 job 自己选的退出码，也不是它崩了。
TIMEOUT_RC = 124
# 单个 job 的默认墙钟上限（秒）；配置 scheduler.jobs.<name>.timeout_sec 可覆盖。
# 历史实测（sidecar ticks=7329）：8 个 job 最慢 metrics 223ms，其余全在 100ms 内，
# 所以这个数是纯兜底 —— 正常路径永远碰不到，碰到就是真的卡住了。
DEFAULT_JOB_TIMEOUT_SEC = 300


# ---------------------------------------------------------------- 配置

def cfg(wb=None):
    """读配置。第五十五轮：允许观察器指定 WB，避免每个模块自己解析一份。"""
    return wb_core.yaml_parse_file(os.path.join(wb or WB, "config.yaml"))


# 缺省 job 周期进调度器，不散落在观察器/记忆台账里。
DEFAULT_JOB_INTERVAL_SEC = {
    "health": 60,
    "gate_timer": 60,
    "metrics": 300,
    "gc": 3600,
    "dream": 86400,
    "observe": 300,
    "decay": 3600,
    "settle": 1800,
}
LEDGER_JOB = {"memory_decay": "decay", "memory_settle": "settle",
              "memory_recall": None}


def job_interval_sec(name, sc=None):
    """job 的 interval_sec 唯一读取点（第五十六轮）。

    之前 due/overdue/tick/status 各写一份 ``or 60``，台账预算走
    ``DEFAULT_JOB_INTERVAL_SEC`` —— 同一个 job 节点被删时，一个说 60s、
    一个说 3600s，两把尺子量同一件事。这里收回一处：配置写了就用写的，
    sc 没这个 job 就回落 DEFAULT_JOB_INTERVAL_SEC，最后才兜底 60。
    """
    if sc is not None:
        node = (sc.get("jobs") or {}).get(name) or {}
        v = node.get("interval_sec")
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    return int(DEFAULT_JOB_INTERVAL_SEC.get(name, 60))


def job_timeout_sec(name, sc=None):
    """job 的墙钟上限唯一读取点。

    与 job_interval_sec 分开两个函数，是因为它们回答的是两个不同的问题：
    interval 是「隔多久跑一次」，cap 是「跑多久算卡死」。合成一个函数的话，
    把 interval 从 60 调到 3600 会顺手把超时上限也放大 60 倍 —— 而调大 interval
    的动机通常是「它太吵」，跟「它可能卡住」毫无关系。
    """
    if sc is not None:
        node = (sc.get("jobs") or {}).get(name) or {}
        v = node.get("timeout_sec")
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    return int(DEFAULT_JOB_TIMEOUT_SEC)


def sched_cfg(wb=None):
    """读 scheduler 段；缺失时给一套保守默认值并留痕。

    这里刻意不抛异常：一个配置读不出来的调度器，最好的行为是按默认值继续
    跑并在 sidecar 里记一笔，而不是把整个自转停掉。停掉是**不可观测**的坏，
    按默认值跑是**可观测**的坏。
    """
    try:
        # 第五十五轮：wb is None 时必须调无参 cfg() —— 测试用
        # ``sch.cfg = lambda: {...}`` 注入的是无参 callable，传 None 进去会让
        # 它抛 TypeError、被下面的 except 吞成「配置全丢」，于是 run_time /
        # enabled / interval 全走默认，四个既有测试同时红。观察器传了路径
        # 才走 cfg(wb)：两种调用方式各自成立，不互相顶掉。
        c = cfg() if wb is None else cfg(wb)
        s = (c.get("scheduler") or {})
    except Exception as exc:            # config 坏了也要能起来
        s = {}
        s["_cfg_error"] = str(exc)
    tick = int(s.get("tick_sec") or DEFAULT_TICK_SEC)
    factor = int(s.get("stale_factor") or DEFAULT_STALE_FACTOR)
    jobs = {}
    for name in JOBS:
        j = (s.get("jobs") or {}).get(name) or {}
        jobs[name] = {"enabled": bool(j.get("enabled", True)),
                      "interval_sec": int(j.get("interval_sec")
                                          or job_interval_sec(name)),
                      "timeout_sec": int(j.get("timeout_sec")
                                         or job_timeout_sec(name))}
        # run_time 只在配置里写了才透传：没写时保持 None，让 due() 走 interval 分支。
        # 不能默认填一个 "04:00" —— 那会让「没配置」变成「每天都跑」。
        if j.get("run_time"):
            jobs[name]["run_time"] = str(j.get("run_time"))
    return {"tick_sec": max(5, tick), "stale_factor": max(2, factor),
            "jobs": jobs, "_error": s.get("_cfg_error")}


def stale_after_ms(sc=None, wb=None):
    """调度器自身的判死窗口；sc 给了就用给的，否则从 WB 的配置读。"""
    sc = sc or sched_cfg(wb)
    return sc["tick_sec"] * 1000 * sc["stale_factor"]


def ledger_stale_after_ms(ch, sc=None, wb=None):
    """记忆台账的停写预算：唯一生成源在 scheduler，不再各模块写 factor。

    周期通道按 job 的 interval_sec × stale_factor；事件驱动通道返回 None，
    由 ledger_summary 用「最近派活」另判，避免把「没派活」判成「台账坏了」。
    """
    sc = sc or sched_cfg(wb)
    job = LEDGER_JOB.get(ch)
    if job is None:
        return None
    sec = job_interval_sec(job, sc)
    return sec * 1000 * int(sc["stale_factor"])


# ---------------------------------------------------------------- sidecar

def load_state():
    try:
        with io.open(SCHED_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {"schema": 1, "ticks": 0, "last_tick_ms": 0, "runs": {}}


def save_state(st):
    os.makedirs(STATE, exist_ok=True)
    tmp = SCHED_FILE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, SCHED_FILE)


# ---------------------------------------------------------------- 到期判定

def _run_time_ms(now_ms, hhmm):
    """把 "HH:MM" 或 "HH:MM,HH:MM,..." 换成"今天这些时刻"的毫秒时间戳列表。

    返回 None 表示 run_time 写坏了 —— 调用方按「不可判定」处理，
    而不是猜一个默认时刻：猜出来的时刻会让人以为"今天已经跑过了"。
    返回 `[]` 表示格式对但没有有效时刻（同样按不可判定处理）。

    为什么支持多个时刻：一天只整理一次，意味着下午发生的事要等到次日 04:00
    才被提炼 —— 而"今天"这个概念在跨过午夜时就变了。允许写
    "04:00,16:00" 之后，下午那次整理仍然只整理**当天**，只是当天被整理了两次。
    幂等键是 `(date, action, content_sha256)`，所以跑两次不会重复写条目。
    """
    out = []
    for part in str(hhmm or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hh, mm = part.split(":")
            hh, mm = int(hh), int(mm)
        except (ValueError, AttributeError):
            return None
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        lt = time.localtime(now_ms / 1000.0)
        base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
        out.append(int(base * 1000))
    if not out:
        return None
    return sorted(set(out))


def due(name, run, now_ms, sc):
    """该 job 现在该不该跑。

    看**上次成功**（`ok_ms`），不看上次尝试（`try_ms`）。理由见文件头第 2 条：
    按尝试算，一个一直失败的 job 会永远"新鲜"。
    """
    j = sc["jobs"].get(name) or {}
    if not j.get("enabled", True):
        return False
    last = int(run.get("ok_ms") or 0)
    if not last:
        return True                      # 从没成功过 → 立刻跑一次
    run_time = j.get("run_time")
    if run_time:
        # 按天跑的 job（dream）：判据是「今天某个 run_time 已过，而上次成功
        # 早于那个时刻」。用 interval 判会让"整理哪一天"取决于唤醒时机 ——
        # 04:00 的 job 若在 03:59 唤醒，它会整理前天；在 04:01 唤醒才整理昨天。
        targets = _run_time_ms(now_ms, run_time)
        if not targets:
            return False
        # 任一已过的时刻晚于上次成功 → 该跑。
        # 注意是 **any** 不是「最后一个」：机器休眠错过 04:00 之后，
        # 若 16:00 也过了，仍然应该跑一次（而不是等到明天）。
        return any(now_ms >= t and last < t for t in targets)
    return (now_ms - last) >= job_interval_sec(name, sc) * 1000


def overdue(name, run, now_ms, sc):
    j = sc["jobs"].get(name) or {}
    last = int(run.get("ok_ms") or 0)
    limit = job_interval_sec(name, sc) * 1000 * sc["stale_factor"]
    if not last:
        return True
    return (now_ms - last) > limit


# ---------------------------------------------------------------- 执行一个 job

def run_job(name, timeout_cap=0):
    """调子模块的 main(argv)，把它的输出收起来。

    收输出的两个理由：① `--json` 模式下调度器要吐干净 JSON；② 成功的 job
    不该在日志里留一堆行 —— 一个每天说 1438 次「我没事」的看门狗，会让人
    在真出事那天也照样滑过去。

    timeout_cap（秒；<=0 表示不设上限）：这个形参曾经是死的 —— 全仓没有一处
    传过它，于是「job 卡住」这件事没有任何上限。真实事故：一次 `--once` 卡了
    8 分钟，而计划任务是 MultipleInstances=IgnoreNew，后续每一分钟的 tick 全被
    系统拒掉（0x800710E0），调度器静默停摆 —— 一个不设上限的循环，卡住就等于
    停摆，而且停得完全没有声音。

    实现用线程 + join，不用子进程：`redirect_stdout` 只捕获本进程的输出，换成
    子进程会让 sidecar 里的 out 变成空，而 out 是排障时唯一能看出「它跑到哪
    一步」的东西。代价是线程杀不掉，超时只能「不再等它」——所以 rc 记
    TIMEOUT_RC 而不是假装它失败了。这是把一个静默的死换成一个响亮的坏。
    """
    modname, argv, _desc = JOBS[name]
    buf = io.StringIO()
    t0 = time.time()
    box = {"rc": 0, "err": ""}

    def _call():
        try:
            mod = __import__(modname)
            with redirect_stdout(buf):
                box["rc"] = int(mod.main(argv) or 0)
        except SystemExit as exc:        # argparse 出错
            box["rc"] = int(getattr(exc, "code", 1) or 1)
            box["err"] = "SystemExit(%s)" % box["rc"]
        except Exception:
            box["rc"] = 1
            box["err"] = traceback.format_exc(limit=4)

    if timeout_cap and timeout_cap > 0:
        real_out = sys.stdout
        th = threading.Thread(target=_call, name="sched-job-%s" % name)
        th.daemon = True
        th.start()
        th.join(timeout_cap)
        if th.is_alive():
            rc = TIMEOUT_RC
            err = ("timeout after %ss（本轮记失败；该 job 的线程仍挂在后台，"
                   "Python 无法强杀线程，它跑完就结束了）" % (timeout_cap,))
            # redirect_stdout 是进程级替换：卡住的线程还留在它的 with 块里，
            # 不抢回来，后续所有 print 都会掉进那个 StringIO 里看不见。
            sys.stdout = real_out
        else:
            rc, err = box["rc"], box["err"]
    else:
        _call()
        rc, err = box["rc"], box["err"]
    dt = int((time.time() - t0) * 1000)
    out = buf.getvalue()
    return {"name": name, "rc": rc, "ms": dt,
            "out": out.strip().splitlines()[-40:], "err": err}


# ---------------------------------------------------------------- tick

def tick(now_ms=None, only=None, force=False):
    """跑一轮：把到期的 job 依次执行，落 sidecar。返回本轮摘要。"""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    sc = sched_cfg()
    st = load_state()
    st.setdefault("runs", {})
    st.setdefault("ticks", 0)
    st["schema"] = 1
    st["last_tick_ms"] = now_ms
    st["ticks"] = int(st["ticks"]) + 1
    st["tick_sec"] = sc["tick_sec"]
    st["stale_factor"] = sc["stale_factor"]
    if sc.get("_error"):
        st["cfg_error"] = sc["_error"]

    picked, skipped = [], []
    for name in JOBS:
        if only and name != only:
            continue
        r = st["runs"].get(name) or {}
        if force or due(name, r, now_ms, sc):
            picked.append(name)
        else:
            skipped.append(name)

    results = []
    for name in picked:
        res = run_job(name, job_timeout_sec(name, sc))
        r = st["runs"].get(name) or {}
        r["try_ms"] = now_ms
        r["try_count"] = int(r.get("try_count") or 0) + 1
        r["rc"] = res["rc"]
        r["ms"] = res["ms"]
        if res["rc"] == 0:
            r["ok_ms"] = now_ms
            r["fail_streak"] = 0
            r.pop("last_error", None)
            # 成功要把延后痕迹清干净：留着 defer_streak 会让人以为它还在卡。
            for k in ("defer_streak", "defer_since_ms", "last_deferred"):
                r.pop(k, None)
        elif res["rc"] == DEFER_RC:
            # 「延后」：job 说「这次别人占着，下轮再来」。
            #   * 不推 ok_ms   → due() 下一轮还会点它，这就是我们要的重试
            #   * 不涨 fail_streak → 别让别人的临时句柄污染失败曲线
            #   * 但不能无限延后：超过一个完整 interval 还在延，说明这已经不是
            #     「一时被占」，此时升级成一次真失败，否则「一直延后」会永远不进 bad。
            r["defer_streak"] = int(r.get("defer_streak") or 0) + 1
            r.setdefault("defer_since_ms", now_ms)
            r["last_deferred"] = (res["out"] or [""])[-1][:300] if res["out"] else ""
            _lim = job_interval_sec(name, sc) * 1000
            if now_ms - int(r["defer_since_ms"]) > _lim:
                r["fail_streak"] = int(r.get("fail_streak") or 0) + 1
                r["last_error"] = ("deferred >1 interval since %d: %s"
                                   % (int(r["defer_since_ms"]),
                                      r.get("last_deferred") or ""))[-800:]
        else:
            r["fail_streak"] = int(r.get("fail_streak") or 0) + 1
            # 只留最后 6 行：sidecar 是给人看的，堆栈留全部会把文件撑成日志。
            r["last_error"] = (res["err"] or "\n".join(res["out"]))[-800:]
        st["runs"][name] = r
        results.append(res)

    save_state(st)
    return {"now_ms": now_ms, "tick_sec": sc["tick_sec"],
            "ran": [r["name"] for r in results],
            "skipped": skipped, "results": results,
            "deferred": [r["name"] for r in results if r["rc"] == DEFER_RC],
            "failed": [r["name"] for r in results
                       if r["rc"] != 0 and r["rc"] != DEFER_RC]}


# ---------------------------------------------------------------- 状态

def status(now_ms=None):
    """新鲜度：调度器本身 + 每个 job。"""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    sc = sched_cfg()
    st = load_state()
    lim = stale_after_ms(sc)
    last = int(st.get("last_tick_ms") or 0)
    age = (now_ms - last) if last else None
    jobs = []
    for name in JOBS:
        r = st.get("runs", {}).get(name) or {}
        jobs.append({
            "name": name,
            "enabled": bool((sc["jobs"].get(name) or {}).get("enabled", True)),
            "ok_ms": int(r.get("ok_ms") or 0),
            "age_ms": (now_ms - int(r["ok_ms"])) if r.get("ok_ms") else None,
            "interval_sec": job_interval_sec(name, sc),
            # 上限也要在 status 里可见：看不到它，就看不出「这个 job 已经被
            # 掐过一次」——rc 只记最后一次，而最后一次可能已经恢复了。
            "timeout_sec": job_timeout_sec(name, sc),
            "fail_streak": int(r.get("fail_streak") or 0),
            # 延后是第三种状态，必须单独可见：否则「一直在等别人让开」
            # 会在 status 里长得跟「刚刚成功」一模一样。
            "defer_streak": int(r.get("defer_streak") or 0),
            "last_deferred": r.get("last_deferred"),
            "rc": r.get("rc"),
            "overdue": overdue(name, r, now_ms, sc),
            "last_error": r.get("last_error"),
        })
    fresh = age is not None and age <= lim
    bad = [j["name"] for j in jobs if j["enabled"] and (j["overdue"] or j["fail_streak"])]
    return {"now_ms": now_ms, "last_tick_ms": last, "age_ms": age,
            "stale_after_ms": lim, "fresh": fresh,
            "ticks": int(st.get("ticks") or 0), "jobs": jobs,
            "bad": bad, "cfg_error": sc.get("_error"),
            "ok": bool(fresh and not bad)}


# ---------------------------------------------------------------- CLI

def _print_once(r):
    if r["failed"]:
        for res in r["results"]:
            if res["rc"] != 0:
                print("  !! %-11s rc=%d %dms%s"
                      % (res["name"], res["rc"], res["ms"],
                         "  TIMEOUT" if res["rc"] == TIMEOUT_RC else ""))
                if res["err"]:
                    for line in res["err"].strip().splitlines()[-4:]:
                        print("     %s" % line)
                for line in res["out"][-4:]:
                    print("     %s" % line)
    print("tick: ran %d job(s)%s%s%s"
          % (len(r["ran"]), " [%s]" % " ".join(r["ran"]) if r["ran"] else "",
             ", %d deferred" % len(r.get("deferred") or []) if r.get("deferred") else "",
             ", %d failed" % len(r["failed"]) if r["failed"] else ""))
    for name in (r.get("deferred") or []):
        print("  .. %-11s rc=%d (deferred, retry next tick)" % (name, DEFER_RC))
    if r["skipped"]:
        print("  not due: %s" % " ".join(sorted(r["skipped"])))


def _print_status(s):
    print("scheduler: ticks=%d last_tick=%s age=%s stale_after=%dms %s"
          % (s["ticks"],
             ("%dms ago" % s["age_ms"]) if s["age_ms"] is not None else "never",
             s["age_ms"], s["stale_after_ms"],
             "FRESH" if s["fresh"] else "STALE"))
    for j in s["jobs"]:
        print("  %-11s %-7s ok_age=%-10s interval=%4ds cap=%4ds rc=%s "
              "streak=%d%s"
              % (j["name"], "enabled" if j["enabled"] else "off",
                 ("%dms" % j["age_ms"]) if j["age_ms"] is not None else "never",
                 j["interval_sec"], j["timeout_sec"], j["rc"], j["fail_streak"],
                 "  OVERDUE" if j["overdue"] else ""))
    if s["bad"]:
        print("  bad: %s" % " ".join(s["bad"]))
    if s["cfg_error"]:
        print("  cfg_error: %s" % s["cfg_error"])
    print("RESULT: %s" % ("OK — 调度器新鲜且无 job 逾期" if s["ok"]
                          else "STALE — 调度器或某个 job 已逾期"))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="让定时器脚本真正转起来的循环")
    ap.add_argument("--once", action="store_true", help="只跑一轮（到期判定照旧）")
    ap.add_argument("--force", action="store_true", help="配 --once：忽略间隔，全部跑一遍")
    ap.add_argument("--forever", action="store_true", help="常驻循环")
    ap.add_argument("--max-ticks", type=int, default=0, help="循环最多跑几轮（0=不限）")
    ap.add_argument("--job", default=None, choices=sorted(JOBS), help="只跑某一个 job")
    ap.add_argument("--status", action="store_true", help="新鲜度自检；stale 时退出码 1")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="循环模式不打印成功轮次")
    a = ap.parse_args(argv)

    if a.status:
        s = status()
        if a.json:
            print(json.dumps(s, ensure_ascii=False, indent=2))
        else:
            _print_status(s)
        return 0 if s["ok"] else 1

    if a.once or not a.forever:
        r = tick(only=a.job, force=a.force)
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            _print_once(r)
        return 0 if not r["failed"] else 1

    # 常驻：每个 tick 重读配置（改 interval 不必重启），Ctrl-C 退出。
    sc = sched_cfg()
    print("scheduler: tick=%ds jobs=%s" % (sc["tick_sec"], ",".join(sorted(JOBS))))
    n = 0
    try:
        while True:
            r = tick(only=a.job)
            n += 1
            if not a.quiet or r["failed"]:
                stamp = time.strftime("%H:%M:%S", time.localtime())
                print("[%s] tick %d: ran=%d failed=%d"
                      % (stamp, n, len(r["ran"]), len(r["failed"])))
                if r["failed"]:
                    _print_once(r)
            if a.max_ticks and n >= a.max_ticks:
                return 0 if True else 1
            sc = sched_cfg()
            time.sleep(max(1, sc["tick_sec"]))
    except KeyboardInterrupt:
        print("\nscheduler: stopped after %d tick(s)" % n)
        return 0


if __name__ == "__main__":
    sys.exit(main())
