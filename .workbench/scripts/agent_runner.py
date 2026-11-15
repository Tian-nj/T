#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""agent_runner.py —— 角色执行槽（role execution slot）

问题背景
--------
`agents.json` 声明了四个角色、`executor` 三种、`slots` 若干，但在此之前
**全仓没有任何代码会把它们变成真实动作**：`dispatch` 只排序并打印，
`claim` 只写事件并把 Lead 自己的 pid 当成 worker，worktree 由 `run.sh`
手工建。名字合法 ≠ 有人执行 —— 这正是本仓反复撞到的那个形状。

这个脚本补的是「控制面到执行体」之间的那一段，职责严格划分：

  控制面负责（不可绕过，脚本强制）
    1. 建/复用 worktree  ← 从 run.sh 收回，它本属控制面
    2. 申请租约 + 定周期心跳（心跳落 state/hb/，不进主链）
    3. 生成 brief.md（角色卡 + 任务 + 写域 + 交付契约）
    4. 派发（三种 executor）
    5. 收尾：写域复核 → result.json → 释放租约 → 交回 lead.py 提交

  执行体负责（真正的思考）
    按 brief 在 worktree 里改文件。它可以是
      · local_process    —— 一条命令（确定性脚本 / 本地模型 CLI）
      · harness_subagent —— 由编排器（DSH / 其他）派出的子代理
      · human_cli        —— 人
      · external_ai      —— 本机安装的第三方 AI 产品（Kimi / DeepSeek …）。
                            由 executors/ext_ai_subagent.py 读 job 写 sentinel，
                            用它自己的凭据与端点干活。哪家有接口、哪家没有，
                            写在那份执行体的 PROVIDERS 能力表里，可核对。
    三种的**控制面行为完全相同**：同一个写域、同一份 brief、同一套收尾。
    区分只在派遣方式，不在待遇。

为什么 harness_subagent 走「job 文件 + 等待」
--------------------------------------------
因为反向的做法（这个脚本内嵌一个 LLM 客户端）会把模型凭据、重试、
流式协议全塞进控制面，而控制面一旦依赖模型供应商，可信根就换主人了。
job 文件是单向的：控制面写下「谁、要什么、写到哪、何时截止」，
执行体完成后写一个 sentinel。**控制面不需要知道对面是谁。**

sentinel 协议（执行体必须产出）
    state/agent_done/<task>.<role>.json
    {"ok": true, "summary": "...", "changed": ["src/calc.py"],
     "selfcheck": {"exit": 0, "cmd": "python tests/test_calc.py"}}

为什么要有 sentinel 而不是直接看文件变了没有
--------------------------------------------
「文件变了」无法区分「做完了」与「改到一半崩了」。sentinel 是执行体的
**签署**：它声称自己完成并给出自检。控制面随后独立复核写域与验收，
所以签署撒谎会被抓，却不会因为执行体沉默而被误当成成功。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)

sys.path.insert(0, HERE)
import wb_core  # noqa: E402
import platform_ops  # noqa: E402
import diff_sandbox_check  # noqa: E402
import health  # noqa: E402
import sign_manifest  # noqa: E402
# 神魂注入。失败必须不致命：persona_loader 读的是 persona/*.md 与记忆库，
# 那两处缺文件是常态（新仓库、演示副本、CI）。它抛异常就让整个 brief 写不出来，
# 等于「没写人格」升级成「派不了活」—— 本末倒置。
try:
    import persona_loader  # noqa: E402
except Exception:  # pragma: no cover
    persona_loader = None

TASKS_DIR = os.path.join(WB, "tasks")

# 第四十三轮：过程行降级（与 run_task.py / lead.py / merge_queue.py 同一套开关）。
# 机器读契约与结果行保持裸 print：finish() 的 OK/REFUSE/WARN/TIMEOUT/ABORT/NOTE、
# status/roles 的表头（外部消费方与测试读它们）；cmd_prepare 的四行
# （worktree/brief/job/sentinel）是给外部执行体看的操作指引，收掉它们等于把
# prepare 变成静默成功 —— 保留。
VERBOSE = [False]   # 单元素列表：cmd_run/cmd_finish/cmd_announce 内的 main() 要改它


def log(*parts, **kw):
    """搭建者过程行（spawn/state 回显）。只在 --verbose 下打。"""
    if VERBOSE[0]:
        print(*parts, **kw)
JOB_DIR = os.path.join(WB, "state", "agent_jobs")
DONE_DIR = os.path.join(WB, "state", "agent_done")
AGENTS = os.path.join(WB, "roles", "agents.json")
ROLES_DIR = os.path.join(WB, "roles")
WT_DIR = os.path.join(WB, "wt")

# 第四十轮：真正「占工作区干活」的角色 = 要写 E05 spawn + 取独占租约的角色。
# 以前是 ("coder", "architect") 字面量散在 4 处（cmd_run 的 exclusive /
# wants_spawn、cmd_finish 的补 E05 / 释放租约）；lead 只当身份位，不在这个
# 集合里 —— 结果 lead 派活时状态停在 claimed，task-submit 要 in_progress，
# 整条「主理人自己动手」链路断掉，而没有任何单测拿 lead 跑过，谁也不会红。
# 现在 lead 真的起本地进程，就真写 E05、真取租约；verifier 仍刻意排除
# （它与被验任务并行，写 spawn 会让 H5 把它当成待 kill 的 worker）。
# 一份常量四个读者，改一处不再漏三处。
SPAWN_ROLES = ("coder", "architect", "lead")
LEASE_ROLES = SPAWN_ROLES

# 执行体类型。第四种 external_ai 是「本机装着的第三方 AI 产品当 worker」：
# 它和 harness_subagent / human_cli 一样只写 job 然后等 sentinel，
# 派发分支不需要为它增加一段代码 —— 控制面照旧不知道对面是谁。
# 一份常量两处 argparse choices，改一处不再漏一处（spec_lint S013 也认这张表）。
EXECUTOR_KINDS = ("harness_subagent", "local_process", "human_cli", "external_ai")

# 定稿 §2.3.1（智能工作台-完整方案-v3.2（实现定稿）.md:128）要求 worker 环境
# 剔除 WORKBENCH_ROOT_KEY。规则与实现只在 platform_ops 一份（scrub_env /
# is_secret_env_name），这里刻意**不再抄一遍常量**：两份会各自漂移，而
# 「哪份说了算」在审链时没人答得上来。


class RunnerError(SystemExit):
    pass


# --------------------------------------------------------------------- 基础
def cfg():
    return wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))


def store():
    wb_core.load_frozen(WB)
    return wb_core.Store(WB, sign_manifest.read_uuid_from_config())


def load_agents():
    reg = (cfg().get("agents") or {}).get("registry")
    p = os.path.join(ROOT, reg) if reg else AGENTS
    if reg and not os.path.exists(p):
        raise RunnerError("config.agents.registry=%r 指向的文件不存在: %s" % (reg, p))
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def role_spec(role: str) -> dict:
    agents = load_agents()
    r = (agents.get("roles") or {}).get(role)
    if not r:
        raise RunnerError("agents.json 里没有角色 %r（有：%s）"
                          % (role, ", ".join(sorted((agents.get("roles") or {}).keys()))))
    return r


def role_card(role: str) -> str:
    """角色卡正文。缺文件不算致命 —— 但要说出来，不静默降级。"""
    p = os.path.join(ROLES_DIR, role + ".md")
    if not os.path.exists(p):
        sys.stderr.write("WARN roles/%s.md 不存在；brief 里只有 agents.json 的字段\n" % role)
        return ""
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _write_json_atomic(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        sys.stderr.write("WARN unreadable %s (%s)\n" % (path, exc))
        return {}


def _git(args, cwd=None):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace")


# --------------------------------------------------------------------- 1 工作区
def ensure_worktree(tid: str, base: str | None = None) -> tuple:
    """建/复用任务 worktree，返回 (path, base_commit, created)。

    这段逻辑原来写在 run.sh 里。它属于控制面而不是演示脚本：
    「任务在哪里干活」是流水线的前提，不是演示的细节。
    留在 run.sh 里的后果是——任何别的入口（CI、真实调度器）都得自己再写一遍，
    而写错一次（比如用仓库根当 worktree）就会把控制面自己的改动算成任务越界。
    """
    wt = os.path.join(WT_DIR, tid)
    if diff_sandbox_check.is_git_worktree(wt):
        head = _git(["-C", wt, "rev-parse", "HEAD"])
        base = (head.stdout or "").strip()[:12] or base
        return wt, base, False

    if base is None:
        r = _git(["-C", ROOT, "rev-parse", "--short", "HEAD"])
        base = (r.stdout or "").strip()
        if not base:
            raise RunnerError("无法取仓库 HEAD：%s" % (r.stderr or "").strip())

    # 顺序不能反：先删目录再 prune。反过来 prune 看到的还是「路径存在」，
    # 注册不会被清掉，下一次 add 会静默失败（-q 把原因也吞掉）。
    if os.path.exists(wt):
        import shutil
        shutil.rmtree(wt, ignore_errors=True)
    _git(["-C", ROOT, "worktree", "prune"])

    add = _git(["-C", ROOT, "worktree", "add", "-B", "task/" + tid, wt, base])
    if add.returncode != 0:
        raise RunnerError("git worktree add 失败（写域校验会在没有 worktree 的情况下空转）：\n%s"
                          % ((add.stderr or "") + (add.stdout or "")).strip())
    return wt, base, True


# --------------------------------------------------------------------- 2 租约
class LeaseKeeper:
    """按 lease_heartbeat_sec 续租，同时写 state/hb/ 心跳。

    两件事周期相近但含义不同，必须分开（见 health.py 的注释）：
      续租失败 → 别人可以抢这个任务      → 严重，要写 sidecar
      心跳缺失 → 看门狗认为 worker 死了   → 也严重，但由 health.py 判定
    把两者揉成一个定时器，会让「心跳正常但租约已被抢」这种最危险的状态无法表达。
    """

    def __init__(self, st, tid: str, epoch: int, ttl: float, beat: float):
        self.st = st
        self.tid = tid
        self.epoch = epoch
        self.ttl = ttl
        self.beat = max(1.0, beat)
        self._stop = threading.Event()
        self._th = None
        self.lost = False
        self.error = ""

    def _loop(self):
        while not self._stop.wait(self.beat):
            now = int(time.time() * 1000)
            try:
                health.write_hb(self.tid, now, epoch=self.epoch, pid=os.getpid(),
                                role="runner")
            except OSError as exc:
                sys.stderr.write("WARN heartbeat write failed (%s)\n" % exc)
            try:
                ok = self.st.renew_lease(self.tid, self.epoch, self.ttl)
            except Exception as exc:  # renew 内部已分类，这里兜底
                self.error = "%s: %s" % (type(exc).__name__, exc)
                self.lost = True
                sys.stderr.write("WARN renew_lease(%s) raised: %s\n" % (self.tid, self.error))
                return
            if not ok:
                # 租约被别人拿走：必须立刻让执行体停下，否则同任务两个执行体。
                self.lost = True
                sys.stderr.write("WARN lease lost on %s (epoch=%s); another runner owns it\n"
                                 % (self.tid, self.epoch))
                return

    def __enter__(self):
        self._th = threading.Thread(target=self._loop, name="lease-keeper", daemon=True)
        self._th.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._th:
            self._th.join(timeout=3)
        return False


# --------------------------------------------------------------------- 3 brief
def task_scope(tid: str) -> list:
    """任务自己声明的写域。**唯一来源**：acceptance.yaml，回落 acceptance.draft.yaml。

    抽成函数是因为它有两处读者：`write_brief` 要把 `<task.scope>` 展开给执行体看，
    `finish` 要拿它做写域复核。两处各写一遍取值逻辑的结果，就是
    "brief 里说能写 src/，复核时按空集放行" —— 一边说出来一边做不到。
    """
    for name in ("acceptance.yaml", "acceptance.draft.yaml"):
        p = os.path.join(TASKS_DIR, tid, name)
        if os.path.exists(p):
            try:
                got = wb_core.yaml_parse_file(p).get("scope") or []
            except Exception as exc:
                sys.stderr.write("WARN task_scope(%s): %s unreadable (%s)\n" % (tid, name, exc))
                continue
            return [x for x in got if isinstance(x, str) and x.strip()]
    return []


def recall_query(tid: str, spec: dict, objective: str, st=None) -> str:
    """本轮召回的查询词。

    第五十轮：`persona_loader.assemble(role=role)` 之前**从不传 query**，
    于是 `build_recall("")` 立刻返回空串 —— 显忆段从头到尾一次都没进过 brief。
    识海能写不能读，而所有测试都是绿的：没有一条断言问过「brief 里有没有
    召回结果」，它们只问「段顺序对不对」。

    查询词的取材只有三样，都来自这次派活本身：
      · 任务标题（**优先从链上 E02 的 title 取**，回落 brief.md 首行）
      · objective（派活那句话）
      · 本任务写域（`spec.write_scope` 展开后的目录）
    刻意**不**把整个 brief 或整条链当查询：那会让召回退化成"什么都像"，
    而 top-N 只有 8 条，等于随机取。

    标题为什么不能只读 brief.md：`write_brief()` 会**覆盖**同路径文件，
    而 `task-new` 写进去的首行 `# <tid> — <title>` 在第一次 prepare 之后
    就不存在了。第一版按 `-` 切分，还撞上了 `T-R40` 自己的连字符，
    切出 `R40 · lead` —— 任务号被当成了标题前半截。
    """
    parts = []
    title = ""
    if st is not None:
        try:
            for row in st._rows():
                if row.get("kind") != "draft_created":
                    continue
                pl_ = row.get("payload") or {}
                if (pl_.get("task") or row.get("task")) == tid and pl_.get("title"):
                    title = str(pl_["title"]).strip()
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("WARN recall_query(%s): 读链上标题失败（%s）\n" % (tid, exc))
    if not title:
        p = os.path.join(TASKS_DIR, tid, "brief.md")
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    head = (fh.readline() or "").strip().lstrip("#").strip()
                # 只认破折号分隔；`T-R40` 里的连字符不是分隔符。
                for sep in ("—", "–", " - "):
                    if sep in head:
                        head = head.split(sep, 1)[1].strip()
                        break
                title = head
            except OSError as exc:
                sys.stderr.write("WARN recall_query(%s): 读 brief 首行失败（%s）\n" % (tid, exc))
    if title:
        parts.append(title)
    obj = (objective or "").strip()
    if obj:
        parts.append(obj)
    for s in _expand_scope(tid, spec.get("write_scope")):
        # 写域的 `**` 之类不是词，留着只会污染二元组。
        parts.append(s.replace("**", "").replace("*", "").strip("/"))
    return " ".join(x for x in parts if x)[:400]


def write_brief(tid: str, role: str, spec: dict, wt: str, base: str, objective: str,
                store=None) -> str:
    """生成 brief.md。这是控制面交给执行体的唯一输入。

    刻意包含「不要做什么」：执行体最容易犯的错不是不会做，是顺手多做了一点
    （改到别人的文件、升级依赖、动控制面）。写域会拦下越界，但被拦下意味着
    一轮白跑，所以先讲清楚比事后拒绝便宜。
    """
    rd = os.path.join(TASKS_DIR, tid)
    os.makedirs(rd, exist_ok=True)
    scope = spec.get("write_scope") or []
    lines = [
        "# brief · %s · %s" % (tid, role),
        "",
        "> 由 `agent_runner.py` 生成。执行体按本文件在 worktree 内作业，",
        "> 完成后写 sentinel：`%s`。" % os.path.relpath(
            os.path.join(DONE_DIR, "%s.%s.json" % (tid, role)), ROOT).replace("\\", "/"),
        "",
        "## 角色",
        "",
        "- role: `%s`" % role,
        "- actor: `%s`" % (spec.get("actor") or role),
        "- executor: `%s`" % (spec.get("executor") or "?"),
        "- slots: %s" % spec.get("slots"),
        "",
        "## 工作区",
        "",
        "```",
        "worktree : %s" % wt,
        "base     : %s" % base,
        "```",
        "",
        "所有改动必须发生在 worktree 里。写域（前缀相交，§5.5）：",
        "",
    ]
    for s in scope:
        if s == "<task.scope>":
            got = task_scope(tid)
            if got:
                for g in got:
                    lines.append("- `%s`" % g)
                lines.append("")
                lines.append("（`<task.scope>` 已按本任务 acceptance 的 scope 展开；"
                             "文件清单见 `.workbench/tasks/%s/acceptance.yaml`）" % tid)
            else:
                lines.append("- `<task.scope>` —— **未能展开**：本任务没有 acceptance "
                             "scope，写域复核将退化为仅 allow/deny")
        else:
            lines.append("- `%s`" % s)
    lines += [
        "",
        "**写了域外的文件会被控制面拒绝**（`diff_sandbox_check.check`），",
        "拒绝的代价是这一轮白跑，不是「下次注意」。",
        "",
        "## 目标",
        "",
        objective.strip() or "（未提供 objective；按任务 acceptance 推断）",
        "",
        "## 禁止",
        "",
        "- 不要修改 `.workbench/` 下任何文件（控制面资产）",
        "- 不要 `git commit` / `git push` / `git checkout`（合并由 `merge_queue` 负责）",
        "- 不要新增依赖（`mg.never_waive` 里的 `dependencies` 会让合并升级为人工双签）",
        "- 不要改验收口径来让验收通过",
        "",
        "## 完成后写 sentinel",
        "",
        "```json",
        "{",
        '  "ok": true,',
        '  "summary": "一句话说明做了什么",',
        '  "changed": ["src/calc.py"],',
        '  "selfcheck": {"cmd": "python tests/test_calc.py", "exit": 0}',
        "}",
        "```",
        "",
        "路径：`%s`" % os.path.join(DONE_DIR, "%s.%s.json" % (tid, role)),
        "",
    ]
    # ---------------------------------------------------------------- 神魂注入
    # 位置刻意在「角色卡」之前：角色卡说的是「你在这个工作台里担任什么」，
    # 神魂说的是「你是谁」。先有身份再谈岗位，顺序反了会读成一份岗位说明书。
    #
    # 第五十轮：这里以前调 `assemble(role=role)`，query 从来没传过 ——
    # 显忆段因 `build_recall("")` 立即返回空串而永不出现，而链上
    # `memory_recall` sidecar 也一直是空的（连"想过但没想起来"都没留下）。
    # 现在传 query（任务标题 + objective + 写域）与 store（写召回台账）。
    if persona_loader is not None:
        _q = recall_query(tid, spec, objective)
        try:
            _rep = persona_loader.assemble_report(role=role, query=_q,
                                                  store=store, task=tid)
            _persona = _rep["text"]
        except Exception as _exc:  # pragma: no cover
            _rep, _persona = {"dropped": [], "recall_injected": False,
                              "sections": [], "used_chars": 0}, ""
            print("  WARN persona 组装失败（不影响派发）：%s" % _exc, file=sys.stderr)
        if _rep.get("dropped"):
            # 被预算整段丢掉的段必须说出来：第四十八轮的分诊段就是这样
            # 无声地没进过 brief，而所有测试都绿着。
            print("  WARN persona 预算 %.0f 丢掉了 %s（%s）"
                  % (_rep.get("budget") or 0,
                     ", ".join(d["title"] for d in _rep["dropped"]),
                     "、".join("%s %d 字符" % (d["title"], d["chars"])
                               for d in _rep["dropped"])),
                  file=sys.stderr)
        if _rep.get("reserve_starved_static"):
            # 预算本身够装下全部静态段，是「显忆预留」把某一段挤掉的。
            # 这不是取舍：把 inject_max_chars 调够就两全，让一段无声消失不是选项。
            sys.stderr.write(
                "WARN persona 显忆预留挤掉了静态段（reserve=%s budget=%s）："
                "请调大 persona.inject_max_chars，而不是接受少一段\\n"
                % (_rep.get("reserve"), _rep.get("budget")))
        if _q and not _rep.get("recall_injected"):
            # 查了但没注入 ≠ 没查。前者可能是"没想起来"，也可能是"想起来了
            # 但被预算丢掉"—— 后者在 dropped 里已经报过；这里只说没有显忆段。
            sys.stderr.write("NOTE persona 显忆段为空（query=%r，召回无命中或未注入）\n"
                             % _q[:80])
        if _persona:
            lines.extend([_persona.rstrip(), ""])
    lines.extend([
        "## 角色卡",
        "",
        role_card(role),
    ])
    p = os.path.join(rd, "brief.md")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    return p


# --------------------------------------------------------------------- 4 派发
def announce_worker(st, tid: str, role: str, pid: int, creation: str = "",
                    spec_executor: str = ""):
    """写 E05 spawn + 迁移 claimed→in_progress（T06）。

    这一步**只能由真正起了 worker 的那个进程做**。以前是 `lead.py task-claim`
    写的，pid 填的是 Lead 自己的 —— 于是链上"派出去了"这件事是真的，
    被派出去的那个进程是假的。这里改成由 runner 在 `spawn_group` 拿到真实
    子进程 pid 之后写，`creation_date` 一并落链，`task-kill` 与
    `task-escalate` 的 H5 判活才能对得上人（否则 pid 复用会杀掉别人的进程）。
    """
    cur = st.current_state(tid)
    pl = {"task": tid, "pid": int(pid), "role": role,
          "executor": (spec_executor or "")}
    # 第三十七轮：`pid_observable` 必须**两个方向都写**。
    # 原来只在 pid<=0 时写 False，pid>0 时干脆不写这个键 ——
    # 于是链上「有个真进程」和「没人写过这件事」在 JSON 里长得一样，
    # 而 boss_report 的风险判据只认 `is False`，`None` 静默通过，
    # 「本机看不到执行体存活」这件事就被藏进了一个缺失的字段里。
    # pid=0 不是"某个进程"，是"没有本地可观测进程"。链上必须能读出这个区别，
    # 否则 H5 会把 pid=0 当成一个已死进程，而把一个陌生 pid 当成活人。
    pl["pid_observable"] = int(pid) > 0
    if creation:
        pl["creation_date"] = creation
    if cur == "claimed":
        # 第一个 worker：状态从未开工变成开工中，这次要迁。
        sp = st.append_event("spawn", pl, "system:runner", tid)
        tr = st.transition(tid, "in_progress", "system:runner", reason_event=sp["seq"])
        return sp, tr
    if cur == "in_progress":
        # 第 2..n 个执行体（同一任务多角色接力，例如 architect 之后 coder）。
        # 状态没变，所以**不写 E08** —— 单载体模型的要点是"每次状态变化
        # 恰好一条 state 事件"，而不是"每个动作一条"。硬写一条
        # in_progress→in_progress 会被 check_transition 判成 no-op 拒绝，
        # 而为了绕过它去放宽规则，就等于给状态机开了一个后门。
        sp = st.append_event("spawn", pl, "system:runner", tid)
        return sp, None
    raise RunnerError("announce: %s 当前是 %r，只有 claimed / in_progress 才能启动 worker"
                      "（先 lead.py task-claim）" % (tid, cur))


def _expand_scope(tid: str, raw) -> list:
    """把角色卡里的 `<task.scope>` 展开成本任务的实际写域。

    三处读者（brief / job / finish）共用它。上一版只有 finish 做了替换，
    于是 brief 里写着 `<task.scope>`、job 里也写着 `<task.scope>` ——
    一个占位符在三个地方有三个含义，其中两个是"你自己猜"。
    """
    out = []
    for s in (raw or []):
        if s == "<task.scope>":
            out.extend(task_scope(tid))
        elif isinstance(s, str) and s.strip():
            out.append(s)
    return out


def write_job(tid: str, role: str, spec: dict, brief: str, wt: str,
              base: str, wait_sec: int) -> str:
    """写 job 文件 —— 外部执行体的**唯一机器可读输入**。

    `prepare` 与 `dispatch_job` 共用它。上一版只有 `dispatch_job` 写 job，
    于是 `prepare` 之后外部执行体只能靠人肉转述"去读哪个 brief、签署写到哪" ——
    而 job 文件本来就是为这件事存在的：`executor / model / deadline / base /
    done_sentinel` 全在里面，执行体不需要猜、也不需要被口头告知。
    """
    done = os.path.join(DONE_DIR, "%s.%s.json" % (tid, role))
    now = int(time.time() * 1000)
    job = {
        "task": tid, "role": role,
        "executor": (spec.get("executor") or "").strip(),
        "brief": brief, "worktree": wt, "done_sentinel": done,
        "base": base,
        "created_ms": now,
        "deadline_ms": now + max(1, int(wait_sec)) * 1000,
        "model": spec.get("model"),
        # job 与 brief 必须给同一个答案：`<task.scope>` 在这里也要展开。
        # 上一版把占位符原样写进 job，外部执行体读到的是一条模板语法 ——
        # 它要么去猜，要么按整仓写，两种都比"直接告诉它"贵。
        "write_scope": _expand_scope(tid, spec.get("write_scope")),
    }
    _write_json_atomic(os.path.join(JOB_DIR, "%s.%s.json" % (tid, role)), job)
    return os.path.join(JOB_DIR, "%s.%s.json" % (tid, role))


def dispatch_job(tid: str, role: str, spec: dict, brief: str, wt: str,
                 wait_sec: int, poll_sec: float = 0.5, base: str = "",
                 on_spawn=None) -> dict:
    """把活派出去。三种 executor 走三条路，但等待语义统一。

    `on_spawn(pid, creation)` 在真正起了子进程后立刻回调，用来写 E05。
    只对 local_process 触发：另外两种 executor 的进程不由本脚本创建，
    它们的 spawn 必须由派发方用 `agent_runner.py announce --pid` 补上。
    """
    ex = (spec.get("executor") or "").strip()
    done = os.path.join(DONE_DIR, "%s.%s.json" % (tid, role))

    if os.path.exists(done):
        # 上一轮的 sentinel 会让这一轮"瞬间成功"。这是真实的坑：
        # 重跑同一任务时 runner 会因为看到旧签署而立刻宣布完成。
        os.replace(done, done + ".stale.%d" % int(time.time() * 1000))
        sys.stderr.write("NOTE archived stale sentinel for %s/%s\n" % (tid, role))

    jobp = write_job(tid, role, spec, brief, wt, base, wait_sec)

    if ex == "local_process":
        ep = spec.get("entrypoint")
        if not ep:
            raise RunnerError("%s 的 executor=local_process 但没声明 entrypoint" % role)
        ep_path = ep if os.path.isabs(ep) else os.path.join(WB, ep)
        if not os.path.exists(ep_path):
            raise RunnerError("entrypoint 不存在: %s" % ep_path)
        # 执行体需要知道自己的签署写到哪 —— 这是它与控制面之间唯一的约定。
        # 不给它路径而让它自己拼，等于让每个执行体各自实现一份路径规则。
        # D-54：原来是 dict(os.environ) 全量复制，WORKBENCH_ROOT_KEY 连同
        # 父进程的 CI/云凭据一起递给了 worker。定稿 §2.3.1(:128) 明写
        # 「启动 worker 时显式从 env 里剔除」。这里先净化再补 WB_*；
        # platform_ops.spawn_group 里还有一道兜底（防止别的调用方绕过）。
        env = platform_ops.scrub_env(os.environ)
        env["WB_SENTINEL"] = done
        env["WB_TASK"] = tid
        env["WB_ROLE"] = role
        env["WB_WORKTREE"] = wt
        env["WB_HOME"] = WB
        # 第十五轮：真实 local_process worker 进入进程级隔离。
        # platform_ops.spawn_group 只有在 WB_ISOLATE=1 时才把 worker_shim
        # 注入 PYTHONPATH；验收命令默认不带这个标记，不会被无差别卷入。
        env["WB_ISOLATE"] = "1"
        # 起进程前先写 E05：events 的因果是"先有 worker 才有 in_progress"，
        # 而 on_spawn 需要 pid，所以用 spawn_group 拿到 Popen 再回调。
        # 第二十三轮：真实 local_process worker 显式 opt-in 进程遏制。
        # 这不是「沙箱就位」：Job Object 只做 kill-on-close / 后代归属，
        # 网络与文件系统隔离仍由 WB_ISOLATE 的 worker_shim 负责。
        proc = platform_ops.spawn_group([sys.executable, ep_path, "--task", tid,
                                         "--worktree", wt],
                                        cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, contain=True)
        creation = ""
        try:
            creation = platform_ops.process_creation_date(proc.pid) or ""
        except Exception as exc:
            sys.stderr.write("WARN creation_date(%s) unavailable: %s\n" % (proc.pid, exc))
        if on_spawn:
            on_spawn(proc.pid, creation)
        out, err = proc.communicate(timeout=max(30, int(wait_sec)))
        return {"mode": "local_process", "rc": proc.returncode, "pid": proc.pid,
                "stdout": (out or "")[-4000:] if isinstance(out, str) else (out or b"").decode("utf-8", "replace")[-4000:],
                "stderr": (err or "")[-4000:] if isinstance(err, str) else (err or b"").decode("utf-8", "replace")[-4000:],
                "sentinel": _read_json(done)}

    # harness_subagent / human_cli：只写 job，然后等 sentinel。
    # 这两种的「谁去把 job 变成 sentinel」在控制面之外——DSH 调度器或人。
    if ex == "external_ai":
        # 外部 AI 执行体是本机脚本，控制面可以自己起它，所以这条路能自转：
        # 派活、等签署都不用人在场。这是它和 harness_subagent 的实际差别。
        ep_path = os.path.join(WB, "executors", "ext_ai_subagent.py")
        if not os.path.exists(ep_path):
            raise RunnerError("executors/ext_ai_subagent.py 不存在（external_ai 无实现）")
        env = platform_ops.scrub_env(os.environ)
        env["WB_SENTINEL"] = done
        env["WB_TASK"] = tid
        env["WB_ROLE"] = role
        env["WB_WORKTREE"] = wt
        env["WB_HOME"] = WB
        env["PYTHONIOENCODING"] = "utf-8"
        # 刻意**不**往 env 里放任何供应商密钥：执行体自己去读它所属产品的
        # 本地凭据（例：Kimi 的 daimon config.json）。密钥根本不经过工作台，
        # 于是 env_deny 里那串 *_API_KEY / *_TOKEN 也就不必为它开口子。
        backend = (spec.get("backend") or os.environ.get("WB_EXT_AI_BACKEND")
                   or "external")
        mode = spec.get("mode") or "consult"
        argv = [sys.executable, ep_path, "--job", jobp,
                "--backend", backend, "--mode", mode]
        if spec.get("provider"):
            argv += ["--provider", spec["provider"]]
        proc = platform_ops.spawn_group(argv, cwd=ROOT, env=env,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, contain=True)
        # 与 local_process 一样有真实子进程，所以 E05 也在这里补。
        # 差别只在「子进程里跑的是谁」：那边是我们自己的脚本，这边是
        # 一个去问第三方 AI 的适配器。控制面记的账是同一笔。
        creation = ""
        try:
            creation = platform_ops.process_creation_date(proc.pid) or ""
        except Exception as exc:
            sys.stderr.write("WARN creation_date(%s) unavailable: %s\n"
                             % (proc.pid, exc))
        if on_spawn:
            on_spawn(proc.pid, creation)
        out, err = proc.communicate(timeout=max(30, int(wait_sec)))
        return {"mode": "external_ai", "rc": proc.returncode, "pid": proc.pid,
                "stdout": (out if isinstance(out, str)
                           else (out or b"").decode("utf-8", "replace"))[-4000:],
                "stderr": (err if isinstance(err, str)
                           else (err or b"").decode("utf-8", "replace"))[-4000:],
                "sentinel": _read_json(done)}

    # harness_subagent / human_cli：只写 job，然后等 sentinel。
    # 这两种的「谁去把 job 变成 sentinel」在控制面之外——DSH 调度器或人。
    # 控制面不知道对面是谁，也不需要知道。
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        time.sleep(poll_sec)
        if os.path.exists(done):
            return {"mode": ex, "sentinel": _read_json(done)}
    return {"mode": ex, "sentinel": {}, "timeout": True}


# --------------------------------------------------------------------- 5 收尾
def _record_exit(st, tid: str, result: dict) -> None:
    """为一个已结束的 worker 写配对的 E07。

    为什么需要它：H5 的原文是「有 spawn 就必须能证明 worker 已死」，
    而最早的实现只认 `task-kill` 写的 E07。于是**正常完成**的 worker
    永远没有 kill 记录 —— 它自己退出了，没人杀它 —— 任何一次
    `task-escalate` 都会被 H5 拦住。一个只在异常路径上才可能满足的
    前置条件，等于把正常路径也一并否决。

    E07 的语义因此扩成「worker 已终止的证据」，`method` 说明证据来源：
    `killed` = 真的杀了；`exited` = 本地进程自己退出（有 rc）；
    `sentinel` = 外部执行体交了签署（无本地进程可等）。
    """
    for row in st._rows():                       # 已经有 kill 就不再补
        if row.get("kind") == "kill" and \
                ((row.get("payload") or {}).get("task") or row.get("task")) == tid:
            return
    pid, method, rc = 0, "sentinel", None
    for row in st._rows():
        if row.get("kind") == "spawn" and \
                ((row.get("payload") or {}).get("task") or row.get("task")) == tid:
            pid = (row.get("payload") or {}).get("pid") or 0
    if result.get("mode") in ("local_process", "external_ai"):
        # 两条路都有真实子进程与退出码，所以「它退了」这件事有本地证据。
        # 只有 harness_subagent / human_cli 那种控制面不起进程的情形，
        # 才退化成 method=sentinel（只能拿签署当"它结束了"的证据）。
        method, rc = "exited", result.get("rc")
    pl = {"task": tid, "pid": int(pid) if isinstance(pid, int) else 0,
          "method": method, "killed": False}
    if rc is not None:
        pl["rc"] = int(rc)
    try:
        st.append_event("kill", pl, "system:runner", tid,
                        meta={"paired_with": "spawn", "auto": True})
    except Exception as exc:
        sys.stderr.write("WARN %s: E07 exit record not written: %s\n" % (tid, exc))


def finish(tid: str, role: str, spec: dict, wt: str, base: str,
           result: dict, submit: bool, st=None) -> int:
    """写域复核 → result.json → 释放租约 →（可选）交回 lead 提交。

    顺序不能换。先复核再写 result.json：result 是执行体的自我陈述，
    而复核是控制面的独立判断；让自我陈述先落盘，会让人误以为它已经被认可。
    """
    sent = result.get("sentinel") or {}
    if result.get("timeout"):
        print("TIMEOUT %s/%s：等待 sentinel 超时，未取得执行体签署" % (tid, role))
        return 4

    changed = diff_sandbox_check.git_changed(wt, base) if diff_sandbox_check.is_git_worktree(wt) else []
    # `<task.scope>` 是占位符，指"任务自己声明的写域"。上一版把它**过滤掉**
    # 就完事 —— 列表变空，前缀相交在空集上恒真，于是 coder 在 runner 这一层
    # 等于完全不受写域约束（只剩全局 allow/deny）。最终还有
    # `lead.py task-submit` 兜底，但 runner 自称的"写域复核"是一句空话。
    # 真正的语义是替换：从该任务的 acceptance 里取 scope，取不到就
    # 退化成"整仓"并明确告警——不静默放行。
    scope = []
    for s in (spec.get("write_scope") or []):
        if s == "<task.scope>":
            got = task_scope(tid)
            if not got:
                sys.stderr.write("WARN %s/%s: write_scope 是 <task.scope> 但任务没有 "
                                 "acceptance scope；本轮写域复核退化为仅 allow/deny\n"
                                 % (tid, role))
            scope.extend(got)
        else:
            scope.append(s)

    # 写域复核用**两把尺子**（第四十轮 T-R40 真跑抓出的真缺陷）：
    #   scope → 任务 acceptance 声明的目录，逐段前缀相交；scope_to_prefix
    #           只认尾部通配（docs/architecture/**）。
    #   allow → 角色/全局白名单，完整 glob 匹配，可含 mid-path **，
    #           例如 .workbench/tasks/**/brief.md。
    # coder 的 write_scope 是 ["<task.scope>"]，展开后就是任务 scope（尾部
    # 通配），所以「把角色白名单直接当 scope」一直没出事；lead 的白名单里
    # 有 mid-path **，T-R40 于是每单必红
    # "mid-path wildcard not allowed in scope"，链路断在 E05/E08 之后。
    # 这个角色从没被真跑过 —— 规则写了没人读，就是声明悬空。
    # 修法：lead 的 scope 取任务声明目录，角色白名单降到 allow；deny 仍从
    # paths.yaml 读（"谁都不许写"对 lead 一样有效）。task-submit 随后再用
    # acceptance scope + paths.yaml 全量复核第二遍，两道闸门各量一件事。
    denys = []
    allows = []
    if role in ("coder", "architect"):
        paths = wb_core.yaml_parse_file(os.path.join(WB, "spec", "paths.yaml"))
        denys = paths.get("deny") or []
        allows = paths.get("allow_write") or []
    elif role == "lead":
        paths = wb_core.yaml_parse_file(os.path.join(WB, "spec", "paths.yaml"))
        denys = paths.get("deny") or []
        allows = [s for s in (spec.get("write_scope") or [])
                  if isinstance(s, str) and s.strip()]
        scope = [s for s in task_scope(tid)
                 if isinstance(s, str) and s.strip()]
        if not scope:
            # 没有任务声明目录就判空：check() 在空 prefixes 上对每个 changed
            # 都报 "outside task scope"，是 fail-closed，不静默放行。
            print("WARN %s/lead: acceptance 没有 scope，写域判空"
                  "（拒绝一切改动，不静默放行）" % tid, file=sys.stderr)
    # allow_write / deny 在 paths.yaml 里都是 `{glob, owner/reason}` 字典。
    # check() 里 deny 只做 `str(s)`，不做 dict→glob 提取（allow 做了）。
    # 直接把 dict 列表塞进去，deny 会变成一堆 "{'glob': ...}" 字符串，
    # glob_match 永远不命中 —— 表上写着"谁都不许写"，闸门却一次都没拦过。
    # lead.py task-submit 那边早已逐条提取，runner 这一层必须对齐。
    def _globs(items):
        return [x if isinstance(x, str) else x.get("glob")
                for x in (items or []) if x] or None

    chk = diff_sandbox_check.check(scope, changed,
                                   _globs(allows),
                                   _globs(denys),
                                   (wb_core.yaml_parse_file(
                                       os.path.join(WB, "spec", "paths.yaml"))
                                    .get("deny_absolute_prefix") or None))
    # 自报的 changed 与 Git 事实取并集：防漏看不防多看（与 merge 闸门同一取向）。
    # 比较前必须统一分隔符：执行体在 Windows 上写的是 `contracts\calc-v2.md`，
    # 而 `git diff --name-status` 永远给 `/`。不归一会得到
    # 「声称改了但 Git 里看不到」的假警报 —— 而假警报的代价是
    # 真的那条（执行体确实没落盘）从此也不被相信。
    def _norm(x: str) -> str:
        return str(x).replace("\\", "/").lstrip("./")

    claimed = [_norm(c) for c in (sent.get("changed") or []) if isinstance(c, str)]
    extra = sorted(set(claimed) - set(changed))
    if extra:
        print("WARN 执行体声称改了 %s，但 Git 里看不到（可能未落盘）" % extra)

    if not chk["ok"]:
        print("REFUSE %s/%s：写域越界" % (tid, role))
        for v in chk["violations"]:
            print("  - %s  %s" % (v.get("file"), v.get("reason")))
        return 5

    rd = os.path.join(TASKS_DIR, tid)
    os.makedirs(rd, exist_ok=True)
    out = {
        "task": tid, "role": role, "executor": spec.get("executor"),
        "base_commit": base, "changed_files": changed,
        "sentinel": sent, "dispatch": {k: v for k, v in result.items() if k != "sentinel"},
        "finished_ms": int(time.time() * 1000),
    }
    if role == "verifier":
        # 验收结果由 verify_harness 写 result.json，这里不覆盖它。
        print("NOTE verifier 角色不写 result.json（verify_harness 拥有该文件）")
    else:
        with open(os.path.join(rd, "result.%s.json" % role), "w",
                  encoding="utf-8", newline="\n") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2, sort_keys=True)

    print("OK %s/%s：changed=%d %s" % (tid, role, len(changed), changed or ""))
    if sent.get("summary"):
        print("   summary: %s" % sent["summary"])
    if sent.get("selfcheck"):
        sc = sent["selfcheck"]
        print("   selfcheck: %s → exit=%s" % (sc.get("cmd"), sc.get("exit")))

    # 上一版这里直接写 `_record_exit(st, tid, result)`，而 `finish` 是模块级
    # 函数，`st` 根本不在作用域里 —— 于是**每一条正常收尾**都死在
    # `NameError: name 'st' is not defined`：工作区已改好、写域已复核通过，
    # 只死在最后一步。这类错最坏的地方在于它的位置：前面每行都打印 OK，
    # 而 demo.sh / run_all_checks 把 [exit=1] 当成了可忽略的输出。
    if st is None:
        try:
            st = store()
        except Exception as exc:      # noqa: BLE001
            sys.stderr.write("WARN %s: 无法打开 Store，E07 终止证据未写（%s）\n"
                             % (tid, exc))
            st = None
    if st is not None:
        try:
            _record_exit(st, tid, result)
        except Exception as exc:      # noqa: BLE001
            # 终止证据写不成不等于这一轮白做：E09 提交仍在，H5 会退回 H5-b。
            sys.stderr.write("WARN %s: E07 exit record not written (%s: %s)\n"
                             % (tid, type(exc).__name__, exc))

    if submit and changed:
        # 交回 lead：这里只启动它，不复制它的逻辑。
        # submit 里有 patch 摘要、secret 扫描、租约栅栏、零改动守卫 ——
        # 任何一条在这里重写一遍，就是第二份"看起来还在守"的实现。
        lp = os.path.join(HERE, "lead.py")
        rc = subprocess.run([sys.executable, lp, "task-submit", "--task", tid,
                             "--worktree", wt, "--base", base],
                            capture_output=True, encoding="utf-8", errors="replace")
        sys.stdout.write(proc_text(rc))
        return rc.returncode
    return 0


def proc_text(p) -> str:
    return (p.stdout or "") + (p.stderr or "")


# --------------------------------------------------------------------- CLI
def _beat_sec() -> float:
    g = cfg().get("global_config") or {}
    ttl = float(g.get("lease_ttl_sec", 30))
    hb = g.get("lease_heartbeat_sec")
    try:
        hb = float(hb) if hb is not None else ttl / 3.0
    except (TypeError, ValueError):
        hb = ttl / 3.0
    if hb <= 0 or hb >= ttl:
        sys.stderr.write("WARN lease_heartbeat_sec=%r invalid for lease_ttl_sec=%.1f; using %.1f\n"
                         % (hb, ttl, ttl / 2.0))
        hb = ttl / 2.0
    return hb


def _spec_with_overrides(a) -> dict:
    """允许在一次运行里临时改 executor / entrypoint。

    为什么要有：`agents.json` 里 coder 写的是 `harness_subagent`，那条路
    需要一个外部框架真的拉起 worker。想验证"控制面到执行体"这一段本身
    （而不是验证某个框架），就得能临时换成 `local_process`。
    改 `agents.json` 是不行的 —— 它属可信根覆盖范围，改动需要 E27；
    而每验证一次就写一条 constitution_change，等于把审计日志当草稿纸。

    覆盖必须留痕：打印 WARN，并且只在本次进程内生效。
    """
    spec = dict(role_spec(a.role))
    if getattr(a, "executor", None):
        sys.stderr.write("WARN executor override: %s.%s %r → %r (this run only)\n"
                         % (a.role, "executor", spec.get("executor"), a.executor))
        spec["executor"] = a.executor
    if getattr(a, "entrypoint", None):
        sys.stderr.write("WARN entrypoint override: %s.entrypoint → %r (this run only)\n"
                         % (a.role, a.entrypoint))
        spec["entrypoint"] = a.entrypoint
    return spec


def cmd_prepare(a):
    spec = _spec_with_overrides(a)
    wt, base, created = ensure_worktree(a.task, a.base)
    print("worktree %s (%s) base=%s" % (wt, "created" if created else "reused", base))
    # 第五十轮：prepare 也传 store —— 召回台账要留下「派发时想起了什么」。
    # 打不开 Store 不算致命（prepare 不该因为记账失败而派不出活）。
    try:
        _st = store()
    except Exception as _exc:  # noqa: BLE001
        sys.stderr.write("WARN prepare: Store 打不开，召回台账不写（%s）\n" % _exc)
        _st = None
    brief = write_brief(a.task, a.role, spec, wt, base, a.objective or "", store=_st)
    jobp = write_job(a.task, a.role, spec, brief, wt, base, int(a.wait or 1800))
    print("brief    %s" % brief)
    print("job      %s" % jobp)
    print("sentinel %s" % os.path.join(DONE_DIR, "%s.%s.json" % (a.task, a.role)))
    print("")
    print("外部执行体只需读 job 文件：executor / model / deadline / write_scope /")
    print("done_sentinel 全在里面，不需要口头转述。完成后：")
    print("  agent_runner.py finish --task %s --role %s --base %s"
          % (a.task, a.role, base))
    return 0


def lease_epoch(st, tid: str):
    """该任务当前的租约 epoch —— 从链上 E04 claim 回放，不从本进程时钟取。

    这是本轮实测撞出来的真缺陷：runner 原本 `epoch = int(time.time()*1000)`
    自己造一个新 epoch 去 `acquire_lease`，于是它拿到的是**第二个**租约，
    而 `lead.py task-submit` 的栅栏拿的是链上 E04 里的**第一个** epoch ——
    于是 runner 收尾时提交被自己拒掉：

        REFUSE T-0002 submit: task T-0002: epoch 1794235818381 superseded by 1794235818560

    两个 epoch 都是"真的"，但只有一个是对的。**租约的唯一权威源是 E04**，
    与 `lead.py task_epoch()` 同一口径。runner 与 lead 读两份不同的时间，
    等于两个人各自记了一笔"谁在干活"的账。
    """
    ep = None
    for row in st._rows():
        if row.get("kind") == "claim" and (row.get("task") == tid):
            e = (row.get("payload") or {}).get("epoch")
            if isinstance(e, int):
                ep = e
    return ep


def cmd_run(a):
    spec = _spec_with_overrides(a)
    st = store()
    tid = a.task

    # 租约：占工作区干活的角色需要独占。verifier 与被验任务并行
    # （agents.json 的 concurrency_rule 明确写了这条），所以它不取租约。
    # 第四十轮起 lead 也在这里面：它真的起本地进程干活了。
    exclusive = a.role in LEASE_ROLES
    ttl = float(cfg()["global_config"]["lease_ttl_sec"])
    epoch = lease_epoch(st, tid)
    keeper = None
    if exclusive:
        if epoch is None:
            raise RunnerError("%s 从未被 claim（链上没有 E04）；先 lead.py task-claim" % tid)
        # 用链上那个 epoch **续租**，而不是新开一个。
        # `acquire_lease` 只在"活的持有者且 epoch 更大"时拒绝，所以用同一个
        # epoch 调用会幂等地拿回自己那一份；新 epoch 则会覆盖它 ——
        # 覆盖的后果是 lead.py 的栅栏认为"执行体已失效"。
        if not st.acquire_lease(tid, epoch, ttl):
            st.append_sidecar("reject", {"task": tid, "stage": "runner",
                                         "reason": "lease_conflict", "role": a.role})
            print("REFUSE %s：租约被占用，另一个执行体在跑同一任务" % tid)
            return 3
        keeper = LeaseKeeper(st, tid, epoch, ttl, _beat_sec())

    wt, base, created = ensure_worktree(tid, a.base)
    brief = write_brief(tid, a.role, spec, wt, base, a.objective or "", store=st)
    print("prepare: worktree=%s base=%s brief=%s" % (wt, base, brief))

    ctx = keeper if keeper else _NullCtx()
    announced = {"done": False}
    # 只有真正"占工作区干活"的角色才写 spawn。verifier 是被 lead.py task-verify
    # 驱动的检查步骤，它没有"领走任务"的语义，写 spawn 会让 H5 把它当成
    # 一个待 kill 的 worker。第四十轮起 lead 真的起进程，也写 spawn ——
    # pid 由 dispatch_job 的 on_spawn 从真子进程取，不编一个假 pid。
    wants_spawn = a.role in SPAWN_ROLES

    def _on_spawn(pid, creation):
        if not wants_spawn:
            return
        try:
            sp, tr = announce_worker(st, tid, a.role, pid, creation,
                                     spec_executor=spec.get("executor") or "")
            announced["done"] = True
            log("spawn  E05=%d pid=%d creation=%s" % (sp["seq"], pid, creation or "-"))
            if tr is None:
                # 第二个及之后的执行体：状态已经是 in_progress，不产生迁移。
                log("state  (already in_progress, no state event) —— 单载体："
                    "每次状态变化恰好一条 state 事件")
            else:
                log("state  E08=%d claimed→in_progress (T06)" % tr["seq"])
        except Exception as exc:
            # 派发已经发生了；这里失败不仅影响记账，也让 H5 失去判据。
            sys.stderr.write("WARN announce_worker(%s/%s pid=%s) failed: %s\n"
                             % (tid, a.role, pid, exc))

    try:
        with ctx:
            res = dispatch_job(tid, a.role, spec, brief, wt, int(a.wait),
                               poll_sec=float(a.poll), base=base, on_spawn=_on_spawn)
            if keeper and keeper.lost:
                print("ABORT %s/%s：租约在作业期间丢失（%s）" % (tid, a.role, keeper.error or "superseded"))
                return 6
            if res.get("mode") not in ("local_process", "external_ai") \
                    and wants_spawn and not announced["done"]:
                print("NOTE %s/%s 由 external executor 执行；E05 需派发方补写："
                      "\n  agent_runner.py announce --task %s --role %s --pid <worker-pid>"
                      % (tid, a.role, tid, a.role))
            return finish(tid, a.role, spec, wt, base, res,
                          submit=bool(a.submit), st=st)
    finally:
        if exclusive:
            try:
                st.release_lease(tid, epoch)
            except Exception as exc:
                sys.stderr.write("WARN release_lease(%s) failed: %s\n" % (tid, exc))


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def cmd_finish(a):
    """prepare 与执行体分离时的收尾入口。

    存在理由：`run` 是"我派人、我等"的一体式用法，它会在派发前把旧 sentinel
    归档。但真实编排里执行体往往由外部驱动（DSH 子代理、另一台机器、人），
    那种情况下 prepare 与收尾之间隔着任意长的时间，用 run 会把已经写好的
    sentinel 当成上一轮的陈旧签署删掉。所以把 prepare / finish 拆开，
    让"执行体何时完成"由执行体自己决定，而不是由等待方假设。
    """
    spec = role_spec(a.role)
    st = store()
    tid = a.task
    wt = os.path.join(WT_DIR, tid)
    if not diff_sandbox_check.is_git_worktree(wt):
        raise RunnerError("%s 没有 worktree，先跑 prepare" % tid)
    base = a.base
    if not base:
        rd = _read_json(os.path.join(JOB_DIR, "%s.%s.json" % (tid, a.role)))
        base = rd.get("base") or ""
    if not base:
        r = _git(["-C", ROOT, "merge-base", "HEAD", "task/" + tid])
        base = (r.stdout or "").strip()[:12]
    if not base:
        raise RunnerError("无法确定 base commit；请显式传 --base")
    sent = _read_json(os.path.join(DONE_DIR, "%s.%s.json" % (tid, a.role)))
    if not sent:
        print("REFUSE %s/%s：没有 sentinel。执行体没写签署，控制面不替它猜" % (tid, a.role))
        return 4

    # 外部执行体不会自己写 E05（它的进程不在本进程树里），但**必须有人写**：
    # 否则状态停在 claimed，`task-submit` 会报
    # `illegal transition claimed→submitted`，整条外部路径等于断了。
    # 这里用 pid=0 表示"没有本地可观测进程"——写 `os.getpid()` 才是撒谎。
    cur = st.current_state(tid)
    if a.role in SPAWN_ROLES and cur == "claimed":
        try:
            sp, tr = announce_worker(st, tid, a.role, 0, "",
                                     spec_executor=spec.get("executor") or "")
            log("spawn  E05=%d pid=0 (external executor: %s) —— 无可观测本地进程"
                % (sp["seq"], spec.get("executor") or "?"))
            if tr is not None:
                log("state  E08=%d claimed→in_progress (T06)" % tr["seq"])
        except Exception as exc:
            sys.stderr.write("WARN announce_worker(%s/%s) failed: %s\n" % (tid, a.role, exc))

    res = {"mode": spec.get("executor"), "sentinel": sent}
    rc = finish(tid, a.role, spec, wt, base, res, submit=bool(a.submit), st=st)
    if a.role in LEASE_ROLES:
        ep = None
        for r in st._rows():
            if r.get("task") == tid and r.get("kind") == "claim":
                ep = (r.get("payload") or {}).get("epoch")
        if ep is not None:
            try:
                st.release_lease(tid, int(ep))
            except Exception as exc:
                sys.stderr.write("WARN release_lease(%s) failed: %s\n" % (tid, exc))
    return rc


def cmd_announce(a):
    """由派发方补写 E05 spawn（executor 不是本脚本创建进程时用）。

    `run` 走 local_process 时能拿到真实 pid，所以它自己写。但当执行体是
    DSH 子代理、另一台机器或一个人时，控制面从来没有 fork 出那个进程，
    硬要写 pid 就只能是 `os.getpid()` —— 那正是这条链之前撒的那个谎。
    所以留一个显式入口：谁起的进程，谁把 pid 报进来；报不进来就没有
    spawn 事件，`task-escalate` 的第二关（有 spawn 必须有 kill）自然放行，
    而 `task-kill` 也不会去杀一个不知道是谁的 pid。
    """
    spec = role_spec(a.role)
    st = store()
    sp, tr = announce_worker(st, a.task, a.role, int(a.pid), a.creation or "",
                             spec_executor=spec.get("executor") or "")
    log("spawn  E05=%d pid=%d role=%s creation=%s"
        % (sp["seq"], int(a.pid), a.role, a.creation or "-"))
    if tr is None:
        # 状态已经是 in_progress：第 2..n 个执行体接力，不产生迁移。
        # 上一版这里直接 `tr["seq"]` —— 而这条路径恰恰是唯一会走到它的路径，
        # 于是 `announce` 在最该工作的场景里必崩（TypeError: NoneType not subscriptable）。
        log("state  (already in_progress, no state event) —— 单载体："
            "每次状态变化恰好一条 state 事件")
    else:
        log("state  E08=%d claimed→in_progress (T06)" % tr["seq"])
    return 0


def cmd_status(a):
    jobs = []
    if os.path.isdir(JOB_DIR):
        for f in sorted(os.listdir(JOB_DIR)):
            if not f.endswith(".json"):
                continue
            j = _read_json(os.path.join(JOB_DIR, f))
            done = os.path.exists(j.get("done_sentinel") or "")
            jobs.append((j.get("task"), j.get("role"), j.get("executor"),
                         "done" if done else "open",
                         j.get("deadline_ms")))
    if not jobs:
        print("no agent job")
        return 0
    print("%-10s %-10s %-16s %-6s %s" % ("TASK", "ROLE", "EXECUTOR", "STATE", "DEADLINE_MS"))
    for t, r, e, s, d in jobs:
        print("%-10s %-10s %-16s %-6s %s" % (t, r, e, s, d))
    return 0


def cmd_roles(a):
    agents = load_agents()
    print("%-10s %-16s %-6s %-22s %s" % ("ROLE", "EXECUTOR", "SLOTS", "MODEL", "WRITE_SCOPE"))
    for name, r in (agents.get("roles") or {}).items():
        m = r.get("model") or {}
        ms = m.get("name") if isinstance(m, dict) else ""
        sc = r.get("write_scope") or []
        print("%-10s %-16s %-6s %-22s %s" % (name, r.get("executor"), r.get("slots"),
                                             ms or "-", ",".join(sc) or "-"))
    print("\npool: %s  max_parallel=%s" % (agents["pool"]["name"], agents["pool"]["max_parallel"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="角色执行槽：把 agents.json 的角色变成真实动作")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("prepare", help="只准备（worktree + brief + job），不等待")
    p.add_argument("--task", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--base")
    p.add_argument("--objective", default="")
    p.add_argument("--executor", choices=list(EXECUTOR_KINDS))
    p.add_argument("--entrypoint")
    p.add_argument("--wait", type=int, default=1800,
                   help="写进 job 文件的 deadline 秒数（prepare 不等待，但截止时间要写下来）")
    p.set_defaults(fn=cmd_prepare)

    p = sub.add_parser("run", help="准备 + 派发 + 等待 + 收尾")
    p.add_argument("--task", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--base")
    p.add_argument("--objective", default="")
    p.add_argument("--wait", type=int, default=1800, help="等待 sentinel 的秒数")
    p.add_argument("--poll", type=float, default=0.5)
    p.add_argument("--executor", choices=list(EXECUTOR_KINDS),
                   help="本次运行临时覆盖 agents.json 的 executor（不改可信根）")
    p.add_argument("--entrypoint", help="本次运行临时覆盖 entrypoint（仅 local_process 有意义）")
    p.add_argument("--submit", action="store_true", help="收尾后交回 lead.py task-submit")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("finish", help="prepare 之后由外部执行体完成时的收尾入口")
    p.add_argument("--task", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--base")
    p.add_argument("--submit", action="store_true")
    p.set_defaults(fn=cmd_finish)

    p = sub.add_parser("status", help="列出 agent 作业与签署状态")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("announce", help="由派发方补写 E05 spawn（executor 不在本进程内）")
    p.add_argument("--task", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--pid", required=True, type=int)
    p.add_argument("--creation", default="", help="进程创建时间，防 PID 复用")
    p.set_defaults(fn=cmd_announce)

    ap.add_argument("--verbose", action="store_true",
                    help="打过程行（spawn/state 回显）；默认只打结果与契约行")

    p = sub.add_parser("roles", help="打印角色→agent 分配")
    p.set_defaults(fn=cmd_roles)

    # argparse 的顶层 flag 不能放在子命令之后：`run --verbose` 会被子解析器
    # 判成 unrecognized arguments。先把 `--verbose` 从 argv 里摘出来再解析，
    # 这样 `--verbose run ...` 与 `run ... --verbose` 两种写法都成立 ——
    # run_task.py 的透传正是追加在末尾（它的调用点已经写好，不能反过来要求
    # 调用方把 flag 挪到前面）。
    argv = list(sys.argv[1:] if argv is None else argv)
    _verb = False
    while "--verbose" in argv:
        argv.remove("--verbose")
        _verb = True

    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    VERBOSE[0] = bool(_verb or getattr(a, "verbose", False))
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
