#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_boss_seat.py —— 主理人工位（seat）与对外汇报渲染层的防回归。

## 为什么这四件事必须一起测

第三十七轮把「主理人」落成三样互相咬合的东西：

    身份（persona/core.md）
      + 行为契约（persona/conduct.md，从豆包 v2 提示词 §0–§14 提炼）
      + 对外汇报渲染层（scripts/boss_report.py）
      + 一个声明位（roles/agents.json 的 roles.lead.seat）

四者只做其三，剩下那件就会退化成装饰：

  · 有契约没注入 → brief 里读不到，模型行为不变（本仓已犯过：神魂注入写了
    三周没人验，直到 test_brief_persona 补上）。
  · 有渲染层没接线 → run_task 还是只打搭建者日志。
  · 有声明没读者 → `seat` 变成一张写着"我们安排好了"的纸条（K11 就是为这个
    加的，但 K11 只验文件在，不验"真被注入"，所以注入这件事必须由测试兜）。

所以本文件四组断言缺一不可。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

import agent_runner as ar          # noqa: E402
import boss_report as br           # noqa: E402
import persona_loader as pl        # noqa: E402
from _memiso import TempMemCopy    # noqa: E402

fails = []
oks = []


def ok(name, cond, got=None):
    if cond:
        oks.append(name)
        print("ok  %s" % name)
    else:
        fails.append(name)
        print("FAIL %s%s" % (name, "" if got is None else "  got=%r" % (got,)))


def _spec():
    with io.open(os.path.join(WB, "roles", "agents.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _brief(tid, role):
    """真跑 write_brief 造一份 brief，返回正文。

    注意 write_brief 的返回值是**文件路径**不是正文（第三十七轮这里踩过一次：
    把路径当正文，`"## 神魂" in txt` 恒假，看起来像"契约没注入"，实际是
    "读错了东西"）。所以这里必须再读一次盘。

    ★ 第一百四十三轮：这里原来只换 `ar.TASKS_DIR`，**没换记忆根**。
      而 `write_brief` 会走 `assemble_report → build_recall → memory_store.recall()`，
      后者在返回前回写被召回条目的 `last_recall_ms` / `recall_count` / 升一档 `status`
      （`memory_store.py:854-857`）。于是每跑一次本文件就改一次真记忆 ——
      被新加的 `prod_drift_guard` 抓到（`M memory/project/... 503 -> 503`，
      大小不变内容变）。用 `TempMemCopy` 把记忆指到副本：brief 与记忆双双隔离，
      而「召回真的有内容」那几条断言照旧成立。
    """
    saved = ar.TASKS_DIR
    tmp = tempfile.mkdtemp()
    iso = TempMemCopy(tag="bossseat_mem")
    try:
        ar.TASKS_DIR = tmp
        path = ar.write_brief(tid, role, _spec(), tmp, "HEAD", "工位探针")
        with io.open(path, encoding="utf-8") as fh:
            return fh.read()
    finally:
        iso.restore()
        ar.TASKS_DIR = saved
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- 1 契约注入
def t_conduct_injected():
    txt = _brief("T-SEAT", "lead")
    ok("brief 里有行为契约段", "## 行为契约" in txt)
    ok("契约排在神魂之后", 0 <= txt.find("## 神魂") < txt.find("## 行为契约"))
    ok("契约排在角色卡之前", 0 <= txt.find("## 行为契约") < txt.find("## 角色卡"))
    # 抽一条契约里的硬规则，验证真读到了文件而不是只有标题
    ok("契约正文真的进来了", "未接入" in txt and "不写 0" in txt.replace("**", ""))
    ok("称呼 BOSS 的规矩仍在神魂里", "BOSS" in txt)


def t_conduct_only_for_external_roles():
    lead_txt = _brief("T-SEAT", "lead")
    coder_txt = _brief("T-SEAT", "coder")
    ok("lead 的 brief 有契约", "## 行为契约" in lead_txt)
    ok("coder 的 brief 没有契约（它不对 BOSS 说话）",
       "## 行为契约" not in coder_txt)
    # 神魂是所有角色都该有的，不能被顺手一起滤掉
    ok("coder 仍带神魂", "## 神魂" in coder_txt)


def t_conduct_missing_is_loud():
    tmp = tempfile.mkdtemp()
    saved = (pl.PERSONA_DIR, pl.CORE_FILE, pl.CONDUCT_FILE)
    try:
        pl.PERSONA_DIR = os.path.join(tmp, "persona")
        pl.CORE_FILE = os.path.join(pl.PERSONA_DIR, "core.md")
        pl.CONDUCT_FILE = os.path.join(pl.PERSONA_DIR, "conduct.md")
        os.makedirs(pl.PERSONA_DIR, exist_ok=True)
        with io.open(pl.CORE_FILE, "w", encoding="utf-8") as fh:
            fh.write("# core\n")
        ok("契约文件缺失时返回空串（不编造）", pl.build_conduct("lead") == "")
        ok("契约文件缺失时 brief 仍能生成（不阻断派发）",
           len(pl.assemble("lead")) >= 0)
    finally:
        pl.PERSONA_DIR, pl.CORE_FILE, pl.CONDUCT_FILE = saved
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- 2 汇报渲染
def t_render_from_chain():
    """用生产链上的三条真实任务验三态，而不是造 fixture。

    理由：渲染层的唯一职责是"把链读对"。造一个理想链来测，测的是我对链的
    想象；读真链，测的是它面对真实字段缺失（`executor=""`、`pid_observable`
    为 None 或 False）时还说不说得出人话。
    """
    import sign_manifest
    import wb_core
    wb_core.load_frozen(WB)
    st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())

    merged = br.render(st, "T-0008")
    ok("已合入任务是 MERGED", merged["verdict"] == "MERGED", merged["verdict"])
    ok("结论段非空", bool(merged["headline"]))
    ok("依据条数 >= 4（转正/派活/改动/验收/合入）", len(merged["evidence"]) >= 4,
       len(merged["evidence"]))
    # 第四十九轮：依据段多了一条「数字从哪来」（溯源摘要）。它的定位不是
    # seq/commit —— 锁台账是进程级的，样本行不带 task 字段，编一个 seq 出来
    # 才是造假。所以判据按**来源**分开：链上来的必须带 seq 或 commit，
    # 台账来的必须点名 criterion。放宽成「有定位就行」会把这条尺子做成摆设。
    _chain_ev = [e for e in merged["evidence"]
                 if e["what"] != br.PROVENANCE_WHAT]
    _prov_ev = [e for e in merged["evidence"]
                if e["what"] == br.PROVENANCE_WHAT]
    ok("链上来的每条依据都带 seq 或 commit",
       all(("seq=" in e["value"]) or ("commit=" in e["value"])
           for e in _chain_ev),
       [e["value"] for e in _chain_ev])
    ok("溯源条目存在且点名 criterion 便于复核",
       len(_prov_ev) == 1 and "lock_sample_provenance_v1" in _prov_ev[0]["value"],
       [e["value"] for e in _prov_ev])
    ok("溯源条目不冒充本任务专属（必须自称全仓）",
       bool(_prov_ev) and "全仓" in _prov_ev[0]["what"],
       _prov_ev and _prov_ev[0]["what"])
    ok("落点带短哈希", "master@" in merged["landed"], merged["landed"])
    ok("风险段永不为空", bool(merged["risks"]))

    ghost = br.render(st, "T-NOT-EXIST")
    ok("不存在的任务给 NOT_STARTED", ghost["verdict"] == "NOT_STARTED",
       ghost["verdict"])
    ok("不存在时也给出风险说明", len(ghost["risks"]) >= 1)

    txt = br.text(merged)
    for sec in ("§ 结论", "§ 依据", "§ 风险", "§ 下一步"):
        ok("人读形态含 %s" % sec, sec in txt)


def t_render_is_readonly():
    import sign_manifest
    import wb_core
    wb_core.load_frozen(WB)
    st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
    before = len(list(st._rows()))
    br.render(st, "T-0008")
    br.text(br.render(st, "T-0008"))
    ok("渲染不写链", len(list(st._rows())) == before)


def t_short_sha_keeps_algo():
    ok("sha256 前缀保留", br._short_sha("sha256:" + "a" * 64).startswith("sha256:"))
    ok("sha256 被截短", len(br._short_sha("sha256:" + "a" * 64)) == len("sha256:") + 8)
    ok("空白不进报告", br._short_sha("") == "")
    ok("git sha 截 7 位", br._short_sha("1e61918c80e8") == "1e61918")


# --------------------------------------------------------------- 3 seat 声明
def t_seat_declared():
    spec = _spec()
    seat = ((spec.get("roles") or {}).get("lead") or {}).get("seat")
    ok("lead 有 seat 块", isinstance(seat, dict))
    ok("seat.title 是主理人", (seat or {}).get("title") == "主理人")
    ok("seat 指向 conduct", (seat or {}).get("conduct") == "persona/conduct.md")
    for k, rel in (("soul", "persona/core.md"),
                   ("conduct", "persona/conduct.md"),
                   ("report", ".workbench/scripts/boss_report.py")):
        ok("seat.%s 指向的文件真在（%s）" % (k, rel),
           os.path.isfile(os.path.join(ROOT, rel)))
    ok("主理人不是新角色位（roles 里没有第六个）",
       len((spec.get("roles") or {})) == 5, sorted((spec.get("roles") or {})))
    ok("seat 没有抬高并发上限",
       ((spec.get("pool") or {}).get("max_parallel")) == 3,
       (spec.get("pool") or {}).get("max_parallel"))


def t_no_new_slot():
    """负向：确认 seat 不是靠 slots 实现的。加一个 slot 会静默抬高并发上限。"""
    spec = _spec()
    total = sum((r.get("slots") or 0) for n, r in (spec.get("roles") or {}).items()
                if n != "verifier")
    ok("非 verifier 的 slots 合计仍为 6（lead1+arch1+coder3+companion1）",
       total == 6, total)


# --------------------------------------------------------------- 4 源码真值
def t_source_truth():
    src = io.open(os.path.join(WB, "scripts", "boss_report.py"), encoding="utf-8").read()
    ok("渲染层用 _rows 读链", "st._rows()" in src)
    ok("渲染层不写事件（无 append_event）", "append_event" not in src)
    ok("风险段有兜底", "无已知风险" in src)
    ok("未知字段写未取值而不是 0", "未取值" in src)

    rt = io.open(os.path.join(ROOT, "run_task.py"), encoding="utf-8").read()
    ok("run_task 真调 boss_report", "import boss_report" in rt)
    ok("run_task 汇报前有分隔线（与搭建者日志分开）", '"=" * 66' in rt)
    ok("--no-report 开关存在", '"--no-report"' in rt)
    ok("渲染失败不吞（打印而不是静默）", "汇报渲染失败" in rt)

    pl_src = io.open(os.path.join(WB, "scripts", "persona_loader.py"),
                     encoding="utf-8").read()
    ok("persona_loader 有 conduct 段", '"conduct": "## 行为契约"' in pl_src)
    ok("contract 按角色过滤", "CONDUCT_ROLES" in pl_src)
    ok("conduct 排在 core 之后", pl_src.find('"core", "conduct"') > 0)


def t_seat_can_emit_mirror():
    """第三十九轮：seat.can_emit 是镜像，不是第二份真相。

    逐字相等（列表顺序也算）；`agents.json` 两边任一边被单改，K11 必须
    报『各说各话』。顺手把 spec 授权也复验一遍：13 个事件里 E26/E29 是
    spec 明确给 lead 的『授权未使用』，不能因为链上没调用点就摘掉。
    """
    spec = _spec()
    lead = (spec.get("roles") or {}).get("lead") or {}
    seat = lead.get("seat") or {}
    ce = list(lead.get("can_emit") or [])
    mirror = seat.get("can_emit")
    ok("seat 有 can_emit 镜像", isinstance(mirror, list), type(mirror).__name__)
    ok("镜像是逐字相等（顺序也算）", mirror == ce, (mirror, ce))
    ok("镜像不是独立随手写的短表", len(mirror or []) == len(ce) == 13,
       (len(mirror or []), len(ce)))
    ok("E26/E29 仍在镜像里（授权未使用 ≠ 声明大于权限）",
       "E26" in (mirror or []) and "E29" in (mirror or []))
    ok("seat 指向 triage", seat.get("triage") == "persona/triage.md")
    ok("triage 文件真在",
       os.path.isfile(os.path.join(ROOT, "persona", "triage.md")))

    ev = json.load(io.open(os.path.join(WB, "spec", "events.schema.json"),
                           encoding="utf-8"))
    by_id = {e["id"]: e for e in ev.get("events") or []}
    for eid in (mirror or []):
        decl = (by_id.get(eid) or {}).get("actors") or []
        ok("%s 在 spec 里授权给 lead" % eid,
           "lead" in decl or "*authority*" in decl, decl)

    cc = io.open(os.path.join(WB, "scripts", "constitution_check.py"),
                 encoding="utf-8").read()
    ok("K11 真读 seat.can_emit", '_seat.get("can_emit")' in cc)
    ok("K11 真与 lead.can_emit 对账", '_seat_ce != _lead_ce' in cc)
    ok("K11 查 triage 指向", 'seat.triage 应指向 persona/triage.md' in cc)
    ok("K11 把四个指向都查存在",
       '("soul", "conduct", "triage", "report")' in cc)


def main():
    t_conduct_injected()
    t_conduct_only_for_external_roles()
    t_conduct_missing_is_loud()
    t_render_from_chain()
    t_render_is_readonly()
    t_short_sha_keeps_algo()
    t_seat_declared()
    t_no_new_slot()
    t_seat_can_emit_mirror()
    t_source_truth()
    print("\ntest_boss_seat: %s (%d ok, %d failed)"
          % ("PASS" if not fails else "FAIL", len(oks), len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
