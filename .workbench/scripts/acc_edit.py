"""改一份 acceptance.draft.yaml 的验收命令，并重算 digest。

为什么需要这个小工具：`task-new` 只能生成草稿，草稿生成之后要改验收命令
就得手改 YAML —— 而手改之后 digest 必然不符，`task-approve` 会拒（return 9）。
提示语写「删掉 digest 行再重算」，但重算本身没有命令入口。

这个脚本把"改字段 + 重算 digest"合成一步，避免出现
「改了但忘了重算」和「重算了但算错」两种中间态。

用法：
  python .workbench/scripts/acc_edit.py --task T-0002 \
      --cmd "python tests/test_contract_calc_v2.py"
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wb_core            # noqa: E402
import verify_harness     # noqa: E402

WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
TASKS = os.path.join(WB, "tasks")


def to_yaml(acc: dict) -> str:
    """与 lead._acc_to_yaml 同一份输出格式。

    复制而不是 import：lead.py 是控制面入口，import 它会在导入期跑
    模块级的东西（路径解析、cfg 读取）。让一个改 yaml 的小工具有副作用
    是得不偿失的。两边格式若漂移，`gen_spec` 与 `spec_lint` 都看不见 ——
    所以这里加了 `--verify-roundtrip` 断言读回来一致。
    """
    L = ["# acceptance.yaml — 由 lead.py / acc_edit.py 生成，可编辑后需重算 digest",
         "schema: %d" % acc.get("schema", 1),
         "task: %s" % acc["task"],
         "source: %s" % acc.get("source", "lead_draft_human_approved"),
         "on_fail: %s" % acc.get("on_fail", "fail_task"),
         'digest: "%s"' % acc["digest"],
         "scope:"]
    for s in acc.get("scope", []):
        L.append('  - "%s"' % s)
    if acc.get("depends_on"):
        L.append("depends_on:")
        for d in acc["depends_on"]:
            L.append('  - "%s"' % d)
    L.append("commands:")
    for c in acc["commands"]:
        L.append("  - id: %s" % c["id"])
        L.append('    cmd: "%s"' % c["cmd"].replace('"', '\\"'))
        L.append('    cwd: "%s"' % c.get("cwd", "."))
        L.append("    timeout_sec: %d" % c.get("timeout_sec", 120))
        L.append("    expect: {exit_code: %d}" % (c.get("expect") or {}).get("exit_code", 0))
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--cmd", action="append", dest="cmds",
                    help="替换验收命令，可重复（每条生成一个 A<n>）")
    ap.add_argument("--scope", action="append", dest="scopes")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--draft", action="store_true", help="改 draft（默认）")
    ap.add_argument("--final", action="store_true",
                   help="（已停用）改已转正的 acceptance.yaml")
    a = ap.parse_args(argv)

    # §6.2 / D-53：--final 曾是孤儿入口 —— 无签名即可改写已转正的
    # acceptance.yaml 并重算文件内 digest，使 verify 的文件内比对形同虚设。
    # 保留参数但明确拒绝，比直接删掉好：删掉只会给一句
    # "unrecognized arguments"，照旧命令敲的人不知道该改走哪条路。
    if a.final:
        raise SystemExit("REFUSE --final：已转正的 acceptance.yaml 不可改写（§6.2 / D-53）。"
                         "转正后改验收标准会让链上 E32 与文件脱节；确需变更请走 "
                         "human_amend_task 重新备料 + 人工转正，不要就地改文件。")

    name = "acceptance.draft.yaml"
    p = os.path.join(TASKS, a.task, name)
    if not os.path.exists(p):
        raise SystemExit("not found: %s" % p)
    acc = wb_core.yaml_parse_file(p)

    if a.cmds:
        acc["commands"] = [
            {"id": "A%d" % (i + 1), "cmd": c, "cwd": ".",
             "timeout_sec": a.timeout, "expect": {"exit_code": 0}}
            for i, c in enumerate(a.cmds)]
    if a.scopes:
        acc["scope"] = a.scopes

    # digest 必须最后算：它覆盖除 digest 自身以外的全部字段。
    acc.pop("digest", None)
    acc["digest"] = verify_harness.canonical_digest(acc)

    body = to_yaml(acc)
    back = wb_core.yaml_parse(body)
    if verify_harness.canonical_digest(back) != acc["digest"]:
        raise SystemExit("roundtrip digest mismatch: 输出格式与解析器不一致，"
                         "写下去会生成一份读回来就变形的文件")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    print("%s updated: %d command(s) digest=%s"
          % (name, len(acc["commands"]), acc["digest"]))
    for c in acc["commands"]:
        print("  %s  %s" % (c["id"], c["cmd"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
