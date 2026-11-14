#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_response_guard.py —— 快速响应 SOP 的机读门禁防回归。

被测对象：仓根 `response_guard.py`（不在 .workbench 可信根内，A 级资产）。

为什么值得有测试：m07054 的教训是「S 级事实问答被当成 B 级结构审计跑了」，
而 SOP.md 当时只是文档 —— 声明悬空。一旦 `classify()` 把 `.workbench/scripts/`
判成 S，这个守卫就会变成「给偷懒盖章」的工具，比没有更坏。所以这里钉三件事：
  1. 分级不能漏判（尤其绝对路径、Windows 反斜杠、B 优先于 A）；
  2. 漏跑必跑项必须非 0 退出（不能只做「一律放行」）；
  3. S 级确实不要求回归链（效率的来源就在这里，不能为了「稳」把它也塞进 B）。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(ROOT, "response_guard.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import response_guard as rg  # noqa: E402

OKS = []
FAILS = []


def ok(name, cond, detail=""):
    if cond:
        OKS.append(name)
    else:
        FAILS.append("%s%s" % (name, ("  << " + detail) if detail else ""))


def t_levels():
    ok("空变更 → S", rg.classify([])["level"] == "S")
    ok("纯文档 README → A", rg.classify(["README.md"])["level"] == "A")
    ok("测试文件 → A", rg.classify(["tests/test_run_task.py"])["level"] == "A")
    ok("回归入口 → A", rg.classify(["run_all_checks.sh"])["level"] == "A")
    ok("本守卫自身 → A", rg.classify(["response_guard.py"])["level"] == "A")


def t_b_assets():
    for p in (".workbench/scripts/lead.py", ".workbench/spec/enums.json",
              ".workbench/config.yaml", ".workbench/roles/agents.json",
              ".workbench/state/members.json", ".workbench/root.manifest"):
        ok("B 覆盖 %s" % p, rg.classify([p])["level"] == "B")


def t_path_forms():
    # Windows 反斜杠
    ok("反斜杠路径仍是 B",
       rg.classify([".workbench\\scripts\\lead.py"])["level"] == "B")
    # 绝对路径：曾因只处理相对前缀而把 B 判成 S（第十六轮自检发现）
    abs_ok = rg.classify(["D:/Users/THH32/DSH/02-projects/znz-workbench/workbench/.workbench/scripts/x.py"])
    ok("绝对路径仍是 B", abs_ok["level"] == "B", json.dumps(abs_ok, ensure_ascii=False))
    ok("绝对路径可被点名", abs_ok["matched"] and
       abs_ok["matched"][0].startswith(".workbench/"), str(abs_ok.get("matched")))
    ok("workbench/ 前缀可归一",
       rg.classify(["workbench/tests/test_x.py"])["level"] == "A")


def t_precedence():
    r = rg.classify(["README.md", ".workbench/scripts/lead.py"])
    ok("B 优先于 A", r["level"] == "B")
    ok("B 只点名命中项", r["matched"] == [".workbench/scripts/lead.py"], str(r["matched"]))


def t_required():
    ok("S 无必跑项（效率来源）", rg.required_checks("S") == [])
    ok("A 两项", len(rg.required_checks("A")) == 2)
    b = rg.required_checks("B")
    ok("B 必含重签 --init", "sign_manifest.py --init" in b)
    ok("B 必含 --verify", "sign_manifest.py --verify" in b)
    ok("B 必含全量回归", "run_all_checks.sh" in b)


def t_audit():
    ok("S 空跑即通过", rg.audit("S", [])["verdict"] == "OK")
    ok("A 全跑通过", rg.audit("A", rg.required_checks("A"))["verdict"] == "OK")
    ok("B 全跑通过", rg.audit("B", rg.required_checks("B"))["verdict"] == "OK")
    miss = rg.audit("B", ["spec_lint.py"])
    ok("B 漏跑被拒", miss["verdict"] == "MISSED_GATE")
    ok("B 漏跑点名重签", "sign_manifest.py --init" in miss["missing"], str(miss["missing"]))
    ok("B 空跑被拒", rg.audit("B", [])["verdict"] == "MISSED_GATE")


def t_audit_conditional():
    b_all = rg.required_checks("B")
    b_no_gen = [c for c in b_all if c != rg.GEN_SPEC_CHECK]

    r1 = rg.audit("B", b_no_gen, changed_paths=[".workbench/scripts/lead.py"])
    ok("B 非生成源免 gen_spec", r1["verdict"] == "OK", str(r1["missing"]))
    ok("B 非生成源标注免跑", r1["gen_spec_required"] is False)

    r2 = rg.audit("B", [], changed_paths=[".workbench/config.yaml"])
    ok("B 非生成源缺重签仍被拒", r2["verdict"] == "MISSED_GATE")

    r3 = rg.audit("B", b_no_gen, changed_paths=[".workbench/spec/enums.json"])
    ok("B 动 spec 源缺 gen_spec 被拒", r3["verdict"] == "MISSED_GATE")
    ok("B 动 spec 源点名 gen_spec", rg.GEN_SPEC_CHECK in r3["missing"], str(r3["missing"]))

    r4 = rg.audit("B", b_no_gen, changed_paths=[".workbench/scripts/gen_spec.py"])
    ok("B 动 gen_spec.py 缺 gen_spec 被拒", r4["verdict"] == "MISSED_GATE")


def t_batch():
    """批量语义：判 S 不等于「这批不用验」—— 措辞必须能自证。"""
    s = rg.batch([("T1", ["src/a.py"]), ("T2", ["src/b.py"])])
    ok("批内不动控制面 → S", s["level"] == "S")
    ok("S 批显式说明不额外回归", "验收仍由任务链负责" in s["note"], s["note"])
    ok("S 批不点名可信根", s["trust_root"] == [])
    b = rg.batch([("T1", ["src/a.py"]), ("T2", [".workbench/spec/enums.json"])])
    ok("批内一个 B 即整批 B", b["level"] == "B")
    ok("B 批点名文件", b["trust_root"] == [".workbench/spec/enums.json"], str(b["trust_root"]))
    ok("B 批带完整清单", b["required"] == rg.required_checks("B"))
    ok("空批 → S", rg.batch([])["level"] == "S")
    ok("批次保留每任务级别", [p["level"] for p in b["per_task"]] == ["S", "B"])


def t_expected_dirs():
    """S021 的事实源：白名单必须覆盖真实存在的顶层目录，且不留过期项。"""
    exp = set(rg.EXPECTED_WORKBENCH_DIRS)
    ok("白名单含 scripts", "scripts" in exp)
    ok("白名单含纪律账本目录", "response_guard" in exp)
    ok("白名单含判据根 capability_evidence", "capability_evidence" in exp)
    ok("白名单含判据根 token_ledger", "token_ledger" in exp)
    wb = os.path.join(ROOT, ".workbench")
    if os.path.isdir(wb):
        actual = {d for d in os.listdir(wb)
                  if os.path.isdir(os.path.join(wb, d)) and not d.startswith(".")
                  and d != "__pycache__"}
        ok("真实目录都在白名单内", not (actual - exp), str(sorted(actual - exp)))
        # 反方向刻意不钉：白名单多一项是「允许存在」不是「必须存在」，
        # archive/ memory/ notify/ 天然可以为空或未创建。把它当失败，
        # 就是让无害的闲项把回归刷红。
        ok("白名单是超集语义（允许未创建）", exp >= {"scripts", "response_guard"})


def t_ledger():
    """纪律账本：追加写入、可回读、不覆盖上一轮。"""
    import tempfile as _tf
    d = _tf.mkdtemp(prefix="guard-ledger-")
    old_dir, old_path = rg.LEDGER_DIR, rg.LEDGER_PATH
    try:
        rg.LEDGER_DIR = d
        rg.LEDGER_PATH = os.path.join(d, "guard_ledger.jsonl")
        n1 = rg.write_ledger({"level": "B", "verdict": "OK"})
        n2 = rg.write_ledger({"level": "A", "verdict": "MISSED_GATE"})
        ok("账本首条行数 1", n1 == 1)
        ok("账本追加不覆盖", n2 == 2, "n2=%d" % n2)
        rows = rg.read_ledger()
        ok("回读两条", len(rows) == 2)
        ok("保留上一轮级别", rows[0]["level"] == "B")
        ok("自动补 ts_ms", isinstance(rows[1].get("ts_ms"), int))
    finally:
        rg.LEDGER_DIR, rg.LEDGER_PATH = old_dir, old_path
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def _cli(*args):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, GUARD] + list(args), cwd=ROOT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)


def t_cli_exit_codes():
    p1 = _cli("--selfcheck")
    ok("CLI --selfcheck 退出 0", p1.returncode == 0, p1.stdout.decode("utf-8", "replace")[:200])
    ok("CLI --selfcheck 输出 PASS", b'"verdict": "PASS"' in p1.stdout)
    p2 = _cli("--audit", "--level", "B", "--ran", "spec_lint.py")
    ok("CLI 漏跑退出非 0", p2.returncode == 2, "rc=%d" % p2.returncode)
    p3 = _cli("--audit", "--level", "S")
    ok("CLI S 级退出 0", p3.returncode == 0, "rc=%d" % p3.returncode)

    b_no_gen = [c for c in rg.required_checks("B") if c != rg.GEN_SPEC_CHECK]
    p4 = _cli("--audit", "--level", "B", "--paths", ".workbench/scripts/lead.py",
              "--ran", *b_no_gen)
    ok("CLI 非生成源免 gen_spec 退出 0", p4.returncode == 0,
       p4.stdout.decode("utf-8", "replace")[:200])
    p5 = _cli("--audit", "--level", "B", "--paths", ".workbench/spec/enums.json",
              "--ran", *b_no_gen)
    ok("CLI 动 spec 源缺 gen_spec 退出 2", p5.returncode == 2,
       p5.stdout.decode("utf-8", "replace")[:200])


def t_observe():
    """观察值：数真实执行痕迹，且「没跑」与「跑了 0 项」必须分得开。"""
    import tempfile as _tf
    d = _tf.mkdtemp(prefix="guard-observe-")
    try:
        p = os.path.join(d, "roundX.txt")
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("[exit=0]  a\n[exit=1]  b\n[exit=0]  c\n")
        ok("数出 3 条 exit 标记", rg.observe(p) == 3)
        ok("不存在的证据 → None", rg.observe(os.path.join(d, "nope.txt")) is None)
        ok("空路径 → None", rg.observe("") is None)
        ok("没证据判 NO_EVIDENCE", rg.compare("S", None)["verdict"] == "NO_EVIDENCE")
        # 全量回归日志不是「本轮清单」：一把尺子量两件事，两处同时错
        full = os.path.join(d, "full.txt")
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("[exit=0] x\n\npassed: 44 / failed: 0 / RESULT: ALL PASS\n")
        ok("全量日志拒比", rg.compare("B", 44, evidence_path=full)["verdict"]
           == "NOT_COMPARABLE")
        ok("小样本可比", rg.compare("A", rg.expected_checks("A"))["verdict"] == "IN_RANGE")
        ok("远超则 OVER_RUN",
           rg.compare("A", rg.expected_checks("A") * 8 + 10)["verdict"] == "OVER_RUN")
        ok("少于则 UNDER_RUN", rg.compare("B", 1)["verdict"] == "UNDER_RUN")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def main():
    for fn in (t_levels, t_b_assets, t_path_forms, t_precedence,
               t_required, t_audit, t_audit_conditional,
               t_batch, t_expected_dirs, t_ledger, t_observe, t_cli_exit_codes):
        fn()
    total = len(OKS) + len(FAILS)
    for f in FAILS:
        print("  FAIL %s" % f)
    print("test_response_guard: %s (%d/%d)" % ("PASS" if not FAILS else "FAIL",
                                               len(OKS), total))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
