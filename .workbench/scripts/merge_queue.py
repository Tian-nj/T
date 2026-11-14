#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""merge_queue.py — 合并队列执行器（v3.2 §8.1 / §8.4 / §8.5）。system:merge_queue。

职责单一：把**已 submitted→done 通过任务级验收**的任务，按拓扑序合进 main。

它不是 `git merge` 的包装。三件事它必须自己做对：

  1. **预演而非试错**：用 `git merge-tree --write-tree`（Git ≥ 2.38）算出合并树，
     冲突在**不动工作区**的情况下判定。真去 `git merge` 再回滚会留下
     "正在合并"状态，后续所有任务都被污染。
  2. **冲突归因到任务**：冲突结果按文件集合反查是哪些任务改的，
     只把**真正冲突的那批**打回 rejected（MERGE_CONFLICT），
     不整批连坐——整批连坐会让一个倒霉任务拖死整条队列。
     merge-tree 拿不到冲突文件时才是基线取错（BASE_COMMIT_STALE）。
     §附录 F F14 要求 rejected(MERGE_CONFLICT)；把两类原因混成一个码，
     「代码有冲突」和「基线取错了」在链上会长得一模一样。
  3. **冒烟失败要冻结**：合并后 smoke 挂了，说明问题可能在任何一条刚合进来的
     提交里。此时**必须**冻结队列、批量 revert、按 changed_files 找受影响任务、
     逐个 escalate。静默继续合并等于把不确定性滚雪球。

    python merge_queue.py                 # 处理默认队列（main）
    python merge_queue.py --json
    python merge_queue.py --dry-run       # 只算不改
    python merge_queue.py --smoke "<cmd>" # 覆盖默认冒烟命令

退出码：0 全部合并成功 / 1 有任务被拒或升级 / 2 环境问题（无 Git、分支不存在）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
sys.path.insert(0, HERE)

import wb_core  # noqa: E402
import sign_manifest  # noqa: E402
import mg_gate  # noqa: E402

# 第四十轮：过程行默认收进 --verbose。机器读（--json）与契约行
# （REFUSE / WARN / RESULT / MERGED / SKIP / FROZEN / VERIFY …）保留裸 print，
# 它们是别的脚本和测试的输入，不是给 BOSS 看的日志。
VERBOSE = [False]   # 单元素列表：main() 要改它，模块级直接赋值会读到旧副本


def log(*parts, **kw):
    """搭建者日志。只在 --verbose 下打。"""
    if VERBOSE[0]:
        print(*parts, **kw)


def warn(*parts, **kw):
    """默认可见的降级/异常；写 stderr 而不是 stdout。

    `--json` 的 stdout 是机器读契约，降级提示混进去会让 JSON 解析直接崩。
    静默掉更坏：会把「退回旧尺子」藏成「一切正常」。
    """
    kw.setdefault("file", sys.stderr)
    print(*parts, **kw)


TASKS_DIR = os.path.join(WB, "tasks")
MERGE_LOG = os.path.join(WB, "state", "merge_queue.jsonl")


def store():
    return wb_core.Store(WB, sign_manifest.read_uuid_from_config())


def cfg():
    return wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))


def git(*args, cwd=None, check=False):
    p = subprocess.run(["git"] + list(args), cwd=cwd or ROOT,
                       capture_output=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise SystemExit("git %s failed: %s" % (" ".join(args), (p.stderr or "").strip()))
    return p


def rev(branch="main", cwd=None):
    p = git("rev-parse", "--verify", branch, cwd=cwd)
    return p.stdout.strip() if p.returncode == 0 else None


def resolve_gate_base(branch, head, tip):
    """闸门 diff 基线：优先 merge-base，失败时如实退回主干 tip。

    第四十一轮：上一版把回退写成一行 `or base`。那行代码唯一能被静默执行的
    操作就是「悄悄换一把旧尺子」——git 失败原因没进 result、没进任何日志，
    T-R40 那种伪 file_delete 会原地复活而没人知道。返回值必须带证据：
    source=merge-base/tip_fallback、detail 里有 rc/stderr，供 result 与 WARN 读。

    注意这里不抛异常：合并目标 `tip` 仍然有效，退回旧尺子只是保守（可能
    多要签名），不是数据损坏；但「保守」必须可观测，不能是无声默认。
    """
    p = git("merge-base", branch, head)
    lines = (p.stdout or "").strip().splitlines()
    sha = lines[0].strip() if lines else ""
    if p.returncode == 0 and sha:
        return {"base": sha, "source": "merge-base", "detail": "merge-base %s %s" % (branch, head[:12])}
    err = ((p.stderr or p.stdout or "").strip() or "no output")[:300]
    return {
        "base": tip,
        "source": "tip_fallback",
        "detail": "git merge-base rc=%d: %s" % (p.returncode, err),
        "rc": p.returncode,
        "stderr": err,
    }


def def_branch():
    """主干分支名不能写死 `main`。

    实测：这个仓库的默认分支是 `master`（`git init` 的历史行为，Windows 上
    Git for Windows 默认仍是 master）。硬编码 `main` 导致 merge_queue 直接
    「no branch 'main'」退出 2 —— 一个看起来像环境问题的错误，
    实际是工具把假设当成了事实。
    """
    p = git("symbolic-ref", "--short", "HEAD")
    name = (p.stdout or "").strip()
    if p.returncode == 0 and name and name != "HEAD":
        return name
    for cand in ("main", "master"):
        if rev(cand):
            return cand
    return "master"


def merge_tree_ok(base, head):
    """`git merge-tree --write-tree base head` 返回 (ok, tree_or_conflicts)。

    Git ≥2.38 的这个模式不写工作区、不动索引，纯粹算树。
    成功时 stdout 只有一行树 oid；冲突时退出码非 0，冲突文件要另外问一次。
    """
    p = git("merge-tree", "--write-tree", base, head)
    out = (p.stdout or "").strip()
    if p.returncode == 0 and out and "\n" not in out:
        return True, out
    files = []
    for line in (p.stdout or "").splitlines():
        if line.startswith("CONFLICT") or "\t" in line:
            part = line.split("\t")[-1].strip() if "\t" in line else ""
            if part:
                files.append(part)
    if not files:
        # 兼容旧格式：`merge-tree base head` 把冲突写在正文里
        for line in (p.stdout or "").splitlines():
            s = line.strip()
            for marker in ("changed in both", "added in both", "removed in"):
                if line.endswith(marker) and s:
                    files.append(s.rsplit(" ", 2)[0].strip())
    return False, sorted(set(f for f in files if f))


def changed_of(task):
    p = os.path.join(TASKS_DIR, task, "result.json")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as fh:
        return list(json.load(fh).get("changed_files") or [])


def done_tasks(st):
    """按 last_seq 升序给出所有 done 的任务（近似拓扑序：先完成先合）。"""
    rb = st.rebuild()["tasks"]
    out = [(i.get("last_seq", 0), t) for t, i in rb.items() if i.get("state") == "done"]
    out.sort()
    return [t for _, t in out]


def diff_stats(base, head):
    """给出 ``(statuses, added, deleted)`` 供闸门判定。

    statuses 用 ``git diff --name-status`` 的**首字母**：A/M/D/R/T。
    净增行取 ``--numstat`` 的加/减列；二进制文件在 numstat 里是 ``-``，
    跳过而不是当 0 —— 把二进制当 0 行会让一个 40MB 的资源替换被判成
    "规模很小，自动合并"。
    """
    statuses = {}
    p = git("diff", "--name-status", base, head)
    for line in (p.stdout or "").splitlines():
        cols = line.split("\t")
        if len(cols) >= 2:
            st_code = cols[0][:1]
            path = cols[-1] if st_code != "R" else cols[-1]
            statuses[path] = st_code
    added = deleted = 0
    p = git("diff", "--numstat", base, head)
    for line in (p.stdout or "").splitlines():
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        a, d = cols[0], cols[1]
        if a.isdigit():
            added += int(a)
        if d.isdigit():
            deleted += int(d)
    return statuses, added, deleted


def approved_of(st, tid):
    """该任务拿到的人签名集合（principal），不是"签过没有"。

    只认 ``human_approve``（E21）。``human_override`` 不算签名 —— override 是
    "越过状态机"，不是"我审过这份 diff"，拿它当合并许可等于让任何一次
    状态修正顺手放行一次合并。

    为什么返回集合：`never_waive` 要求 **双签**，而"有人签过"与
    "两个不同的人各自签过"是两件事。原实现返回布尔，于是
    `need == "double_sign"` 与 `need == "single_sign"` 在代码里走同一条
    `merge = bool(approved)` —— 闸门打印了 `double_sign`，然后对一次签名放行。
    **一条判定如果两个分支走同一条路，它就不是判定。**
    """
    who = set()
    for row in st._rows():
        if row.get("task") != tid or row.get("kind") != "human_approve":
            continue
        actor = row.get("actor") or ""
        name = actor[6:] if actor.startswith("human:") else actor
        if name:
            who.add(name)
    return who


def gate_of(tid, base, head, mg, gray_seen, approvals=None):
    """算出这次合并该走哪个分支，并按 need 兑现签名门槛。

    两段式：先让 `mg_gate.decide` 判分支（它不知道也不该知道"谁签了名"），
    再在这里把 `need` 翻译成"够不够"。分开的理由：`decide` 是纯策略，
    能被单测穷举；"签名够不够"依赖链上事实，只能在这里读。

    为什么要 probe 两遍：分支判定不该受签名影响（"这是禁区"是客观事实，
    不因为有人签过就变成非禁区），但 `merge` 该不该放行完全由签名决定。
    一遍算出来的 `merge` 把两件事揉在一起 —— 这正是"算出 double_sign
    却对一次签名放行"的根源。
    """
    files = changed_of(tid)
    statuses, added, deleted = diff_stats(base, head)
    # result.json 的 changed_files 是 coder 自报；Git 的 diff 是事实。
    # 两者不一致时取**并集**——闸门要防的是漏看，不是防多看。
    files = sorted(set(files) | set(statuses))
    approvals = set(approvals or ())
    probe = mg_gate.decide(files, mg, statuses=statuses, added=added,
                           deleted=deleted, gray_in_batch=gray_seen,
                           approved=True)
    met = needs_met(probe["need"], approvals)
    d = mg_gate.decide(files, mg, statuses=statuses, added=added, deleted=deleted,
                       gray_in_batch=gray_seen, approved=met)
    d["approvals"] = sorted(approvals)
    d["need_met"] = met
    return d


def needs_met(need, approvals):
    """签名门槛：none 恒真；single_sign 要 >=1 人；double_sign 要 >=2 个不同的人。

    为什么区分 principal 而不是数事件：同一个人签两次也是"两个事件"。
    双签的意义是"两个独立的人各自看过"，用事件数替代人数会把这条控制
    降级成"多点一次"。
    """
    n = len(set(approvals or ()))
    if need == "none":
        return True
    if need == "single_sign":
        return n >= 1
    if need == "double_sign":
        return n >= 2
    return False


def gate_reject(st, tid, d, dry_run=False):
    """闸门不许合并时的记账。

    NEEDS_REBREAK / GRAY_OVERFLOW → ``escalate``（P1，要人重拆或补签）。
    用 escalate 而不是 verify(fail)：验收已经过了，卡住的是**合并策略**，
    写成验收失败会把"代码不对"和"流程不许"混成一件事。

    **但"缺签名"不属于这一档，它必须留在 done。**
    `done_tasks()` 只从 done 里取候选；一旦把"只差一个签名"的任务迁到
    escalated，它就永久离开合并队列 —— 之后补多少签名都不会被重新评估，
    因为根本没有第二次评估。实测 T-0005 就是这样掉出去的：
    链上先 escalate（seq 55→56），再 human_approve（seq 57），
    merge_queue 第二轮只打印 `SKIP T-0004` 就收工，T-0005 连候选都不是。
    这个分支的语义是「等人来签」，而等人来签的前提是**下一轮还会被看**。

    为什么不为此写一条事件：缺签名这件事是**可重算**的 —— 给定链上的
    human_approve 集合与 git 的两棵树，闸门会给出同一个结论。为可重算的
    瞬时状态写链事件，等于把每次轮询都变成一条历史噪声；而 escalate 是
    不可逆的状态迁移，那才必须落链。这里只记 sidecar 账。
    """
    wait_sign = (d.get("need") in ("single_sign", "double_sign")
                 and not d.get("need_met"))
    code = "NEEDS_REBREAK"
    payload = {
        "task": tid, "reason_code": code,
        "detail": "MG %s: %s" % (d["branch"], "; ".join(d["reasons"])[:400]),
        "gate_branch": d["branch"], "gate_need": d["need"],
        "gate_stats": d["stats"],
        # 一句给人读的判定说明。落在链上才回答得了
        # "三个月后有人问这个任务为什么没合进去"。
        "gate_describe": mg_gate.describe(d),
    }
    seq = None
    if wait_sign:
        if not dry_run:
            record({"task": tid, "event": "gate_wait", "branch": d["branch"],
                    "need": d.get("need"), "have": d.get("approvals") or []})
        return None
    if not dry_run:
        e = st.append_event("escalate", payload, "system:merge_queue", tid)
        seq = e["seq"]
        # 光写事件不够：任务状态还停在 done，而 done 不会被再 pick，
        # 于是"要人重拆"这件事永远躺在链上没人处理。
        # 迁移到 escalated（T14）才真正把任务推出自动流程。
        try:
            st.transition(tid, "escalated", "system:merge_queue", reason_event=seq)
        except wb_core.SpecViolation as exc:
            # 迁移不成时留痕：事件写了、状态没动的半完成态必须看得见。
            sys.stderr.write("WARN %s: escalate written but state not moved (%s)\n"
                             % (tid, exc))
        record({"task": tid, "event": "gate_blocked", "seq": seq,
                "branch": d["branch"], "reasons": d["reasons"][:4]})
    return seq


def record(rec):
    os.makedirs(os.path.dirname(MERGE_LOG), exist_ok=True)
    with open(MERGE_LOG, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def run_smoke(cmd, cwd):
    """跑主干冒烟。

    `{py}` 占位符换成当前解释器的**绝对路径**。理由：config 里写死
    `python ...` 依赖 PATH，而控制面起子进程时的 PATH 与交互式 shell 的
    PATH 不是同一个东西 —— 实测就因此把一条完全正确的冒烟判成了失败。
    一个"配置里看起来对、运行时解析不到"的命令，比没有配置更坏：
    它让每个合并都回滚，而回滚日志说"冒烟失败"，指向错误的方向。
    """
    if not cmd:
        return True, "no smoke configured"
    cmd = cmd.replace("{py}", '"%s"' % sys.executable)
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                       encoding="utf-8", errors="replace", timeout=600)
    tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-6:]
    return p.returncode == 0, "\n".join(tail) or ("exit %d" % p.returncode)


def dirty_paths():
    """工作区里未提交的路径（不含未跟踪的临时文件之外的东西也一并列出）。

    这个函数存在的唯一理由是 `merge` 与"人正在改的东西"共用一棵工作树。
    没有它，`revert_to` 用 `git reset --hard` 回滚一次合并，会把控制面
    自己的未提交改动一起抹掉 —— 实测就发生过：给 `merge_queue.py` 打的
    补丁被它自己触发的一次回滚清空了，而日志只写"冒烟失败，已回滚"。
    **回滚的边界必须等于这次合并的边界。**
    """
    p = git("status", "--porcelain", "--untracked-files=no")
    out = []
    for line in (p.stdout or "").splitlines():
        if len(line) > 3:
            out.append(line[3:].strip().strip('"'))
    return out


def touched_from_merge(merge_commit):
    """从合并提交的**第一父**反推本次合并真正触碰的路径。

    第四十轮：这是第三十九轮「边界未知就停下来」之后补上的可证明兜底。
    ``result.json`` 里的 changed_files 是执行体自己报的，可能被漏写、被改；
    而 ``git diff --name-only <merge>^1 <merge>`` 问的是 git 对象本身 ——
    第一父就是合并前的主干，两者之差就是这次合并**实际落地**的边界。
    它不依赖 reflog（会被 checkout/gc 改写），也不依赖人的自述。

    返回 ``(files, note)``；note 把来源 commit 与路径数写清楚，调用方把它
    塞进 detail，值班的人照着就能复现一次 diff。失败时返回 ``([], 原因)``，
    绝不静默当「没有触碰任何文件」。
    """
    if not merge_commit:
        return [], "no merge_commit supplied"
    first_parent = "%s^1" % merge_commit
    p = git("diff", "--name-only", first_parent, merge_commit)
    if p.returncode != 0:
        return [], "git diff %s %s rc=%d: %s" % (
            first_parent, merge_commit[:12], p.returncode,
            (p.stderr or "").strip()[:200])
    files = sorted(set(
        line.strip().strip('"') for line in (p.stdout or "").splitlines()
        if line.strip()))
    note = "fallback=merge_commit@%s^1 diff %d path(s) [%s]" % (
        merge_commit[:12], len(files), ", ".join(files[:5]))
    return files, note


def revert_to(st, tid, base, result, touched=None, merge_commit=""):
    """把主干退回到 base，并**验证**退回成功。返回 ``(ok, detail)``。

    只有一条路：``git update-ref`` 挪分支指针 + 只恢复本次触碰的路径。
    它**只动这次合并碰过的文件**，不碰工作区里别人未提交的改动。

    边界有两个来源，取并集（第四十轮）：
      · ``touched`` —— 调用点从 ``changed_of(tid)`` / ``diff_stats`` 汇总；
      · ``merge_commit`` 的第一父 diff —— 可证明的兜底，见
        :func:`touched_from_merge`。
    两者都拿不到时才 ``REVERT_FAILED`` 停下来。把两个来源各贡献多少写进
    detail，是为了让「回滚了哪些路径」这件事可复核，而不是一句「已回滚」。

    第三十九轮删掉了原来的路径 2/3：
      · 路径 2 ``git merge --abort`` + ``git reset --hard base`` —— 在受管
        环境（CC Safety Net 的 git.reset-hard 规则）**必然被拦**，于是它
        「必然静默失败」；而在没被拦的环境里它会把控制面自己未提交的
        脚本/config/manifest 一起抹掉。两种结局都不能接受。
      · 路径 3 ``git checkout -B <branch> base`` —— 抹掉 reflog，且同样
        越过本次合并的边界。

    留着它们的理由是「万一路径 1 失败还能兜底」，但兜底的代价是毁掉
    不相干的工作，且失败时日志只写「已回滚」。回滚的边界必须等于本次
    合并的边界；连 git 对象都问不出边界时，正确的动作是停下来并写
    ``REVERT_FAILED``，由调用点写 escalate 交给人。
    """
    branch = result.get("branch") or def_branch()

    def head_is_base():
        return (rev("HEAD") or "") == base

    def path_in_base(f):
        return git("cat-file", "-e", "%s:%s" % (base, f)).returncode == 0

    def restore_one(f):
        """把单个路径恢复成 base 的样子。base 里没有这个文件就**删掉**它。

        上一版只做 `git checkout base -- f` + `git reset base -- f`，于是：
        合并**新增**的文件（base 里根本没有）在回滚后既不在索引里、也还在磁盘上，
        变成一个未跟踪文件。HEAD 确实回到了 base，回滚报告写着「kept 0 unrelated
        path(s)」—— 全部绿灯。而下一轮合并会撞上
        `error: The following untracked working tree files would be overwritten by merge`
        然后被归因成 BASE_COMMIT_STALE（"基线过期"），指向完全错误的方向。
        回滚的判据不能只看 HEAD：**合并进来的东西全都不在了，才算回滚。**
        """
        if path_in_base(f):
            git("checkout", base, "--", f)
            git("reset", "-q", base, "--", f)
            return "restored"
        p = os.path.join(ROOT, *f.split("/"))
        if os.path.isfile(p):
            os.remove(p)
            return "deleted"
        return "absent"

    def leftovers(files):
        """还没被清理干净的路径（索引里有差异，或磁盘上还留着）。"""
        out = []
        for f in files:
            if git("status", "--porcelain", "--", f).stdout.strip():
                out.append(f)
        return out

    tried = []
    # 边界来源一：调用点汇总的 touched（changed_of / diff_stats）。
    explicit = sorted(set(touched or []))
    # 边界来源二（第四十轮）：从合并提交第一父反推，结果写进 detail 才可证明。
    fallback, fb_note = touched_from_merge(merge_commit)
    tried.append(fb_note)
    boundary = sorted(set(explicit) | set(fallback))
    tried.append("boundary: explicit=%d fallback=%d union=%d"
                 % (len(explicit), len(fallback), len(boundary)))
    # 两个来源都拿不到就没有边界。旧版在这里退到 reset --hard / checkout -B，
    # 两者都会越过边界（详见 docstring）；现在直接停止并把原因写进 detail。
    if not boundary:
        return False, "; ".join(tried) + (
            "; REVERT_FAILED: touched boundary is empty and merge_commit parent "
            "diff yielded nothing; refusing whole-tree reset (边界未知就停下来)")
    touched = boundary
    # 边界来源三（R63）：工作区里**不属于本次合并**的未提交改动。
    # 必须在回滚前取快照 —— 回滚之后工作区已被恢复，这里会算成空集，
    # 越界永远看不见（"判据存在但永真"的典型形态）。
    outside = sorted(set(dirty_paths()) - set(touched))
    tried.append("outside_dirty=%d" % len(outside))

    cur = rev("HEAD") or ""
    # 1a. 指针 CAS 回 base：HEAD~ 的父提交位置不变，只是分支不再指向合并
    p = git("update-ref", "refs/heads/%s" % branch, base, cur)
    tried.append("update-ref rc=%d" % p.returncode)
    if p.returncode != 0:
        return False, "; ".join(tried) + "; REVERT_FAILED: update-ref refused; HEAD=%s" % (
            (rev("HEAD") or "?")[:12])
    # 1b. 逐路径恢复：base 有的 checkout 回来，base 没有的（本次新增的）删掉
    acts = {}
    for f in touched:
        a = restore_one(f)
        acts[a] = acts.get(a, 0) + 1
    # 1c. 索引回到 base（不动工作区里的其他未提交文件）
    git("reset", "-q", base, "--", *touched)
    summary = ", ".join("%s=%d" % (k, v) for k, v in sorted(acts.items()))
    left = leftovers(touched)
    if head_is_base() and not left:
        still = [f for f in outside
                 if git("status", "--porcelain", "--", f).stdout.strip()]
        if len(still) != len(outside):
            lost = [f for f in outside if f not in still]
            return False, "; ".join(tried) + (
                "; REVERT_FAILED: 回滚越过边界，抹掉了 %d 个不属于本次合并的"
                "未提交改动: %s" % (len(lost), ", ".join(lost[:5])))
        return True, "; ".join(tried) + (
            "; touched %d path(s) (%s); residual=0; kept %d unrelated path(s)"
            % (len(touched), summary, len(still)))
    if head_is_base():
        # 指针回去了但工作区没干净 —— 不能报成功。
        tried.append("RESIDUAL %d path(s): %s" % (len(left), ", ".join(left[:5])))
    return False, "; ".join(tried) + (
        "; REVERT_FAILED: head=%s base=%s residual=%d; 主干仍停在坏状态，已写 escalate 交人处理"
        % ((rev("HEAD") or "?")[:12], base[:12], len(left)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true",
                    help="打过程行（队列标题/冒烟尾巴/合计）；结果行一直打")
    ap.add_argument("--branch", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--smoke", default="")
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args(argv)
    VERBOSE[0] = bool(a.verbose or a.dry_run)

    a.branch = a.branch or def_branch()
    if rev(a.branch) is None:
        print("no branch %r" % a.branch)
        return 2
    st = store()
    c = cfg()
    smoke_cmd = a.smoke or ((c.get("mg") or {}).get("smoke_cmd") or "")

    pending = [t for t in done_tasks(st) if rev("task/%s" % t)][: a.limit]
    result = {"branch": a.branch, "merged": [], "rejected": [], "skipped": [],
              "gated": [], "frozen": False, "smoke": smoke_cmd or None,
              "gate_bases": []}

    mg = c.get("mg") or {}
    gray_seen = 0
    merged_any = False
    for tid in pending:
        head = rev("task/%s" % tid)
        base = rev(a.branch)
        if head is None:
            continue
        # 闸门基线是「主干与任务的分叉点」，不是主干 tip。base 是**合并目标**
        # （merge-tree / git merge 把任务并到它上面），两件事不能混：
        # 拿主干 tip 与任务 head 做两点 diff，会把主干在分叉之后新增的文件
        # 读成「这条任务把它们删了」→ file_delete → NEVER_WAIVE 双签。
        # T-R40 真跑就是这么被挡住的：worktree 从旧 base 建，主干后来提交了
        # evidence/*，任务本身只加了一个 docs 文件。闸门要量的是「任务自己
        # 改了什么」，所以 diff 从 merge-base 量起；merge_tree_ok 仍用主干 tip。
        gb = resolve_gate_base(a.branch, head, base)
        gate_base = gb["base"]
        result["gate_bases"].append({"task": tid, "branch": a.branch,
                                     "head": head, "merge_target": base,
                                     "gate_base": gate_base,
                                     "source": gb["source"],
                                     "detail": gb["detail"]})
        if gb["source"] == "tip_fallback":
            warn("WARN merge_queue: %s 的 merge-base 失败，闸门退回主干 tip：%s"
                 % (tid, gb["detail"]))
        # 已经合过（head 是 base 的祖先）
        anc = git("merge-base", "--is-ancestor", head, base)
        if anc.returncode == 0:
            result["skipped"].append({"task": tid, "why": "already merged"})
            continue

        # ── MG 合并闸门（v3.2 §8.4）：先判策略，再算能否合并 ──────────
        # 顺序不能反：merge-tree 能算通只说明"技术上不冲突"，
        # 不说明"这次改动该不该自动进主干"。
        g = gate_of(tid, gate_base, head, mg, gray_seen,
                    approvals=approved_of(st, tid))
        if g["branch"] == "AUTO_CORE":
            gray_seen += 1
        if not g["merge"]:
            seq = gate_reject(st, tid, g, a.dry_run)
            result["gated"].append({"task": tid, "branch": g["branch"],
                                    "need": g["need"], "reasons": g["reasons"],
                                    "stats": g["stats"], "seq": seq,
                                    "wait_sign": bool(g["need"] != "none"
                                                      and not g["need_met"])})
            continue
        result.setdefault("gate", []).append({"task": tid, "branch": g["branch"],
                                              "need": g["need"]})

        ok, info = merge_tree_ok(base, head)
        if not ok:
            files = info
            # §附录 F F14：merge-tree 预演出的冲突是「代码真冲突」，不是
            # 「基线取错」。BASE_COMMIT_STALE 是另一类原因，混用会让链上
            # 的 conflict_files 看起来像 stale，处置方向完全相反。
            result["rejected"].append({"task": tid, "reason_code": "MERGE_CONFLICT",
                                       "conflict_files": files})
            if not a.dry_run:
                e = st.append_event("verify", {
                    "task": tid, "result": "fail",
                    "reason_code": "MERGE_CONFLICT",
                    "acceptance_digest": None, "exit_code": None,
                    "evidence_path": "state/merge_queue.jsonl",
                    "conflict_files": files,
                }, "system:merge_queue", tid)
                # done → rejected 不是合法迁移；冲突任务必须先 reopen 再打回
                # 这里只记录 verify(fail)，由 retain/rebase 流程处理状态
                record({"task": tid, "event": "conflict", "seq": e["seq"], "files": files})
            continue

        if a.dry_run:
            result["merged"].append({"task": tid, "tree": info, "dry_run": True})
            continue

        # 真合：用 merge-tree 算出的树做一次 no-ff merge，保持审计痕迹。
        #
        # 提交者身份必须显式给。上一版没给，于是：
        #   · 在开发者本机 —— 恰好配了全局 user.name，能跑；
        #   · 在 CI / 临时克隆 / 容器 —— `Committer identity unknown`，
        #     合并失败，而失败被归成 `BASE_COMMIT_STALE`（"基线过期"）。
        # 一个"本机跑得通、别处跑不通"的失败，且错误码指向完全错误的方向，
        # 是最难查的一类。合并的提交者是控制面，不是人，所以固定身份。
        m = git("-c", "user.name=workbench-merge",
                "-c", "user.email=merge@workbench.local",
                "merge", "--no-ff", "-m", "merge: %s" % tid, head)
        if m.returncode != 0:
            err = ((m.stderr or "") + (m.stdout or "")).strip()
            git("merge", "--abort")
            # 分两类：真冲突 vs 环境原因。混在一起会让"仓库没配身份"
            # 看起来像"代码有冲突"，而这两件事的处置完全不同。
            env_bad = any(k in err for k in (
                "Committer identity unknown", "Please tell me who you are",
                "unable to auto-detect email", "Author identity unknown"))
            # 「未跟踪文件会被覆盖」也是环境问题，不是代码冲突、更不是基线过期。
            # 它的典型成因是上一次合并被回滚时，合并**新增**的文件没被删干净
            # （旧 revert_to 只 checkout base 里有的路径）。归成 BASE_COMMIT_STALE
            # 会让值班的人去 rebase 一个本来就没问题的分支。
            if "untracked working tree files would be overwritten" in err:
                env_bad = True
                err = err + "  ← 主干工作区有未跟踪残留（多半是上次回滚没清理新增文件）；" \
                            "删掉这些文件或让 revert_to 清理后重试。"
            if env_bad:
                result["rejected"].append({
                    "task": tid, "reason_code": "ENV_BROKEN",
                    "detail": err[:400],
                    "conflict_files": []})
                record({"task": tid, "event": "merge_env_broken", "detail": err[:200]})
                st.append_event("escalate", {
                    "task": tid, "reason_code": "ENV_BROKEN",
                    "detail": "git identity missing for merge: %s" % err[:300],
                }, "system:merge_queue", tid)
            else:
                result["rejected"].append({"task": tid, "reason_code": "BASE_COMMIT_STALE",
                                           "detail": err[:400],
                                           "conflict_files": changed_of(tid)})
                record({"task": tid, "event": "merge_failed"})
            continue

        merge_commit = git("rev-parse", "HEAD").stdout.strip()
        e = st.append_event("merge", {"task": tid, "merge_commit": merge_commit,
                                      "head": head, "into": a.branch,
                                      "tree": info}, "system:merge_queue", tid)
        result["merged"].append({"task": tid, "seq": e["seq"], "tree": info})
        record({"task": tid, "event": "merged", "seq": e["seq"]})
        merged_any = True

        # 冒烟：每条合并后都跑，跑挂就冻结并回滚
        ok2, tail = run_smoke(smoke_cmd, ROOT)
        if not ok2:
            result["frozen"] = True
            result["smoke_tail"] = tail
            # 回滚必须**验证**，不能"发个命令就当成了"：全失败时把
            # revert_ok=false 写进事件，并由下面的分支补一条 escalate。
            # 第三十九轮删掉了 reset --hard / checkout -B 假兜底；第四十轮给
            # revert_to 补了可证明兜底：除了调用点汇总的 touched，再传本次
            # merge_commit，让它从第一父 diff 反推实际落地边界（result.json
            # 是执行体自述，可能漏写）。两处都拿不到才 REVERT_FAILED。
            _st_map, _add, _del = diff_stats(base, rev('task/%s' % tid) or head)
            revert_ok, revert_detail = revert_to(
                st, tid, base, result,
                touched=sorted(set(changed_of(tid)) | set(_st_map)),
                merge_commit=merge_commit)
            se = st.append_event("merge_reverted", {
                "tasks": [tid], "merge_commit": merge_commit, "reason": "smoke_failed",
                "reverted_to": base, "revert_ok": revert_ok,
                "revert_detail": revert_detail, "smoke_tail": tail[:2000],
            }, "system:merge_queue", tid)
            result["reverted"] = {"seq": se["seq"], "to": base, "ok": revert_ok}
            if not revert_ok:
                # 回滚失败必须升级到人，不能只是链上多一个 false。
                result["revert_failed"] = True
                st.append_event("escalate", {
                    "task": tid, "reason_code": "ENV_BROKEN",
                    "detail": "revert to %s failed: %s" % (base[:12], revert_detail),
                }, "system:merge_queue", tid)
            # 受影响任务：改动文件与失败任务有交集的全部 escalate
            bad = set(changed_of(tid))
            affected = []
            for other in done_tasks(st):
                if other == tid:
                    continue
                if bad & set(changed_of(other)):
                    affected.append(other)
            for other in affected:
                ee = st.append_event("escalate", {
                    "task": other, "reason_code": "ENV_BROKEN",
                    "detail": "smoke failed after merging %s" % tid,
                    "suspect_files": sorted(bad),
                }, "system:merge_queue", other)
                result.setdefault("escalated", []).append(
                    {"task": other, "seq": ee["seq"]})
            break

    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        log("merge_queue → %s (dry_run=%s)" % (a.branch, a.dry_run))
        for m in result["merged"]:
            print("  MERGED   %s  tree=%s" % (m["task"], (m.get("tree") or "")[:12]))
        for r in result["rejected"]:
            print("  REJECTED %s  %s  %s" % (r["task"], r["reason_code"],
                                             ",".join(r.get("conflict_files") or [])[:80]))
        for s in result["skipped"]:
            print("  SKIP     %s  — %s" % (s["task"], s["why"]))
        for g in result.get("gated", []):
            # "W AIT" 与 "GATED" 必须能一眼分开：前者是"补个签名就能合"，
            # 后者是"要人重拆"。同一个词会让值班的人对两种情况做同一个动作
            # —— 而补签名对 NEEDS_REBREAK 完全无效。
            tag = "WAIT" if g.get("wait_sign") else "GATED"
            print("  %-8s %s  %-13s %-11s %s"
                  % (tag, g["task"], g["branch"], g["need"],
                     "; ".join(g["reasons"])[:70]))
        if result["frozen"]:
            rv = result.get("reverted") or {}
            print("  FROZEN   smoke failed → reverted to %s (ok=%s)"
                  % ((rv.get("to") or "?")[:12], rv.get("ok")))
            log("           tail: %s" % result.get("smoke_tail", "")[:300].replace("\n", " | "))
            if result.get("revert_failed"):
                print("           REVERT FAILED —— 已写 escalate，主干仍停在坏状态")
        log("merged %d / rejected %d / gated %d / skipped %d"
              % (len(result["merged"]), len(result["rejected"]),
                 len(result.get("gated", [])), len(result["skipped"])))
    bad = (result["rejected"] or result["frozen"] or result.get("gated"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
