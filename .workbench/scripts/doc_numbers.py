#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc_numbers.py — 文档里的数字必须能被机器核，而不是靠人记得同步。

## 为什么有这个文件

这个仓在 **四轮**里反复栽在同一件事上：

| 轮次 | 出问题的数字 | 怎么发现的 |
|---|---|---|
| 第十二轮 | `10873` 行（实际 10874） | 我手工 `wc -l` 才发现 |
| 第十四轮 | `11611` → `11693` → `11699` → `11706` | 每改一次脚本就要再改一次 README |
| W1–W4 | `15372` → `15377` | 同上 |
| 第十七轮 | `15377` → `15433` | 同上 |

每次都一样：改完脚本 → 跑回归 → 回归不报错 → 我事后想起来 README 里那个数
可能过期了 → 手工 `wc -l` → `edit` → 重签 manifest → 再跑一次回归。

**这个循环的成本不是那几秒 `wc -l`，是三件事：**
1. 它每次都要多签一次 manifest（80 个文件的 HMAC 全量重算）；
2. 它每次都可能被忘掉 —— 忘了不会红，只会让文档慢慢失真；
3. 文档失真的代价在别处：将来有人按 README 的「36 个 .py / 15433 行」判断
   这仓有多大，得到的答案是错的，而没有任何东西会告诉他。

所以这个文件做两件事：**把数字算出来**（`compute`），**拿它核 README**（`check`），
需要时**就地修正**（`fix`）。真正的读者是 `spec_lint.py` 的 S022。

## 为什么不用「生成整份 README」

因为 README 里绝大多数句子是人写的判断（为什么这么设计、踩过什么坑），
把它们塞进模板只会让文档变得没人想读。这里只接管**有唯一正确答案**的
那几处声明，其余照旧手写。

## 一处特别的自指

本文件自己也住在 `.workbench/scripts/` 下，所以**新增它本身就会让
`script_files` 这个数变一次**。第一次跑 `--fix` 就是给它自己改的。
这不是 bug，是这个设计在演示它自己：数字由代码算，不由记忆维护。

用法：
    python .workbench/scripts/doc_numbers.py            # 打印真值
    python .workbench/scripts/doc_numbers.py --check    # 核 README，不符 exit 2
    python .workbench/scripts/doc_numbers.py --fix      # 就地修正，打印改了哪几处
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
README = os.path.join(ROOT, "README.md")


# ---------------------------------------------------------------- 真值计算
def compute(wb=None) -> dict:
    """从仓库现状算出所有被文档引用的数字。每个值都能被独立复核。"""
    wb = wb or WB
    root = os.path.dirname(wb)
    t = {}

    # manifest 条目数（"path" 是每个 entry 的固定键）
    man = os.path.join(wb, "root.manifest")
    t["manifest_entries"] = 0
    if os.path.isfile(man):
        with open(man, encoding="utf-8") as fh:
            t["manifest_entries"] = sum(1 for line in fh if '"path"' in line)

    # 事件总数 / 在役数
    ev = os.path.join(wb, "spec", "events.schema.json")
    if os.path.isfile(ev):
        with open(ev, encoding="utf-8") as fh:
            doc = json.load(fh)
        events = doc.get("events") or doc.get("event") or []
        total = len(events)
        retired = sum(1 for e in events if e.get("retired") or
                      str(e.get("note", "")).startswith("已退役"))
        t["events_total"] = total
        t["events_live"] = total - retired
    else:
        t["events_total"] = t["events_live"] = 0

    # 状态 / 迁移 / 硬规则
    st = os.path.join(wb, "spec", "states.json")
    t["states"] = t["transitions"] = 0
    if os.path.isfile(st):
        with open(st, encoding="utf-8") as fh:
            doc = json.load(fh)
        t["states"] = len(doc.get("states") or [])
        t["transitions"] = len(doc.get("transitions") or [])
    ru = os.path.join(wb, "spec", "rules.json")
    t["rules"] = 0
    if os.path.isfile(ru):
        with open(ru, encoding="utf-8") as fh:
            t["rules"] = len((json.load(fh).get("checks") or []))

    # 脚本文件数 / 总行数
    files = sorted(glob.glob(os.path.join(wb, "scripts", "*.py")))
    t["script_files"] = len(files)
    lines = 0
    for p in files:
        with open(p, "rb") as fh:
            lines += fh.read().count(b"\n")
    t["script_lines"] = lines

    # 回归项数 / 测试数 —— 从 run_all_checks.sh 真实调用点算，不读它自己的注释。
    #
    # 为什么不数 `^run` 的行数：测试是在 `for t in ...; do run "test $t" ... done`
    # 里循环跑的，那一个 run 会执行 N 次。只数行会得到 21，而实际 PASS 是 46 ——
    # 差的那 25 个正是循环展开的测试。**注释里的数字和代码里的调用点不是一回事**，
    # 这里必须按「实际会执行几次」算。
    rail = os.path.join(root, "run_all_checks.sh")
    t["checks_total"] = t["test_count"] = 0
    t["audit_count"] = t["check_count"] = 0
    if os.path.isfile(rail):
        with open(rail, encoding="utf-8") as fh:
            body = fh.read()
        # 测试清单：for 循环里的名字
        m = re.search(r"^for t in (.*?); do", body, re.M | re.S)
        looped = bool(m)
        if m:
            t["test_count"] = len(m.group(1).replace("\\\n", " ").split())
        # 缩进敏感：`run` 在 for 循环里是缩进的，在顶层不缩进。只抓顶层会漏掉
        # 循环里那一个（漏了才知道），所以这里是 `\s*`。
        labels = re.findall(r'^\s*run(?:_warn)?\s+"([^"]+)"', body, re.M)
        t["audit_count"] = sum(1 for x in labels if x.startswith("audit "))
        # 「校验」= check 类（含 warn-only 的环境探针）+ 契约类。README 把它们
        # 合成一个数，因为对读的人而言它们都是「不是测试、也不是审计的闸门」。
        t["verify_count"] = sum(1 for x in labels
                                if x.startswith(("check ", "contract "))
                                or x.startswith("sign_manifest"))
        # 三个分项各自留名：头注释按 check / contract / sign_manifest 分行声明，
        # 只给总数时它只能比总数，任一分项写错都查不出来。
        t["check_only"] = sum(1 for x in labels if x.startswith("check "))
        t["contract_only"] = sum(1 for x in labels if x.startswith("contract "))
        t["sign_only"] = sum(1 for x in labels if x.startswith("sign_manifest"))
        # 项数 = 单项标签数 − 那个循环本身 + 循环展开的真实测试数。
        # 直接数标签会漏掉循环展开（21 标签 → 46 项），这正是「注释数字
        # 和代码调用点不是一回事」的地方，必须按「实际执行几次」算。
        # singles = 循环体之外的独立条目数。
        # 早先写成 len(labels) - 1（默认循环里只有一个 run），于是循环体里
        # 加第二个 run（复用分支）后总数凭空多 1，README 与真值差一位。
        # 判据改成「标签里不含 $t 的才是独立条目」：循环里无论几个 run，
        # 它们的标签都带 $t，不会污染计数。
        singles = sum(1 for x in labels if "$t" not in x)
        t["checks_total"] = singles + t["test_count"]
        t["e2e_count"] = sum(1 for x in labels if x.startswith("e2e "))
    return t


# ---------------------------------------------------------------- 声明点
# 每条 = (名字, 正则, 取值键, 替换模板)。正则必须恰好命中一处，
# 命中 0 处或 >1 处都要报出来 —— 「规则悄悄失效」比「规则报错」危险得多。
CLAIMS = [
    ("manifest", r"# (\d+) 个文件的 HMAC 清单", "manifest_entries",
     "# {v} 个文件的 HMAC 清单"),
    ("events", r"#\s*(\d+) 个编号 / (\d+) 在役", "events_total",
     "#   {v} 个编号 / {live} 在役"),
    ("states", r"#\s*(\d+) 态 / (\d+) 迁移", "states",
     "#   {v} 态 / {transitions} 迁移"),
    ("scripts_tree", r"scripts/\s+# (\d+) 个 \.py", "script_files",
     "scripts/                 # {v} 个 .py"),
    ("scripts_head", r"脚本清单与职责边界（(\d+) 个 \.py / (\d+) 行", "script_files",
     "脚本清单与职责边界（{v} 个 .py / {lines} 行"),
    # 2026-11-14：正则原先是
    #   r"# (\d+) 项：(\d+) 测试 \+ (\d+) 审计"
    # 圈到「审计」就停，后半句「+ 27 校验」整个落在量程外 —— 修正函数不动它，
    # 检查函数不比它。同一个句子里，两个数字有人管，第三个没人管。
    ("rail_items",
     r"# (\d+) 项：(\d+) 测试 \+ (\d+) 审计 \+ (\d+) 校验",
     "checks_total",
     "# {v} 项：{tests} 测试 + {audit} 审计 + {verify} 校验"),
    ("rail_detail",
     r"→ (\d+) 测试 \+ (\d+) 审计 \+ (\d+) 校验",
     "checks_total",
     "→ {tests} 测试 + {audit} 审计 + {verify} 校验"),
    ("rail_summary", r"= \*\*(\d+) 项 / (\d+) 通过\*\*", "checks_total",
     "= **{v} 项 / {v} 通过**"),
]


def _fmt(tmpl, key, truth) -> str:
    return tmpl.format(
        v=truth.get(key, 0),
        live=truth.get("events_live", 0),
        transitions=truth.get("transitions", 0),
        lines=truth.get("script_lines", 0),
        tests=truth.get("test_count", 0),
        audit=truth.get("audit_count", 0),
        # 「校验」这一位原本没有名字：rail_items 的模板不收它，正则也不比它，
        # 于是 README 写 27、真值 31、:4 写 29，三个数字同时存在而无人报警。
        # 量程缺一格的尺子比没有尺子更坏 —— 它让人以为量过了。
        verify=truth.get("verify_count", 0),
        check=truth.get("check_only", 0),
        contract=truth.get("contract_only", 0),
        sign=truth.get("sign_only", 0),
    )


def check(text, truth, rail=True) -> list:
    """核对 README 里的声明数字。返回 findings 列表（空 = 一致）。

    ``rail=False`` 只量传入的 text，不顺手去量 run_all_checks.sh ——
    假仓测试必须传 False，否则会拿假 truth 去核真头注释：
    那不是「数字错」，那是「一把尺子量了两件事」。
    """
    out = []
    for name, pat, key, tmpl in CLAIMS:
        hits = list(re.finditer(pat, text))
        if not hits:
            out.append("%s: 声明点没找到（正则 %r）—— 要么文案被改走了，"
                       "要么这条约束已经悄悄失效" % (name, pat))
            continue
        if len(hits) > 1:
            out.append("%s: 正则命中 %d 处，预期恰好 1 处" % (name, len(hits)))
            continue
        want = _fmt(tmpl, key, truth)
        have = hits[0].group(0)
        # 只比「模板里出现的那些数字」，不比整句（整句里的措辞可能合法地变）
        want_nums = re.findall(r"\d+", want)
        have_nums = re.findall(r"\d+", have)
        if want_nums != have_nums:
            out.append("%s: README 写 %s，实际应为 %s" % (name, have_nums, want_nums))
    if rail:
        out.extend(check_rail_header(truth))
    return out


# ------------------------------------------------------- 回归脚本自我声明
# run_all_checks.sh 的头注释是它对「自己会跑多少项」的声明。它不在 README 里，
# 也不被任何测试断言，于是 doc_numbers 只管 README 时它就永久无人核对 ——
# 2026-11-14 实测：:3 写 31 校验 / 111 项（对），:4 写 29 check（错，实际 28），
# README 另写 27 校验（错，实际 31）。同一条尺子必须同时量「文档说跑多少」
# 和「它真的跑多少」，因为这是同一件事的两种写法。
# HERE = <仓根>/.workbench/scripts，仓根要往上两级。
# 第一版写成 dirname(HERE) 得到 .workbench，于是 find 不到脚本，
# 而 check 只报「找不到 run_all_checks.sh」—— 尺子量错了对象还自称在工作。
RAIL = os.path.join(os.path.dirname(os.path.dirname(HERE)), "run_all_checks.sh")
RAIL_HEAD = [
    ("rail_header_items",
     r"# run_all_checks\.sh —— 一键回归：(\d+) 测试 \+ (\d+) 审计 \+ (\d+) 校验 "
     r"\+ 覆盖率 \+ e2e = (\d+) 项",
     "# run_all_checks.sh —— 一键回归：{tests} 测试 + {audit} 审计 + {verify} 校验"
     " + 覆盖率 + e2e = {v} 项"),
    ("rail_header_split",
     r"#\s+(\d+) 测试（for 清单）/ (\d+) audit / (\d+) check（含 1 scheduler status，"
     r"warn-only）/ (\d+) contract \+ (\d+) sign_manifest / (\d+) e2e",
     "#   {tests} 测试（for 清单）/ {audit} audit / {check} check（含 1 scheduler status，"
     "warn-only）/ {contract} contract + {sign} sign_manifest / {e2e} e2e"),
]


def _rail_fmt(tmpl, truth):
    return tmpl.format(
        v=truth.get("checks_total", 0),
        tests=truth.get("test_count", 0),
        audit=truth.get("audit_count", 0),
        verify=truth.get("verify_count", 0),
        check=truth.get("check_only", 0),
        contract=truth.get("contract_only", 0),
        sign=truth.get("sign_only", 0),
        e2e=truth.get("e2e_count", 0),
    )


def check_rail_header(truth) -> list:
    """核对 run_all_checks.sh 头注释的自我声明。"""
    out = []
    if not os.path.isfile(RAIL):
        out.append("rail_header: 找不到 %s" % RAIL)
        return out
    with open(RAIL, encoding="utf-8") as fh:
        text = fh.read()
    for name, pat, tmpl in RAIL_HEAD:
        hits = list(re.finditer(pat, text))
        if len(hits) != 1:
            out.append("%s: 正则命中 %d 处，预期恰好 1 处" % (name, len(hits)))
            continue
        want = re.findall(r"\d+", _rail_fmt(tmpl, truth))
        have = re.findall(r"\d+", hits[0].group(0))
        if want != have:
            out.append("%s: 头注释写 %s，实际应为 %s" % (name, have, want))
    return out


def fix(text, truth):
    """就地修正。返回 (新文本, 改了哪几处)。"""
    changed = []
    for name, pat, key, tmpl in CLAIMS:
        hits = list(re.finditer(pat, text))
        if len(hits) != 1:
            continue
        want = _fmt(tmpl, key, truth)
        have = hits[0].group(0)
        if re.findall(r"\d+", want) != re.findall(r"\d+", have):
            text = text[:hits[0].start()] + want + text[hits[0].end():]
            changed.append("%s: %s  →  %s" % (name, have.strip(), want.strip()))
    return text, changed


def fix_rail_header(truth):
    """就地修正 run_all_checks.sh 头注释。返回 (新文本, 改了哪几处)。"""
    changed = []
    if not os.path.isfile(RAIL):
        return None, changed
    with open(RAIL, encoding="utf-8") as fh:
        text = fh.read()
    for name, pat, tmpl in RAIL_HEAD:
        hits = list(re.finditer(pat, text))
        if len(hits) != 1:
            continue
        want = _rail_fmt(tmpl, truth)
        have = hits[0].group(0)
        if re.findall(r"\d+", want) != re.findall(r"\d+", have):
            text = text[:hits[0].start()] + want + text[hits[0].end():]
            changed.append("%s: %s  →  %s" % (name, have.strip(), want.strip()))
    return text, changed


def _cover_count(wb=None) -> int:
    """按 sign_manifest 自己的 COVER 规则算出「签名之后应该有多少条 entry」。

    存在的理由是一次真实的踩坑：我先跑 `--fix` 再跑 `sign_manifest --init`，
    于是 README 被写上了**签名前**的条数（81），而真值已经是 82。
    `--fix` 没有报错，它安静地把一个马上就要过期的数字写进了文档 ——
    这比不做自动修正更坏。所以 fix 之前先问一句「manifest 是不是已经合上了」。

    **为什么直接调 sign_manifest 而不再实现一遍 COVER**：我第一版就是自己抄了
    一份 glob 列表，算出来 60 条（真值 82）—— 两把尺子量同一个东西，两处同时错。
    这里必须复用 `sign_manifest.collect_files()`，那条规则只允许有一个实现。
    """
    sys.path.insert(0, HERE)
    import sign_manifest as sm
    prev = sm.WB if hasattr(sm, "WB") else None
    try:
        if wb:
            sm.WB = wb
        return len(sm.collect_files())
    finally:
        if prev is not None:
            sm.WB = prev


def _stale_manifest_hint(wb=None) -> str:
    real = _cover_count(wb)
    t = compute(wb)
    if t["manifest_entries"] and t["manifest_entries"] != real:
        return ("manifest 陈旧：文件里有 %d 条，按 COVER 应该是 %d 条。"
                "先跑 sign_manifest.py --init 再 --fix，否则写进 README 的是旧值。"
                % (t["manifest_entries"], real))
    return ""


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    truth = compute()
    if "--fix" in argv:
        if not os.path.isfile(README):
            print("REFUSE：找不到 %s" % README)
            return 2
        hint = _stale_manifest_hint()
        if hint:
            print("REFUSE --fix：%s" % hint)
            print("（这条守卫是踩出来的：先 fix 后签名，会把旧条数写进文档，而且不报错。）")
            return 2
        with open(README, encoding="utf-8") as fh:
            text = fh.read()
        new, changed = fix(text, truth)
        if changed:
            with open(README, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
            print("README 已修正 %d 处：" % len(changed))
            for c in changed:
                print("  " + c)
        else:
            print("README 无需修正")
        # 头注释与 README 是同一套数，必须同一个命令一起修。
        # 只修 README 会让 --fix 打印「无需修正」而 check 仍红。
        rail_new, rail_changed = fix_rail_header(truth)
        if rail_changed and rail_new is not None:
            with open(RAIL, "w", encoding="utf-8", newline="") as fh:
                fh.write(rail_new)
            print("run_all_checks.sh 已修正 %d 处：" % len(rail_changed))
            for c in rail_changed:
                print("  " + c)
        elif not rail_changed:
            print("run_all_checks.sh 无需修正")
        return 0
    if "--check" in argv:
        if not os.path.isfile(README):
            print("REFUSE：找不到 %s" % README)
            return 2
        with open(README, encoding="utf-8") as fh:
            findings = check(fh.read(), truth)
        if findings:
            for f in findings:
                print("  [S022] %s" % f)
            print("RESULT: FAIL（%d 处不一致；跑 --fix 可自动修正）" % len(findings))
            return 2
        print("RESULT: PASS — README 里的 %d 处声明数字与实际一致" % len(CLAIMS))
        return 0
    for k in sorted(truth):
        print("%-18s %s" % (k, truth[k]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
