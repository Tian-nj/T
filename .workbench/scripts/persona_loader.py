#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""persona_loader.py — 识海神魂段拼装（W1 建骨架，W2/W3/W4 逐段接实）。

出场顺序（指南 §1.1，第三十七轮插入「行为契约」，第三十九轮插入「分诊」）：
  工程规则 → core → 行为契约 → 分诊 → 心绪 → 心念 → 显忆 → 隐忆 → 角色卡 → 任务 brief

本模块负责中间的七段（core / 行为契约 / 分诊 / 心绪 / 心念 / 显忆 / 隐忆）。工程规则与
角色卡由 `agent_runner.write_brief()` 自己持有，任务 brief 由调用方拼 —— 分段边界
必须和「谁负责写这段」对齐，否则同一条规则会在两处各写一遍然后慢慢说不到一起。

## 为什么「行为契约」是独立一段，而不压进角色卡

角色卡（`roles/<role>.md`）回答「这个工位能做什么、不能做什么」——它是**岗位说明**。
行为契约（`persona/conduct.md`）回答「做的时候怎么说话、怎么分诊、怎么披露」——
它是**风格与判断**。两者职责不同：把 style 塞进岗位说明，结果是以后想改语气
就得动权限文件，而权限文件在 `paths.yaml` 的 deny 里、改动要 E27。一件本该随手
微调的事被绑上了可信根，人就会干脆不改，然后风格描述慢慢变成装饰。

## 行为契约只给对外负责的角色
`CONDUCT_ROLES = ("lead",)`。理由不是保密，是**没有对外职责的角色读到它只会浪费
预算**：`coder` 的产出是 patch，`verifier` 的产出是 verify.log，它们不跟 BOSS 说话。
要不要扩这个集合，判断标准是「这个角色会不会直接对 BOSS 输出自然语言」。

三条硬约束（都是为了不让它撒谎）：
1. 任何一段读不到就返回空串 + stderr 告警，**不编造**、不静默降级；
   静默降级和"这段本来就没有"在输出上长得一样，而两者该差得很远。
2. 心绪段只给事实与强度，不给表演指令 —— "你要表现得开心"是台词，不是状态。
3. 超预算按 core > 行为契约 > 分诊 > 心绪 > 心念 > 显忆 > 隐忆 截断（即
   `SECTION_ORDER`）。顺序即优先级：先丢最容易被重新取回的那段（隐忆的原文
   在 archive 里躺着，core 丢了就没有第二份）。

CLI：
  python persona_loader.py sections            # 打印七段（给人看）
  python persona_loader.py sections --json     # 机读
  python persona_loader.py budget              # 打印当前注入预算
  python persona_loader.py selftest
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)

sys.path.insert(0, HERE)

PERSONA_DIR = os.path.join(os.path.dirname(WB), "persona")
CORE_FILE = os.path.join(PERSONA_DIR, "core.md")
GROWTH_FILE = os.path.join(PERSONA_DIR, "growth.md")
OWNER_FILE = os.path.join(PERSONA_DIR, "owner.md")
CONDUCT_FILE = os.path.join(PERSONA_DIR, "conduct.md")
TRIAGE_FILE = os.path.join(PERSONA_DIR, "triage.md")

DEFAULT_BUDGET = 3200
# 第五十轮：显忆段的预留额度。见 `recall_reserve()` 的说明 ——
# 不预留的话，lead 的静态四段已经吃掉 92% 预算，召回正文进不去，
# 表现为「接了召回和没接一样」。
DEFAULT_RECALL_RESERVE = 600
# 行为契约排在 core 之后、心绪之前：先有身份，再讲做事的方式，最后才是当下心情。
# 心绪是易变的，契约是稳定的 —— 稳定的东西被挤掉比易变的东西被挤掉损失大。
SECTION_ORDER = ("core", "conduct", "triage", "emotion", "purpose",
                 "recall", "implicit")
SECTION_TITLES = {
    "core": "## 神魂",
    "conduct": "## 行为契约",
    "triage": "## 分诊",
    "emotion": "## 心绪",
    "purpose": "## 心念",
    "recall": "## 显忆",
    "implicit": "## 隐忆",
}
# 只有会对 BOSS 直接输出自然语言的角色才注入行为契约。
CONDUCT_ROLES = ("lead",)


# --------------------------------------------------------------- 基础读取
def _cfg() -> dict:
    import wb_core
    p = os.path.join(WB, "config.yaml")
    try:
        return wb_core.yaml_parse_file(p)
    except Exception as exc:                                   # noqa: BLE001
        sys.stderr.write("WARN persona_loader 读 config 失败（%s）；用默认预算 %d\n"
                         % (exc, DEFAULT_BUDGET))
        return {}


def budget() -> int:
    """注入预算。读不到就回退默认值并告警 —— 不抛崩（brief 装配不能因为配置缺失全挂）。"""
    cfg = _cfg()
    node = ((cfg.get("persona") or {}).get("inject_max_chars"))
    if isinstance(node, int) and node > 0:
        return node
    if node is not None:
        sys.stderr.write("WARN persona.inject_max_chars=%r 不是正整数；回退 %d\n"
                         % (node, DEFAULT_BUDGET))
    else:
        sys.stderr.write("WARN config 缺 persona.inject_max_chars；回退 %d\n" % DEFAULT_BUDGET)
    return DEFAULT_BUDGET


def _read(path: str, label: str) -> str:
    if not os.path.exists(path):
        sys.stderr.write("WARN %s 不存在；本轮不注入该段\n" % label)
        return ""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError as exc:
        sys.stderr.write("WARN 读 %s 失败（%s）；本轮不注入该段\n" % (label, exc))
        return ""


# --------------------------------------------------------------- 七段构造
def build_core() -> str:
    """神魂内核。缺文件只告警不编造 —— 编一段"人格"比没有人格更坏。"""
    return _read(CORE_FILE, "persona/core.md")


def build_conduct(role: str = "") -> str:
    """行为契约（第三十七轮）。只对 CONDUCT_ROLES 注入；缺文件只告警不编造。

    为什么按角色过滤而不是全员注入：这段是「怎么对 BOSS 说话」。让 coder 读到
    它并不会让它说得更好 —— 它本来就不说话，只是白占预算。

    为什么不过滤时静默返回空串：`build_persona_sections` 会跳过空段，
    于是「这个角色不该有契约」和「契约文件丢了」在输出上就分不开了。
    所以丢文件那条路必须 WARN（由 `_read` 负责），不该有的角色那条路静默。
    """
    if role not in CONDUCT_ROLES:
        return ""
    return _read(CONDUCT_FILE, "persona/conduct.md")


def build_triage(role: str = "") -> str:
    """分诊（第三十九轮）：S/A/B 三级与代价上限。注入条件同行为契约。

    为什么单独一段而不并进行为契约：契约是「怎么说」，分诊是「多快」。
    写在同一节里，改语气就得连带改成本口径 —— 两件更新频率不同的事
    绑在一起，最后一定是一起烂掉。

    为什么不设新角色白名单：分诊只对会对 BOSS 输出自然语言的角色有意义，
    这个集合与 CONDUCT_ROLES 相同；各自维护一份会在下一次扩角色时漏一个。
    """
    if role not in CONDUCT_ROLES:
        return ""
    return _read(TRIAGE_FILE, "persona/triage.md")


def build_emotion() -> str:
    """心绪底色：只给事实与强度。emotion_engine 未就位（W1/W2）时返回空串。"""
    try:
        import emotion_engine
    except Exception:                                          # noqa: BLE001
        return ""
    try:
        return (emotion_engine.summarize() or "").strip()
    except Exception as exc:                                   # noqa: BLE001
        sys.stderr.write("WARN 心绪摘要失败（%s）；本轮不注入心绪段\n" % exc)
        return ""


def build_purpose() -> str:
    """心念 open 项。空则返回空串（没有惦记的事是正常状态，不是错误）。"""
    try:
        import purpose_store
    except Exception:                                          # noqa: BLE001
        return ""
    try:
        items = purpose_store.open_items()
    except Exception as exc:                                   # noqa: BLE001
        sys.stderr.write("WARN 心念读取失败（%s）；本轮不注入心念段\n" % exc)
        return ""
    if not items:
        return ""
    lines = ["未了结的惦记（按最后触碰时间排序）："]
    for it in items[:10]:
        lines.append("- [%s] %s（最后触碰 %s）"
                     % (it.get("id"), it.get("note"), _ago(it.get("last_touch_ms"))))
    return "\n".join(lines)


def build_recall(query: str = "", store=None, task: str | None = None) -> str:
    """显忆 Top-N。召回为空返回空串：没有相关记忆就说没有，不拿无关条目凑数。

    第五十轮：`store` 透传给 `memory_store.recall()`。不给 store 时召回照样
    返回结果（读的是记忆目录），但**不会写 `memory_recall` sidecar** ——
    于是「派活时到底想起来过什么」在台账里查不到，只有「有没有想起」的区别
    变成了「有没有调用」的区别。传了 store 才有那一行。

    第四十九轮：`task` 也透传。台账里不带任务号的话，那 21 行召回记录
    全都回答不了「哪次派活想起了什么」—— 而那正是这套记忆存在的理由。
    """
    if not query:
        return ""
    try:
        import memory_store
    except Exception:                                          # noqa: BLE001
        return ""
    try:
        hits = memory_store.recall(query, store=store, task=task)
    except Exception as exc:                                   # noqa: BLE001
        sys.stderr.write("WARN 记忆召回失败（%s）；本轮不注入显忆段\n" % exc)
        return ""
    if not hits:
        return ""
    lines = []
    for h in hits:
        lines.append("- " + memory_store.render_for_injection(h))
    return "\n".join(lines)


def build_implicit() -> str:
    """隐忆：归档条目的印象级回放。W4 才有 dream 的 replay，之前恒空。"""
    try:
        import dream_consolidation
    except Exception:                                          # noqa: BLE001
        return ""
    try:
        hits = dream_consolidation.replay_archive(top_n=1)
    except Exception as exc:                                   # noqa: BLE001
        sys.stderr.write("WARN 隐忆回放失败（%s）；本轮不注入隐忆段\n" % exc)
        return ""
    if not hits:
        return ""
    return "\n".join("- " + h.get("line", "") for h in hits if h.get("line"))


def _ago(ms) -> str:
    try:
        ms = int(ms or 0)
    except (TypeError, ValueError):
        return "时间未知"
    if ms <= 0:
        return "时间未知"
    import time
    days = max(0, (int(time.time() * 1000) - ms) // 86400000)
    if days == 0:
        return "今天"
    if days == 1:
        return "昨天"
    return "约 %d 天前" % days


def build_persona_sections(role: str = "", query: str = "", store=None,
                           task: str | None = None) -> list:
    """按 §1.1 顺序返回**已带标题**的分段列表；空段被跳过（调用方不必再判空）。"""
    bodies = {
        "core": build_core,
        "conduct": (lambda: build_conduct(role)),
        "triage": (lambda: build_triage(role)),
        "emotion": build_emotion,
        "purpose": build_purpose,
        "recall": (lambda: build_recall(query, store, task)),
        "implicit": build_implicit,
    }
    out = []
    for key in SECTION_ORDER:
        try:
            body = (bodies[key]() or "").strip()
        except Exception as exc:                               # noqa: BLE001
            sys.stderr.write("WARN 段 %s 构造失败（%s）；跳过\n" % (key, exc))
            continue
        if body:
            out.append(SECTION_TITLES[key] + "\n" + body)
    return out


def truncate_to_budget(sections: list, max_chars: int) -> list:
    """超预算按顺序（即优先级）保留前缀。

    刻意**不做**"截一半留半句"：半句人格比没有这段更糟 —— 它看起来像说过。
    要么整段进，要么整段不进。
    """
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = DEFAULT_BUDGET
    if max_chars <= 0:
        return []
    out, used = [], 0
    for s in sections:
        if not s:
            continue
        if used + len(s) <= max_chars:
            out.append(s)
            used += len(s)
        else:
            break
    return out


def recall_reserve() -> int:
    """显忆段的**预留**额度（字符）。

    第五十轮的真缺陷：显忆段在 `SECTION_ORDER` 里排倒数第二，`truncate_to_budget`
    是"整段进或整段不进"，而 lead 的静态段（神魂 1612 + 契约 1049 + 分诊 929
    + 心绪 86 = 3676）已经吃掉 4000 里的 92%。实测召回正文 330 字符 ——
    于是**即使 query 传进来了、召回也命中了，显忆段仍然整段进不去**。
    结果和"根本没接召回"完全一样：brief 里没有它。

    这里的修法不是把总预算调大（那是拆东墙），而是先划出一块给记忆：
    记忆是**每轮变化**的那部分，人格是**恒定**的；让恒定部分占满预算、
    再把易变部分挤掉，等于这个系统只记得自己是谁、不记得做过什么。
    """
    cfg = _cfg()
    node = ((cfg.get("persona") or {}).get("recall_reserve_chars"))
    if isinstance(node, int) and node >= 0:
        return node
    if node is not None:
        sys.stderr.write("WARN persona.recall_reserve_chars=%r 不是非负整数；回退 %d\n"
                         % (node, DEFAULT_RECALL_RESERVE))
    return DEFAULT_RECALL_RESERVE


def truncate_recall_lines(section: str, max_chars: int) -> str:
    """把显忆段按**行**截到额度内，并如实写出被裁掉几条。

    人格段用"整段进或整段不进"（半句人格比没有更糟）；记忆条目不同：
    `- 内容（适用于：场景）` 每一行自成一句，截掉第 6 条不影响前 5 条的可读性。
    所以这里按行截，并在末尾写一行「另有 N 条未列出」——
    不写这句，读的人会以为只召回到这几条。
    """
    lines = [ln for ln in (section or "").split("\n")]
    if not lines:
        return ""
    head, body = lines[0], [ln for ln in lines[1:] if ln.strip()]
    if max_chars <= 0 or not body:
        return section if max_chars > len(section) else ""
    kept, used = [], len(head)
    for ln in body:
        if used + len(ln) + 1 > max_chars:
            break
        kept.append(ln)
        used += len(ln) + 1
    if not kept:
        return ""
    dropped = len(body) - len(kept)
    if dropped:
        kept.append("（还有 %d 条相关记忆因预算未列出）" % dropped)
    return "\n".join([head] + kept)


def assemble(role: str = "", query: str = "", max_chars: int | None = None,
             store=None, task: str | None = None) -> str:
    """拼好的七段正文（已截断）。agent_runner 直接 append 这一段。"""
    return assemble_report(role=role, query=query, max_chars=max_chars,
                           store=store, task=task)["text"]


def assemble_report(role: str = "", query: str = "", max_chars: int | None = None,
                    store=None, reserve: int | None = None,
                    task: str | None = None) -> dict:
    """拼正文 + **报告哪几段被预算丢了**。

    为什么需要一个带报告版本：`truncate_to_budget` 是整段进或整段不进，
    所以「加了新段但没提预算」的后果不是正文变短一点，而是整段消失 ——
    第四十八轮分诊段就这样无声地没进过 brief，而所有测试都绿着。
    正文照旧返回（调用方不用改），但 `dropped` 必须能被打印出来：
    「注入了但没有」和「根本没注入」在 brief 里长得一模一样。

    第五十轮再加一层：显忆段先按 `recall_reserve_chars` **预留**额度。
    静态段用 `budget - reserve`，显忆段用 `reserve`。这样"记忆永远被挤掉"
    在结构上不可能再发生，而不是靠下一轮有人想起来调 budget。
    """
    secs = build_persona_sections(role=role, query=query, store=store, task=task)
    budget_chars = budget() if max_chars is None else max_chars
    reserve_chars = recall_reserve() if reserve is None else reserve
    # 预留不能吃掉全部：静态段至少要留下预算的 60%，否则人格被记忆挤没了，
    # 那是同一个错误的镜像。
    reserve_chars = max(0, min(int(reserve_chars), int(budget_chars) // 3))

    recall_title = SECTION_TITLES["recall"]
    recall_secs = [s for s in secs if s.split("\n", 1)[0] == recall_title]
    static_secs = [s for s in secs if s.split("\n", 1)[0] != recall_title]
    static_budget = max(0, int(budget_chars) - reserve_chars)

    kept = truncate_to_budget(static_secs, static_budget)
    recall_text = ""
    if recall_secs:
        recall_text = truncate_recall_lines(recall_secs[0], reserve_chars)

    # 显忆段插回它本来的位置（SECTION_ORDER 里在心绪之后、隐忆之前），
    # 不能一律追加到末尾 —— 段序本身是"先身份后际遇"的表达。
    if recall_text:
        idx = SECTION_ORDER.index("recall")
        before_titles = {SECTION_TITLES[k] for k in SECTION_ORDER[:idx]}
        pos = len(kept)
        for i, s in enumerate(kept):
            if s.split("\n", 1)[0] not in before_titles:
                pos = i
                break
        kept = kept[:pos] + [recall_text] + kept[pos:]

    kept_titles = [s.split("\n", 1)[0] for s in kept]
    dropped = [{"title": s.split("\n", 1)[0], "chars": len(s)}
               for s in secs if s.split("\n", 1)[0] not in kept_titles]

    # 第五十轮：预留挤掉人格必须报出来，不能静默。
    # 实测踩到过：budget 4000、reserve 600 → static_budget 3400，
    # 而静态四段合计 3676 → 分诊段（929 字符）被整段丢掉。
    # 那不是"记忆更重要"，那是配置装不下：正确动作是把 inject_max_chars
    # 调够，而不是让某一段无声消失。这里只负责让它在报告里可见。
    static_titles = [s.split("\n", 1)[0] for s in static_secs]
    starved = [t for t in static_titles if t not in kept_titles]
    reserve_starved = bool(starved) and sum(len(s) for s in static_secs) <= budget_chars

    return {
        "text": "\n\n".join(kept),
        "role": role,
        "query": query,
        "budget": budget_chars,
        "reserve": reserve_chars,
        "sections": kept_titles,
        "dropped": dropped,
        "used_chars": sum(len(s) for s in kept),
        # 预算够装下全部静态段，但预留把它挤掉了 —— 这是配置问题，不是取舍。
        "reserve_starved_static": reserve_starved,
        # recall 段有内容 = 本轮真的召回并注入了记忆。它单独列出来是因为
        # 「显忆为空」有两个完全不同的原因：没召回到，或者召回到了但被预算丢掉。
        "recall_injected": recall_title in kept_titles,
    }


# --------------------------------------------------------------- CLI
def _selftest() -> int:
    import tempfile
    ok, bad = 0, []

    def check(name, cond):
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append(name)

    tmp = tempfile.mkdtemp(prefix="persona_selftest_")
    global PERSONA_DIR, CORE_FILE, GROWTH_FILE, OWNER_FILE, CONDUCT_FILE, TRIAGE_FILE
    saved = (PERSONA_DIR, CORE_FILE, GROWTH_FILE, OWNER_FILE, CONDUCT_FILE,
             TRIAGE_FILE)
    try:
        PERSONA_DIR = os.path.join(tmp, "persona")
        CORE_FILE = os.path.join(PERSONA_DIR, "core.md")
        CONDUCT_FILE = os.path.join(PERSONA_DIR, "conduct.md")
        TRIAGE_FILE = os.path.join(PERSONA_DIR, "triage.md")
        os.makedirs(PERSONA_DIR, exist_ok=True)

        check("缺 core 时返回空串", build_core() == "")
        check("七段构造不抛异常", isinstance(build_persona_sections(), list))
        check("缺 conduct 时 lead 拿空串", build_conduct("lead") == "")
        check("缺 triage 时 lead 拿空串", build_triage("lead") == "")
        check("非对外角色不注入 conduct", build_conduct("coder") == "")
        check("非对外角色不注入 triage", build_triage("coder") == "")

        with open(CORE_FILE, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# 神魂内核 core\n\n## 称谓\n- 称呼主人为 **BOSS**\n")
        with open(CONDUCT_FILE, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# 行为契约 conduct\n\n不要写 0。\n")
        with open(TRIAGE_FILE, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# 分诊 triage\n\nS 级不跑工具。\n")
        core = build_core()
        check("core 读得到", "BOSS" in core)
        check("conduct 读得到", "不要写 0" in build_conduct("lead"))
        check("triage 读得到", "S 级不跑工具" in build_triage("lead"))
        secs = build_persona_sections("coder")
        check("core 段带标题", any(s.startswith("## 神魂") for s in secs))
        check("coder 的段里没有契约", not any(s.startswith("## 行为契约") for s in secs))
        check("coder 的段里没有分诊", not any(s.startswith("## 分诊") for s in secs))
        secs_lead = build_persona_sections("lead")
        check("lead 的段里有契约", any(s.startswith("## 行为契约") for s in secs_lead))
        check("lead 的段里有分诊", any(s.startswith("## 分诊") for s in secs_lead))
        check("契约紧跟在神魂之后",
              [s.split("\n")[0] for s in secs_lead][:2] == ["## 神魂", "## 行为契约"])
        check("分诊紧跟在契约之后（稳定→次稳定→易变）",
              [s.split("\n")[0] for s in secs_lead][:3]
              == ["## 神魂", "## 行为契约", "## 分诊"])

        many = ["A" * 100, "B" * 100, "C" * 100]
        check("预算内全留", len(truncate_to_budget(many, 1000)) == 3)
        check("预算不足按优先级前缀", truncate_to_budget(many, 250) == ["A" * 100, "B" * 100])
        check("预算 0 返回空", truncate_to_budget(many, 0) == [])
        check("不截半段", all(len(s) == 100 for s in truncate_to_budget(many, 250)))
        check("assemble 不抛异常", isinstance(assemble("coder", "", 500), str))
        check("_ago(0) 不炸", _ago(0) == "时间未知")
        check("_ago(None) 不炸", _ago(None) == "时间未知")
    finally:
        PERSONA_DIR, CORE_FILE, GROWTH_FILE, OWNER_FILE, CONDUCT_FILE, \
            TRIAGE_FILE = saved
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    if bad:
        print("persona_loader: FAIL (%d/%d)" % (ok, ok + len(bad)))
        for b in bad:
            print("  - " + b)
        return 1
    print("persona_loader: PASS (%d/%d)" % (ok, ok))
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="识海神魂段拼装")
    ap.add_argument("cmd", nargs="?", default="sections",
                    choices=["sections", "budget", "selftest"])
    ap.add_argument("--role", default="")
    ap.add_argument("--query", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return _selftest()
    if a.cmd == "budget":
        print(budget())
        return 0
    secs = build_persona_sections(role=a.role, query=a.query)
    if a.json:
        print(json.dumps({"budget": budget(), "sections": secs,
                          "lengths": [len(s) for s in secs]},
                         ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(secs) if secs else "（七段皆空：persona 未初始化）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
