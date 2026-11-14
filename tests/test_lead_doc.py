"""T-R40 的验收基线：主理人自己动手留下的产物。

验收标准由「要东西的人」写、在任务开始前就进主干；执行体只允许让这个文件
通过，不允许改它。基线指向一个**现在还不存在**的产物文件：派活前不绿，
只有 lead 执行体真写了 docs/architecture/T-R40-lead-dispatch.md 才会绿。

与 T-R38 的 test_greet.py 同一条纪律：不复用现成模块，否则「验收通过」与
「这次真的写了东西」脱钩，派活成功会退化成「什么都没发生也成功」。
"""
import io
import os
import sys

DOC = os.path.join("docs", "architecture", "T-R40-lead-dispatch.md")


def main() -> int:
    assert os.path.isfile(DOC), "主理人产物不存在：%s" % DOC
    with io.open(DOC, encoding="utf-8") as fh:
        body = fh.read()
    assert "主理人自己动手" in body, "产物缺少身份标记"
    assert "role: lead" in body, "产物缺少 lead 角色记录"
    assert "E05" in body and "E15" in body, "产物缺少链路证据清单"
    print("test_lead_doc: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
