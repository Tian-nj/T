"""audit_payload.py — 逐调用点核对：每个 append_event 传的 payload 是否满足 required_payload。

这是"加校验前先量伤害"的步骤。append_event 现在会拒收缺必填键的写入，
如果不先核对，run.sh 会在某个中间步骤突然炸，而炸点看起来像别的问题。

2026-11-15 round135：main() 原先恒 return 0 —— 算出 problems 却不判红，
README 因此常年写「0 处问题」而实跑有 2 处。现改为 bad>0 就非零，并加
持久化负向验证 selftest()：每次跑都合成一条缺 purpose_id 的调用点，
证明 gate 真的会拦，而不是只靠人工记得注入一次。
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SCRIPTS = ".workbench/scripts"
SPEC = ".workbench/spec/events.schema.json"

data = json.load(open(SPEC, encoding="utf-8"))
KIND2ID = {}
REQ = {e["id"]: list(e.get("required_payload") or []) for e in data["events"]}
RETIRED = set(data.get("retired_ids") or [])
for e in data["events"]:
    if e["id"] in RETIRED:
        continue
    KIND2ID[e["kind"]] = e["id"]


def keys_of(node):
    """从 Dict 字面量取固定键；遇到 **splat 或非字面量返回 None（表示不确定）。"""
    if not isinstance(node, ast.Dict):
        return None
    out = []
    for k in node.keys:
        if k is None:
            return None            # **splat
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out.append(k.value)
        else:
            return None
    return out


def kind_of(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def main() -> int:
    print("=" * 78)
    print("payload 必填键核对 audit_payload.py")
    print("=" * 78)
    print()

    rows = []
    for fn in sorted(f for f in os.listdir(SCRIPTS) if f.endswith(".py")):
        p = os.path.join(SCRIPTS, fn)
        try:
            tree = ast.parse(open(p, encoding="utf-8").read())
        except SyntaxError as e:
            print("  SKIP %s: %s" % (fn, e))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not isinstance(f, ast.Attribute) or f.attr != "append_event":
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            pos = node.args
            kind = kind_of(pos[0]) if len(pos) > 0 else kind_of(kw.get("kind"))
            payload_node = pos[1] if len(pos) > 1 else kw.get("payload")
            rows.append((fn, node.lineno, kind, keys_of(payload_node) if payload_node else None))

    problems = 0
    for fn, lineno, kind, keys in sorted(rows, key=lambda r: (r[0], r[1])):
        if kind is None:
            print("  %-22s :%-5d  (动态 kind，无法静态判定)" % (fn, lineno))
            continue
        if kind not in KIND2ID:
            print("  %-22s :%-5d  %-20s  ← kind 不在活动事件表（退役或未知）" % (fn, lineno, kind))
            problems += 1
            continue
        need = REQ[KIND2ID[kind]]
        if keys is None:
            status = "payload 非字面量 → 运行期校验"
        else:
            miss = [k for k in need if k not in keys]
            if miss:
                status = "缺 %r" % miss
                problems += 1
            else:
                status = "ok"
        print("  %-22s :%-5d  %-20s  %s" % (fn, lineno, kind, status))

    print()
    print("-" * 78)
    print("调用点 %d 个，静态可见的问题 %d 处" % (len(rows), problems))
    print("（payload 非字面量的调用点由运行期 SpecViolation 兜底，属预期）")
    return 0 if problems == 0 else 1


def selftest() -> int:
    """负向验证：合成一条缺 purpose_id 的调用点，gate 必须返回非零。

    走的是与真实审计完全相同的 main() 路径，只把 SCRIPTS 临时指向一个
    只含探针文件的目录；用完还原，不落盘仓库、不污染真实结论。
    """
    import contextlib
    import shutil
    import tempfile
    global SCRIPTS
    saved = SCRIPTS
    tmp = tempfile.mkdtemp(prefix="payload_gate_")
    try:
        with open(os.path.join(tmp, "probe.py"), "w", encoding="utf-8") as fh:
            fh.write("def f(store):\n"
                     "    store.append_event('purpose_add', {'note': 'x'}, "
                     "'companion', None)\n")
        SCRIPTS = tmp
        # 静默 main() 的逐行输出：否则「静态可见的问题 1 处」会与真实审计段
        # 的「0 处」同形并排出现 —— 假的红不能长得像真的红（round134 的教训）。
        # 这里只取它的退出码，负向结论用下面一行带明确标记的话单独说明。
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            injected = main()
    finally:
        SCRIPTS = saved
        shutil.rmtree(tmp, ignore_errors=True)
    ok = injected == 1
    print("gate selftest（合成缺 purpose_id 的调用点，不计入真实结论）: %s（注入缺陷 rc=%d，期望 1）"
          % ("PASS" if ok else "FAIL", injected))
    return 0 if ok else 1


if __name__ == "__main__":
    rc = main()
    gate = selftest()
    # 真实审计发现缺陷、或 gate 自身失效，都必须判红
    raise SystemExit(rc if rc != 0 else gate)
