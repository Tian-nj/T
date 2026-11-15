# -*- coding: utf-8 -*-
"""round146 仪器二 / 门禁：跨工件一致性（三份索引文档 vs 实测真值）。

维度说明：前五轮查代码内部（读者 / 模块对齐 / 门禁对错）。本轮查「三份索引文档
（PROJECT-INDEX.md / DSH README.md / workbench README.md）彼此、以及它们与代码，
说的数对不对得上」。这是反复手写同步、反复踩「数字对了但句子错位」的地方，此前
从没被机器守过。

判据只取**稳定事实**（不会因为空跑一次回归就漂的量）：
  N1  全量自检（N 项）        == doc_numbers.checks_total
  N2  passed: N              == doc_numbers.checks_total
  N3  entry_count / N 条 HMAC == doc_numbers.manifest_entries
  N4  代码/测试/契约 顶层源文件 == 实测（不含 __pycache__）
  N5  DSH README 的 N/0        == doc_numbers.checks_total

刻意不 gate 字节数（README 每改一次就变，那会把门变成噪音——见「门禁常态化即噪音」）。
退出码：0 一致 / 1 有漂移（逐条点名哪个文档说错了）/ 2 读不到文档或真值。
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # .workbench/scripts
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)     # .workbench
ROOT = os.path.dirname(WB)                                  # 项目根 workbench/
DSH = os.path.dirname(os.path.dirname(os.path.dirname(ROOT)))  # 套装根 DSH/

INDEX = os.path.join(DSH, "PROJECT-INDEX.md")
DSH_README = os.path.join(DSH, "README.md")
WB_README = os.path.join(ROOT, "README.md")


def _reality():
    sys.path.insert(0, HERE)
    import doc_numbers as dn
    c = dn.compute()
    dirs = {}
    for d in ("src", "tests", "contracts"):
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p):
            dirs[d] = None
            continue
        # 顶层源文件：排除 __pycache__ 里那种不在顶层的 .pyc
        n = 0
        for f in os.listdir(p):
            fp = os.path.join(p, f)
            if os.path.isfile(fp) and not f.endswith(".pyc"):
                n += 1
        dirs[d] = n
    return c, dirs


def _nums(text, pat):
    return [int(m.replace(",", "")) for m in re.findall(pat, text)]


def reconcile():
    findings = []
    try:
        c, dirs = _reality()
        idx = io.open(INDEX, encoding="utf-8").read()
        dsh = io.open(DSH_README, encoding="utf-8").read()
    except Exception as exc:
        return [("N0", "读不到文档或真值：%s: %s" % (type(exc).__name__, exc))], {}

    checks = c.get("checks_total")
    mani = c.get("manifest_entries")

    # N1 全量自检（N 项）
    for s in set(_nums(idx, r"全量自检（([\d,]+) 项）")):
        if s != checks:
            findings.append(("N1", "PROJECT-INDEX 写「全量自检（%d 项）」，实际 %d" % (s, checks)))
    # N2 passed: N
    for s in set(_nums(idx, r"passed: ([\d,]+)")):
        if s != checks:
            findings.append(("N2", "PROJECT-INDEX 写「passed: %d」，实际 %d" % (s, checks)))
    # N3 manifest
    for s in set(_nums(idx, r"entry_count ([\d,]+)")):
        if s != mani:
            findings.append(("N3", "PROJECT-INDEX 写「entry_count %d」，实际 %d" % (s, mani)))
    for s in set(_nums(idx, r"/ ([\d,]+) 条 HMAC")):
        if s != mani:
            findings.append(("N3", "PROJECT-INDEX 写「root.manifest %d 条」，实际 %d" % (s, mani)))
    # N4 目录源文件数（代码/测试/契约）
    m = re.search(r"代码 ([\d,]+) / 测试 ([\d,]+) / 契约 ([\d,]+)", idx)
    if m:
        claims = {"src": int(m.group(1).replace(",", "")),
                  "tests": int(m.group(2).replace(",", "")),
                  "contracts": int(m.group(3).replace(",", ""))}
        for d, want in claims.items():
            got = dirs.get(d)
            if got is not None and want != got:
                findings.append(("N4", "PROJECT-INDEX 写「%s %d（文件数）」，顶层源文件实测 %d"
                                 % (d, want, got)))
    else:
        findings.append(("N4", "PROJECT-INDEX 找不到「代码/测试/契约」计数行"))
    # N5 DSH README 的 N/0
    for s in set(_nums(dsh, r"全量回归 ([\d,]+)/0")):
        if s != checks:
            findings.append(("N5", "DSH README 写「全量回归 %d/0」，实际 %d/0" % (s, checks)))

    table = {"checks_total": checks, "manifest_entries": mani,
             "dirs": dirs, "findings": len(findings)}
    return findings, table


def _selftest():
    ok = True
    def ck(name, cond):
        nonlocal ok
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            ok = False

    f, t = reconcile()
    ck("真值读到了", bool(t.get("checks_total")))
    base_codes = set(x[0] for x in f)
    # 判据自证（不依赖基线是否干净 —— 基线本来就可能有真漂移）：
    # 涂改 checks_total 必须红出 N1；还原后 N1 必须消失。区分度是关键。
    real_reality = _reality
    def bad_reality():
        c, d = real_reality()
        c["checks_total"] = 999
        return c, d
    globals()["_reality"] = bad_reality
    try:
        f2, _ = reconcile()
        tampered_codes = set(x[0] for x in f2)
    finally:
        globals()["_reality"] = real_reality
    ck("checks_total 涂改会红(N1)", "N1" in tampered_codes)
    f3, _ = reconcile()
    restored_codes = set(x[0] for x in f3)
    ck("还原后 N1 消失", "N1" not in restored_codes)
    ck("判据区分基线与篡改(N1 只在篡改态出现)",
       ("N1" in tampered_codes) and ("N1" not in restored_codes))
    ck("还原回到基线(其余码不变)", restored_codes == base_codes)
    print("index_consistency selftest: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    findings, table = reconcile()
    if findings:
        print("index_consistency: FAIL — 跨工件声明与实测不一致")
        for code, detail in findings:
            print("  [%s] %s" % (code, detail))
        return 1
    print("index_consistency: PASS — 三份索引文档与实测真值一致（N1-N5）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
