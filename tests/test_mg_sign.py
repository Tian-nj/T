"""MG 闸门的签名门槛：单人签 / 双签 / 免签 三种兑现。

用户故事（真跑的）：T-0002 改的是 `contracts/calc-v2.md`，闸门正确判成
`NEVER_WAIVE / double_sign`；然后 `lead.py` 签了一次 `human_approve`
（`yg`），`merge_queue` 就把它合了。

根因：`approved_of()` 返回**布尔**，`decide()` 拿它当 `approved=` 参数，
而 `mg_gate.decide` 里 `merge = bool(approved)` —— `single_sign` 与
`double_sign` 走到同一个表达式。所以"双签"这个词只出现在打印里，
从来没有出现在判定里。

修法：`approved_of()` 返回**principal 集合**，`gate_of()` 用 probe 两遍
（第一遍拿 need，第二遍带 need_met 算 merge）。这里穷举三种门槛。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), ".workbench", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "..", ""))

import merge_queue as mq  # noqa: E402


def t(name, got, want):
    ok = got == want
    print("%-46s %s  got=%r want=%r" % (name, "ok" if ok else "FAIL", got, want))
    return ok


def main():
    r = []
    # 门槛算术
    r.append(t("none / 0 人", mq.needs_met("none", set()), True))
    r.append(t("single / 0 人", mq.needs_met("single_sign", set()), False))
    r.append(t("single / 1 人", mq.needs_met("single_sign", {"yg"}), True))
    r.append(t("double / 1 人", mq.needs_met("double_sign", {"yg"}), False))
    r.append(t("double / 2 人", mq.needs_met("double_sign", {"yg", "ops"}), True))
    # 同一个人两次不算两个人 —— 传入的是集合，重复自动合并
    r.append(t("double / 同一人重复", mq.needs_met("double_sign", {"yg", "yg"}), False))
    r.append(t("rebreak 任何人数", mq.needs_met("rebreak", {"a", "b", "c"}), False))

    # 与 mg_gate.decide 的联动：NEVER_WAIVE 必须双签才 merge
    mg = {"max_file_count": 5, "max_net_line_count": 200, "gray_batch_max": 10,
          "allowed_paths": ["src/**", "tests/**", "docs/**"],
          "generated_paths": ["dist/**"],
          "never_waive": ["contracts", "dependencies", "cicd", "credentials",
                          "db_migration", "file_delete", "git_force", "release",
                          "env_vars", "high_risk_script", "permissions",
                          "manifest_change"]}
    d = mq.mg_gate.decide(["contracts/x.md"], mg, approved=False)
    r.append(t("NEVER_WAIVE 0 签 → 不合", d["merge"], False))
    r.append(t("NEVER_WAIVE 0 签 → 要双签", d["need"], "double_sign"))
    d = mq.mg_gate.decide(["contracts/x.md"], mg, approved=True)
    r.append(t("NEVER_WAIVE 已签 → 可合", d["merge"], True))

    d = mq.mg_gate.decide(["src/a.py"], mg, added=5, deleted=1, approved=False)
    r.append(t("AUTO_CORE 0 签 → 不合", d["merge"], False))
    r.append(t("AUTO_CORE 0 签 → 单签", d["need"], "single_sign"))
    d = mq.mg_gate.decide(["src/a.py"], mg, added=5, deleted=1, approved=True)
    r.append(t("AUTO_CORE 已签 → 可合", d["merge"], True))

    d = mq.mg_gate.decide(["dist/bundle.js"], mg, added=9999, deleted=0)
    r.append(t("AUTO_ALL 免签即合", (d["branch"], d["merge"], d["need"]),
               ("AUTO_ALL", True, "none")))

    ok = all(r)
    print("\nmg_sign: %s (%d/%d)" % ("PASS" if ok else "FAIL", sum(r), len(r)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
