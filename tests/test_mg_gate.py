#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_mg_gate.py — 合并闸门四分支的反证测试。

每条都是"必须被分到这一支"，而不是"看起来合理"。
判据只有两条：①分支名对 ②理由里出现关键类别。

运行：python tests/test_mg_gate.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".workbench", "scripts"))

import mg_gate  # noqa: E402
import wb_core  # noqa: E402

FAILS = []


def check(name, got, want, must_contain=None):
    ok = got["branch"] == want
    if ok and must_contain:
        blob = " ".join(got["reasons"])
        ok = must_contain in blob
    if not ok:
        FAILS.append("%-12s want=%s got=%s reasons=%s"
                     % (name, want, got["branch"], got["reasons"]))
        print("  FAIL %-12s want=%-13s got=%-13s %s"
              % (name, want, got["branch"], (got["reasons"] or [""])[0][:60]))
    else:
        print("  ok   %-12s %-13s %s" % (name, got["branch"],
                                         mg_gate.describe(got)[22:88]))


def main():
    cfg = wb_core.yaml_parse_file(os.path.join(ROOT, ".workbench", "config.yaml"))
    mg = cfg["mg"]
    print("mg:", {k: mg[k] for k in ("max_file_count", "max_net_line_count",
                                     "gray_batch_max")})
    print()

    # ── 1. never_waive 必须先撞先赢 ──────────────────────────────────
    # 这几条"改动极小"，如果先看规模就会被放成 AUTO_CORE。
    d = mg_gate.decide(["requirements.txt"], mg, added=1, deleted=0)
    check("依赖1行", d, "NEVER_WAIVE", "dependencies")

    d = mg_gate.decide([".github/workflows/ci.yml"], mg, added=3, deleted=1)
    check("CI改3行", d, "NEVER_WAIVE", "cicd")

    d = mg_gate.decide(["contracts/api.yaml"], mg, added=2, deleted=2)
    check("契约", d, "NEVER_WAIVE", "contracts")

    d = mg_gate.decide(["secrets/token.txt"], mg, added=1, deleted=0)
    check("凭据", d, "NEVER_WAIVE", "credentials")

    d = mg_gate.decide(["migrations/001_add.sql"], mg, added=3, deleted=0)
    check("迁移", d, "NEVER_WAIVE", "db_migration")

    d = mg_gate.decide([".workbench/roles/coder.md"], mg, added=1, deleted=0)
    check("权限表", d, "NEVER_WAIVE", "permissions")

    d = mg_gate.decide([".workbench/config.yaml"], mg, added=1, deleted=0)
    check("改config", d, "NEVER_WAIVE", "manifest_change")

    # file_delete 需要 statuses；不给就必须标 unverified，不许"没证据=通过"
    d = mg_gate.decide(["src/a.py"], mg, added=1, deleted=0)
    check("无statuses", d, "AUTO_CORE")
    assert "file_delete" in d["stats"]["unverified_categories"], d
    d = mg_gate.decide(["src/old.py"], mg, statuses={"src/old.py": "D"},
                       added=0, deleted=80)
    check("删文件", d, "NEVER_WAIVE", "file_delete")

    # git_force 同理
    d = mg_gate.decide(["src/a.py"], mg, commands=["git push --force origin x"],
                       added=1, deleted=0)
    check("强推", d, "NEVER_WAIVE", "git_force")

    # ── 2. AUTO_ALL：生成物不看规模 ─────────────────────────────────
    d = mg_gate.decide(["dist/bundle.js", "src/generated/a.py"], mg,
                       added=900, deleted=300)
    check("生成物", d, "AUTO_ALL", "generated_paths")

    # 一混进非生成物就不再是 AUTO_ALL。
    # 而且 dist/ 不在 allowed_paths 里 → 连灰区都不算，必须重拆。
    # 理由：生成物本身可以不看，但"同时改了生成物和手写代码"的提交里，
    # 手写那部分没有被任何人看过就进了主干。
    d = mg_gate.decide(["dist/bundle.js", "src/calc.py"], mg, added=5, deleted=0)
    check("混入手写", d, "NEEDS_REBREAK")

    # ── 3. AUTO_CORE 与它的三个边界 ─────────────────────────────────
    d = mg_gate.decide(["src/calc.py"], mg, added=6, deleted=0)
    check("小改", d, "AUTO_CORE")
    assert d["need"] == "single_sign" and d["merge"] is False, d

    d = mg_gate.decide(["src/a.py", "src/b.py", "src/c.py", "src/d.py",
                        "src/e.py", "src/f.py"], mg, added=10, deleted=0)
    check("6文件", d, "NEEDS_REBREAK", "规模超阈值")

    d = mg_gate.decide(["src/a.py"], mg, added=150, deleted=60)   # net=210 > 200
    check("净增210", d, "NEEDS_REBREAK", "规模超阈值")

    d = mg_gate.decide(["src/a.py"], mg, added=100, deleted=100)  # net=200 恰好
    check("净增200", d, "AUTO_CORE")

    d = mg_gate.decide(["vendor/x.py"], mg, added=1, deleted=0)
    check("出白名单", d, "NEEDS_REBREAK", "allowed_paths")

    d = mg_gate.decide([], mg, added=0, deleted=0)
    check("空改动", d, "NEEDS_REBREAK")

    # ── 4. 灰区批量上限 ────────────────────────────────────────────
    d = mg_gate.decide(["src/a.py"], mg, added=1, deleted=0, gray_in_batch=10)
    check("灰区10", d, "AUTO_CORE")
    d = mg_gate.decide(["src/a.py"], mg, added=1, deleted=0, gray_in_batch=11)
    check("灰区11", d, "GRAY_OVERFLOW", "gray_batch_max")

    # ── 5. approved 只兑现签名，不改变分支归类 ─────────────────────
    d = mg_gate.decide(["src/a.py"], mg, added=1, deleted=0, approved=True)
    assert d["branch"] == "AUTO_CORE" and d["merge"] is True, d
    d = mg_gate.decide(["requirements.txt"], mg, added=1, deleted=0, approved=True)
    assert d["branch"] == "NEVER_WAIVE" and d["merge"] is True, d
    print("  ok   approved     只放行已签，不改分支")

    # ── 6. 未知类别必须报错，不能静默放行 ──────────────────────────
    bad = dict(mg)
    bad["never_waive"] = list(mg["never_waive"]) + ["typo_category"]
    try:
        mg_gate.decide(["src/a.py"], bad, added=1, deleted=0)
        FAILS.append("未知 never_waive 类别未报错")
        print("  FAIL 未知类别    静默通过")
    except ValueError as e:
        print("  ok   未知类别     %s" % str(e)[:60])

    # ── 7. 段边界：不能把 migrations_old 当 migrations ─────────────
    d = mg_gate.decide(["src/migrations_old/x.py"], mg, added=1, deleted=0)
    if d["branch"] == "NEVER_WAIVE":
        FAILS.append("migrations_old 被误判为 db_migration")
        print("  FAIL 段边界       migrations_old 误判")
    else:
        print("  ok   段边界       migrations_old 不误判")

    print()
    if FAILS:
        print("FAILED %d case(s):" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("test_mg_gate: PASS (all cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
