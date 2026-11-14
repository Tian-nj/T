#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""response_guard.py —— 快速响应 SOP 的机读执行体（第十六轮）。

为什么需要它：SOP.md 与 USER.md 都是「声明」，没有真读者、没有退出码，
等于本项目反复修的那一类 defect —— 声明悬空。规则写了没人强制执行时，
「改了 scripts/spec 忘了重签」会第 N 次发生，而且报错长得像链坏。

本文件把 SOP 的三条硬约束变成可判定的函数：
  classify(paths)          按变更路径判 S / A / B，不靠记忆
  required_checks(level)   给出该级别必须跑的验证清单
  audit(level, ran)        B 级漏跑任一必跑项 -> MISSED_GATE + 非 0 退出码
  batch(pairs)             一批任务的合并「可信根影响面」（供 dispatch 开工前提示）
  write_ledger(record)     把每轮实跑清单追加落盘，让「跑了哪些」可事后复核
  selfcheck()              正反用例自检，由 tests/test_response_guard.py 与回归门禁调用

诚实边界：它约束「改文件的人」，不拦截模型思考；S 级 required_checks 为空
是刻意设计 —— 事实问答不启动回归链，这正是效率的来源。

两处刻意不放进 .workbench 可信根的东西（原因不同，都要写清）：
  1. 本文件本身放在仓根：它是 Lead 的开工前 preflight 纪律工具，不是控制面
     组件；塞进去就要给一份「纪律工具」做 HMAC 签名，语义不对。
  2. 账本落在 .workbench/response_guard/：它是纪律记录，不是判据的根。
     若放进 capability_evidence/ 或 token_ledger/（两者都在 COVER 内），
     每写一条都会让 manifest drift，等于逼着人为了消红去重签。
"""
import argparse
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.join(HERE, ".workbench")
LEDGER_DIR = os.path.join(WB, "response_guard")
LEDGER_PATH = os.path.join(LEDGER_DIR, "guard_ledger.jsonl")

# 命中即 B：改这些会改变生成源 / 可信根 / 状态机语义
B_PREFIXES = (
    ".workbench/scripts/", ".workbench/spec/", ".workbench/config.yaml",
    ".workbench/roles/", ".workbench/state/members.json",
    ".workbench/root.manifest", ".workbench/Constitution.md",
    ".workbench/constitution.rules.yaml", ".workbench/WORKING-AGREEMENT.md",
)
# 命中且未命中 B 即 A：文档 / 报告 / 测试 / 仓根编排脚本
A_PREFIXES = (
    "README.md", "tests/", "evidence/", "run_all_checks.sh", "demo.sh",
    "run_task.py", "response_guard.py", "audit_", "智能工作台-",
)

# S021 的唯一事实源：.workbench 下允许出现的顶层目录。
# 新增目录却不登记，等于给「证据/记录放哪儿」开了后门 —— 放错位置的文件
# 要么进不了可信根（改了不报 drift），要么进错可信根（每写一条都要重签）。
EXPECTED_WORKBENCH_DIRS = {
    # 控制面（在 manifest COVER 内）
    "scripts", "spec", "roles", "state", "templates", "contracts",
    # 第三十三轮：参考执行体。它在 COVER 内（契约的另一半改了必须 drift），
    # 但**不在 scripts/ 下**——位置本身就是声明：执行体活在控制面之外，
    # 控制面不该替它决定怎么干活，只该决定它交上来的东西算不算数。
    "executors",
    "archive", "memory", "notify",
    # 判据的根（在 COVER 内，改了必须重签）
    "capability_evidence", "token_ledger",
    # 运行产物（不在 COVER，随时可变）
    "events", "tasks", "wt", "metrics",
    # 纪律记录（不在 COVER：写一条就 drift 的账本没人敢记）
    "response_guard",
    # 识海（W1–W4）：persona/diary 是伙伴的人格与日记 —— 两者都在
    # paths.yaml 的 allow_write 里，但**不在** manifest COVER 内。
    # 理由：它们是「会持续长大的内容」，每写一段人格或一篇日记就 drift，
    # 等于逼人在"记下来"和"别刷红"之间选，最后一定选不记。
    # 可信根只保护**规则**（spec/scripts/config/roles），不保护**内容**。
    "persona", "diary",
}

B_CHECKS = [
    "gen_spec.py --check（仅当动生成源）",
    "sign_manifest.py --init",
    "sign_manifest.py --verify",
    "spec_lint.py",
    "targeted_test",
    "run_all_checks.sh",
]
A_CHECKS = ["py_compile", "targeted_test"]
S_CHECKS = []

# 条件式必跑项：只有动了「生成源」才需要重生成并核对。
# 为什么不做成无条件：无条件要求会让人在纯改 config.yaml 时也去跑
# gen_spec.py --check —— 它必然通过（没动源），于是这条检查退化成
# 「点了名但永不失败」，跟没写一样。条件化后它仍会真失败（动了 spec 却不重生成）。
GEN_SPEC_CHECK = "gen_spec.py --check（仅当动生成源）"
GEN_SPEC_TRIGGERS = (
    ".workbench/spec/",
    ".workbench/scripts/gen_spec.py",
)


def gen_spec_required(paths):
    """这一笔变更是否必须重生成 spec —— 判据只有「有没有动生成源 or 产物」。"""
    ps = [_norm(p) for p in (paths or [])]
    return any(p.startswith(x) for p in ps for x in GEN_SPEC_TRIGGERS)


def _norm(p):
    """归一路径：反斜杠→斜杠，去掉 ./、workbench/ 前缀；绝对路径截到 .workbench/。

    第十六轮自检发现：只处理相对路径时，传绝对路径（例如 ops_bash 里
    `git -C D:/.../workbench diff` 的输出或手工贴的完整路径）会让 B_PREFIXES
    全部匹配不到，于是「改了 scripts/ 却被判成 S」—— 正是本工具要防的那类
    过度省验证。截取规则以 `/.workbench/` 为锚，比猜盘符稳。
    """
    s = str(p).replace("\\", "/")
    if s.startswith("./"):
        s = s[2:]
    mark = "/.workbench/"
    if mark in s:
        s = ".workbench/" + s.split(mark, 1)[1]
    elif s.startswith("workbench/"):
        s = s[len("workbench/"):]
    return s


def classify(paths):
    """返回 {'level','matched','reason'}。B 优先于 A，A 优先于 S。"""
    ps = [_norm(p) for p in (paths or [])]
    b = [p for p in ps if any(p.startswith(x) for x in B_PREFIXES)]
    if b:
        return {"level": "B", "matched": b,
                "reason": "变更命中生成源/可信根，必须走完整验证链"}
    a = [p for p in ps if any(p.startswith(x) for x in A_PREFIXES)]
    if a:
        return {"level": "A", "matched": a,
                "reason": "局部改动，一条命令串行 py_compile + 针对性测试"}
    return {"level": "S", "matched": [],
            "reason": "无控制面变更/纯口径问答，0 次或最多 1 次取值，不启动回归链"}


def required_checks(level):
    return {"S": list(S_CHECKS), "A": list(A_CHECKS), "B": list(B_CHECKS)}.get(level, [])


def audit(level, ran, changed_paths=None):
    """声明级别 vs 实跑检查。少一项即 MISSED_GATE —— 把 SOP 第 4 条变成门禁。

    changed_paths 给了就按「动了什么」收窄必跑清单：只有动生成源才要
    gen_spec.py --check。不给时保守地按全清单算（宁可多要一项，也不假装
    「没说清楚就当不需要」）。
    """
    level = (level or "S").upper()
    need = list(required_checks(level))
    need_gen = True
    if changed_paths is not None:
        need_gen = gen_spec_required(changed_paths)
        if not need_gen:
            need = [c for c in need if c != GEN_SPEC_CHECK]
    got = set(ran or [])
    missing = [c for c in need if c not in got]
    return {
        "level": level,
        "required": need,
        "ran": sorted(got),
        "missing": missing,
        "gen_spec_required": need_gen,
        "verdict": "OK" if not missing else "MISSED_GATE",
    }


def batch(pairs):
    """一批 (label, paths) 的合并「可信根影响面」。

    与 classify 的区别必须说清，否则会变成误导性信号：
      classify 回答「我自己这一笔编辑该跑到哪一级」；
      batch    回答「这批任务会不会**直接**改可信根」。
    任务的验收由 派发 → 执行 → verify → merge 那条链负责，与本文件正交；
    判成 S 不等于「不用验」，只是「不额外加一层可信根重签+全量回归」。
    """
    per, trusted = [], []
    for label, paths in (pairs or []):
        r = classify(paths or [])
        per.append({"label": label, "level": r["level"],
                    "matched": r["matched"]})
        if r["level"] == "B":
            trusted.extend(r["matched"])
    if trusted:
        level, note = "B", "批内有任务直接改可信根：必须人工转正 + 重签 + 全量回归"
    elif any(p["level"] == "A" for p in per):
        level, note = "A", "批内只动文档/测试/编排：一条命令串行验证"
    else:
        level, note = "S", "批内不动控制面：验收仍由任务链负责，不额外启动回归"
    seen, uniq = set(), []
    for t in trusted:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return {"level": level, "per_task": per,
            "trust_root": uniq, "required": required_checks(level), "note": note}


def write_ledger(record):
    """追加一条实跑记录。返回写入后的总行数。

    用追加而非覆盖：一轮的记录被下一轮覆盖掉，"这轮跑了哪些"就只剩口头说法。
    写盘失败不静默 —— 账本记不下来属于「纪律失效」，比漏一项检查更值得报错。
    """
    os.makedirs(LEDGER_DIR, exist_ok=True)
    rec = dict(record or {})
    rec.setdefault("ts_ms", int(time.time() * 1000))
    line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    with open(LEDGER_PATH, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return len(read_ledger())


def report():
    """纪律账本汇总：回答「这段时间是不是反复过度审计」。

    不统计「模型说自己调了几次工具」——那是自述，权重 0（本方案自己的公理）。
    这里统计的是账本里每条记录**当场记录的真实观察值**：
    每级多少轮、有多少轮 OVER_RUN / UNDER_RUN / NO_EVIDENCE。
    只报数，不判人；判人的事交给人看这张表。
    """
    rows = read_ledger()
    by_level = {}
    verdicts = {}
    for r in rows:
        lv = r.get("level") or "?"
        by_level[lv] = by_level.get(lv, 0) + 1
        cmp_ = (r.get("compare") or {}).get("verdict")
        if cmp_:
            verdicts[cmp_] = verdicts.get(cmp_, 0) + 1
        v = r.get("verdict")
        if v:
            verdicts["audit:" + str(v)] = verdicts.get("audit:" + str(v), 0) + 1
    return {"path": LEDGER_PATH, "rounds": len(rows),
            "by_level": by_level, "verdicts": verdicts,
            "last": rows[-1] if rows else None,
            "note": "计数来自各轮当场记录的真实观察值；不含模型自报的工具调用次数"}


def expected_checks(level, changed_paths=None):
    """该级别「该跑几项」的数目（供观察值比对）。

    给 changed_paths 时按生成源条件收窄 —— 与 audit() 同一把尺子，
    这里不另算一套，否则「该跑几项」和「该跑哪些」会漂成两个答案。
    """
    return len(audit(level, [], changed_paths=changed_paths)["required"])


def observe(evidence_path):
    """从 evidence/roundN_full.txt 数**真实执行过**的检查项。

    只数 `[exit=` 标记：那是 run() 真正跑过命令留下的唯一痕迹。
    刻意返回 None 而不是 0 —— 「没跑」和「跑了 0 项」在输出上必须不等价，
    否则本函数会变成又一个「把没跑说成通过」的尺子。
    """
    if not evidence_path or not os.path.isfile(evidence_path):
        return None
    n = 0
    with open(evidence_path, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            if ln.lstrip().startswith("[exit="):
                n += 1
    return n


def _is_full_regression(evidence_path):
    """判断这份证据是不是「全量回归日志」而不是「本轮检查清单」。

    两者不是一回事：全量回归一次跑 44 项，B 级的必跑清单只有 6 项。
    拿全量日志去比级别清单，会把「跑了一次回归」算成「过度审计」——
    一把尺子量两件事，两处同时错。宁可显式拒比，也不给一个好看但错的数。
    """
    if not evidence_path or not os.path.isfile(evidence_path):
        return False
    try:
        with open(evidence_path, encoding="utf-8", errors="replace") as fh:
            body = fh.read()
    except OSError:
        return False
    return ("RESULT: ALL PASS" in body) or ("passed:" in body and "failed:" in body)


def compare(level, observed, changed_paths=None, evidence_path=None):
    """声明级别 vs 真实观察值。under/over 都报，但都不阻断。

    over-run 才是效率问题：跑了远超该级别需要的检查，正是「把 S 级做成 B 级」。
    这里不设硬阈值拦截 —— 「多跑」有时是对的（比如怀疑有隐蔽漂移），
    拦死会把谨慎也一起拦掉；它只负责让这件事**可见**。

    tool_calls 刻意不作为判据：模型自报调用次数是自述，权重 0。
    能验的只有 evidence 里那些真实 exit 标记的条数。
    """
    exp = expected_checks(level, changed_paths)
    if observed is None:
        return {"level": level, "expected": exp, "observed": None,
                "verdict": "NO_EVIDENCE",
                "note": "没有证据文件：未跑不等于通过，也不等于跑少了"}
    if _is_full_regression(evidence_path):
        return {"level": level, "expected": exp, "observed": observed,
                "verdict": "NOT_COMPARABLE",
                "note": "这是全量回归日志，不是本轮检查清单；两者项目数本就不同，"
                        "不能用来判 over/under"}
    if observed < exp:
        verdict, note = "UNDER_RUN", "实跑少于该级别必跑项，按 audit 的 missing 处理"
    elif observed > exp * 2 + 3:
        verdict, note = "OVER_RUN", "实跑远超该级别所需（疑似把低级任务做成高级审计）"
    else:
        verdict, note = "IN_RANGE", "实跑与级别相称"
    return {"level": level, "expected": exp, "observed": observed,
            "verdict": verdict, "note": note}


def read_ledger():
    if not os.path.isfile(LEDGER_PATH):
        return []
    out = []
    with open(LEDGER_PATH, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except ValueError:
                out.append({"corrupt": ln[:200]})
    return out


def _fixture_dir():
    d = os.path.join(tempfile.gettempdir(), "response_guard_fixtures")
    os.makedirs(d, exist_ok=True)
    return d


def _fixture_full_log():
    """造一份「全量回归日志」样本。

    刻意不指向 evidence/roundN_full.txt：自检若依赖仓内真实证据文件，
    换目录、清证据、从别处调用都会让它变红 —— 那测的是环境不是判据。
    """
    p = os.path.join(_fixture_dir(), "full_like.txt")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("[exit=0] x\npassed: 44 / failed: 0 / RESULT: ALL PASS\n")
    return p


def _fixture_plain_log():
    p = os.path.join(_fixture_dir(), "plain_like.txt")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("[exit=0] a\n[exit=0] b\n")
    return p


def selfcheck():
    """正反用例：既要判对级别，也要能抓到漏跑（不能只做「一律拒绝」）。"""
    cases = []

    def t(name, cond):
        cases.append((name, bool(cond)))

    t("S 无变更", classify([])["level"] == "S")
    t("S 纯问答不要求回归", required_checks("S") == [])
    t("A README", classify(["README.md"])["level"] == "A")
    t("A 测试", classify(["tests/test_x.py"])["level"] == "A")
    t("B scripts", classify([".workbench/scripts/lead.py"])["level"] == "B")
    t("B spec", classify([".workbench/spec/enums.json"])["level"] == "B")
    t("B 优先于 A", classify(["README.md", ".workbench/scripts/x.py"])["level"] == "B")
    t("B 必含重签", "sign_manifest.py --init" in required_checks("B"))
    t("B 漏重签被拒", audit("B", ["spec_lint.py"])["verdict"] == "MISSED_GATE")
    t("B 漏重签点名", "sign_manifest.py --init" in audit("B", [])["missing"])
    t("B 全跑通过", audit("B", required_checks("B"))["verdict"] == "OK")
    t("S 无需实跑", audit("S", [])["verdict"] == "OK")
    t("动 spec 才要 gen_spec", gen_spec_required([".workbench/spec/enums.json"]))
    t("纯改 config 不要 gen_spec", not gen_spec_required([".workbench/config.yaml"]))
    t("非生成源免跑 gen_spec",
      audit("B", [c for c in required_checks("B") if c != GEN_SPEC_CHECK],
            changed_paths=[".workbench/config.yaml"])["verdict"] == "OK")
    t("动 spec 缺 gen_spec 被拒",
      audit("B", [c for c in required_checks("B") if c != GEN_SPEC_CHECK],
            changed_paths=[".workbench/spec/enums.json"])["verdict"] == "MISSED_GATE")
    t("不给路径时保守要求 gen_spec",
      audit("B", [c for c in required_checks("B") if c != GEN_SPEC_CHECK])["verdict"]
      == "MISSED_GATE")
    # 批量语义：判 S 不等于「不用验」
    t("批内不动控制面 → S", batch([("T1", ["src/a.py"])])["level"] == "S")
    t("批内改可信根 → B",
      batch([("T1", [".workbench/scripts/lead.py"])])["level"] == "B")
    t("批内 B 点名文件",
      batch([("T1", [".workbench/spec/enums.json"])])["trust_root"]
      == [".workbench/spec/enums.json"])
    t("批内 S 明说不额外回归",
      "验收仍由任务链负责" in batch([("T1", ["src/a.py"])])["note"])
    # 观察值语义：没证据与跑了 0 项必须不同（否则又是一个「没跑说成通过」的尺子）
    t("预期项数 B 大于 S", expected_checks("B") > expected_checks("S"))
    t("无证据文件 → None", observe("evidence/__no_such_file__.txt") is None)
    t("无证据 → NO_EVIDENCE", compare("S", None)["verdict"] == "NO_EVIDENCE")
    t("实跑超 8 倍 → OVER_RUN",
      compare("A", expected_checks("A") * 8 + 10)["verdict"] == "OVER_RUN")
    t("实跑与级别相称 → IN_RANGE",
      compare("A", expected_checks("A"))["verdict"] == "IN_RANGE")
    # 全量回归日志不能被当成「本轮清单」去判 over/under（一把尺子量两件事）
    _full = _fixture_full_log()
    t("全量回归日志拒比",
      compare("B", 44, evidence_path=_full)["verdict"] == "NOT_COMPARABLE")
    _empty = _fixture_plain_log()
    t("普通日志不误判为全量",
      compare("B", expected_checks("B"), evidence_path=_empty)["verdict"] == "IN_RANGE")
    bad = [n for n, ok in cases if not ok]
    return {"cases": len(cases), "failed": bad, "verdict": "PASS" if not bad else "FAIL"}


def main(argv=None):
    ap = argparse.ArgumentParser(description="快速响应 SOP 机读守卫")
    ap.add_argument("--classify", nargs="*", default=None, help="变更路径列表")
    ap.add_argument("--audit", action="store_true", help="校验级别与实跑检查")
    ap.add_argument("--level", default="S")
    ap.add_argument("--ran", nargs="*", default=[])
    ap.add_argument("--paths", "--changed-paths", dest="changed_paths",
                    nargs="*", default=None,
                    help="本轮实际改了哪些文件；给了就按它收窄必跑清单（如免跑 gen_spec）")
    ap.add_argument("--write-ledger", action="store_true",
                    help="把本次 audit 结果追加到 .workbench/response_guard/guard_ledger.jsonl")
    ap.add_argument("--ledger", action="store_true", help="回读纪律账本")
    ap.add_argument("--report", action="store_true",
                    help="纪律账本汇总：每级轮数 / 过度审计轮数 / 无结论轮数")
    ap.add_argument("--observe", default=None,
                    help="从 evidence/roundN_full.txt 数真实执行项数")
    ap.add_argument("--compare-level", default=None,
                    help="用 --observe 的结果与该级别应有项数比对")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args(argv)
    if a.report:
        print(json.dumps(report(), ensure_ascii=False, indent=2))
        return 0
    if a.ledger:
        rows = read_ledger()
        print(json.dumps({"path": LEDGER_PATH, "rows": len(rows),
                          "last": rows[-1] if rows else None},
                         ensure_ascii=False, indent=2))
        return 0
    if a.observe and not a.audit:
        r = {"evidence": a.observe, "observed": observe(a.observe)}
        if a.compare_level:
            r["compare"] = compare(a.compare_level, r["observed"],
                                   changed_paths=a.changed_paths,
                                   evidence_path=a.observe)
        # 不在这里 return 0：落进下面的统一退出码分支，
        # 否则「无证据」会被这一条捷径说成成功。
    elif a.selfcheck:
        r = selfcheck()
    elif a.classify is not None:
        r = classify(a.classify)
    elif a.audit:
        r = audit(a.level, a.ran, changed_paths=a.changed_paths)
        if a.observe:
            # 一次审计就能带上真实观察值，避免「先 audit 再 observe」两次调用
            obs = observe(a.observe)
            r = dict(r)
            r["observed"] = obs
            r["compare"] = compare(a.level, obs, changed_paths=a.changed_paths,
                                   evidence_path=a.observe)
        if a.write_ledger:
            r = dict(r)
            r["ledger_rows"] = write_ledger(r)
    else:
        r = selfcheck()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    # 退出码必须让三种状态在机器上也分得开：
    #   0 = 通过 / 无需检查   2 = 明确失败（漏跑、自检失败、输入非法）
    #   3 = 无结论（没证据、拒比）
    # 若 NO_EVIDENCE 也返 0，「没跑」和「跑过且通过」在调用方看来完全一样 ——
    # 那正是本方案自己列为 P3 的头号禁忌。
    if r.get("verdict") in ("FAIL", "MISSED_GATE", "INVALID"):
        return 2
    if r.get("verdict") == "NO_EVIDENCE" or r.get("compare", {}).get("verdict") == "NO_EVIDENCE":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
