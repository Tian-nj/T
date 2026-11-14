# -*- coding: utf-8 -*-
"""silent_swallow_probe.py —— 「吞掉异常且一句话没说」的探针（第四十四轮/第四十五轮）。

## 判据为什么收得这么窄

第一版把「处理器里没有任何可观测动作」当成判据，结果报了 48 条，其中
`bad("K9", ...)`、`out["error"] = ...`、`return False, "..."` 全被判成静默 ——
它们明明把异常写进了返回值或失败清单。**尺子做错比没尺子更坏**：48 条假红
会让人很快学会忽略这个探针。

收窄后只认一种：处理器体里**只有** `pass` / `continue`（允许 docstring）。
这种写法在任何解释下都没有留下痕迹，且不可能被误读成「留了痕」。

## 三处作用域，不是一处（第四十五轮）

第四十四轮只扫 `.workbench/scripts`（110 个处理器），仓根与 `tests/` 里的
静默处理器全在视野外 —— 实测仓根 2 条、`tests/` 4 条。**判据的覆盖范围
本身也必须可验**：`scope_audit()` 把「扫描到的文件集」与「该作用域的实际
文件集」逐一对账，新增目录/漏 join 都会红，否则下一次扩目录时会再漏一次。

## 定位方式：作用域内键 + 序号，不是行号

行号会随任何一次编辑漂移。第一版用 `文件:行号` 当 key，加完一行注释后整个
白名单立刻变成 stale（9 条全烂）。改成「作用域内相对路径#该文件里第几个
Exception 处理器」：插入新处理器会移动序号，但那种改动本来就应该重新看一遍
白名单。

## 白名单不是豁免通道

每条要写 why。本文件在 root.manifest 的 COVER 内，改白名单 = 改可信根 =
一次可观测的提交。
"""
import ast
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
SCRIPTS = os.path.join(WB, "scripts")

SCHEMA = 2
CRITERION = "silent_swallow_audit_v1"

# 扫哪几处。值是相对 ROOT 的前缀，键是给人看的名字。
# 只列目录或「目录 + 一层 *.py」，不递归：递归会把 wt/ 下的整份仓库副本
# 算成第二份源码，于是判据从「本仓没有静默」变成「磁盘任何角落都没有」。
SCAN_SCOPES = (
    ("scripts", os.path.join(".workbench", "scripts")),
    ("root", "."),
    ("tests", "tests"),
)

# 已核对过、允许静默的处理器。
# key = "<作用域>:<相对该作用域的路径>#<该文件内第几个 Exception 处理器>"
#
# 判据只有两条，缺一条就不该进这张表：
#   A. 异常发生时**没有需要传达的信息**（清理失败本来就不该影响结论）；
#   B. 或者信息已经被别处可观测地承载（返回值、产物文件、上层判据）。
ALLOWED = {
    # --- 资源清理：失败不影响结论，且清理失败没有可传达的信息 ---
    "scripts:appcontainer_probe.py#6": "DeleteProcThreadAttributeList 回收失败；引导已完成或已抛，进程即将退出，句柄由 OS 回收",
    "scripts:appcontainer_probe.py#8": "测试用 HTTP server 关闭失败；端口随进程退出释放，结论已写入证据文件",
    "scripts:job_object_probe.py#1": "TerminateJobObject 回收子进程，失败时子进程会在探针超时后被杀；结论不依赖它",
    "scripts:job_object_probe.py#2": "CloseHandle 回收句柄，进程退出时 OS 兜底",
    "scripts:job_object_probe.py#4": "loopback 监听桩的 accept 清理；连接数上限与结论都不依赖它",
    "scripts:job_object_probe.py#6": "listener.close 清理；同 #4",
    "scripts:signature_backend.py#1": "BCryptCloseAlgorithmProvider 回收 provider；失败时句柄随进程退出，签名结果已在返回值里",
    "scripts:signature_backend.py#2": "BCryptDestroyKey 回收密钥句柄；同 #1",
    # --- 探测：失败本身就是结论，且产物文件承载了它 ---
    "scripts:capability_escape_child.py#1": "探针正是在测 ctypes 能否写文件；写失败 = 没逃逸，产物 via-ctypes.txt 不存在即证据",
    # --- 信息在别处可见 ---
    # 第五十三轮：repeat_min_count_conflict() 新增了一个有痕 except（失败时把
    # mem 置空，不是 pass），它排在告警上报处理器之前，于是那条由 #5 变 #6。
    # 序号漂移是有意义的红：逼人确认新处理器不吞异常 —— 这里确认它把失败
    # 写进了返回值，所以只改序号，不改判据。
    "scripts:dream_consolidation.py#6": "告警上报失败时，紧接着 return {\"ok\": False, \"error\": ...} 把同一个异常交给了调用方",
    # 第四十九轮：longrun_risks 里新增了一个**有痕**的 except（写进 risk 条目），
    # 它排在前面，于是 selftest 那个 keygen 的序号由 #1 变 #2。
    # 第五十三轮：longrun_risks 里又加了一个有痕 except（记忆台账读不动时写
    # risk 条目），序号再由 #2 变 #3。两次都确认新增处理器不吞异常。
    # 序号漂移是有意义的红：它逼人来看一眼「是不是多了个处理器」——
    # 这次确认新增的那个不吞异常，所以只改序号，不改判据。
    # 第五十九轮：working_capacity_unreadable 新增一个**有痕** except（把异常
    # 写进 risk 条目），排在 selftest keygen 之前，序号由 #3 变 #4。确认它
    # 不吞异常，只改序号。
    "scripts:scheduler_observe.py#4": "selftest 里 keygen 失败是预期（非 Windows 无 CNG），后面走 algo=none 分支并在结论里体现",
    # --- 第四十五轮新增：仓根与 tests ---
    "root:run_task.py#2": "读不到 batch_max_parallel 就落进下一个分支做纯文本解析，那是真正的兜底路径（不是静默降级）",
    "tests:test_containment_telemetry.py#2": "夹具 finally 里 kill 之后的 wait 清理；被测对象已经结束，清理失败无信息",
    "tests:test_spawn_containment.py#2": "同上：kill 之后的 wait 清理",
    "tests:test_scheduler_observe.py#1": "夹具现造临时密钥，造不出就走 algo=none，verify 会判红 —— 那是真实结果，不许绕",
    "tests:test_telemetry_aggregate.py#1": "同上：夹具现造密钥失败即走 algo=none 分支",
}


def _scope_key(scope, name, ordinal):
    return "%s:%s#%d" % (scope, name, ordinal)


def _is_silent(handler):
    """处理器体里只有 pass / continue（可带 docstring）→ 静默。"""
    for stmt in handler.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Continue):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
                and isinstance(stmt.value.value, str):
            continue                       # docstring
        return False
    return True


def _scope_files(base):
    """该作用域下要看的 .py 文件（只一层，不递归）。"""
    if not os.path.isdir(base):
        return []
    return sorted(n for n in os.listdir(base)
                  if n.endswith(".py") and os.path.isfile(os.path.join(base, n)))


def scan_scope(scope, base):
    """扫一处，返回 (findings, scanned, files)。"""
    findings = []
    scanned = 0
    files = _scope_files(base)
    for name in files:
        path = os.path.join(base, name)
        try:
            with io.open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src)
        except (OSError, SyntaxError) as exc:
            findings.append({"scope": scope, "file": name, "ordinal": 0,
                             "line": 0, "kind": "unparsable",
                             "detail": "%s: %s" % (type(exc).__name__, exc)})
            continue
        handlers = [n for n in ast.walk(tree)
                    if isinstance(n, ast.ExceptHandler)]
        handlers.sort(key=lambda n: (n.lineno, n.col_offset))
        ordinal = 0
        for node in handlers:
            typ = node.type
            tname = ""
            if isinstance(typ, ast.Name):
                tname = typ.id
            elif isinstance(typ, ast.Attribute):
                tname = typ.attr
            if tname not in ("Exception", "BaseException"):
                continue
            ordinal += 1
            scanned += 1
            if not _is_silent(node):
                continue
            findings.append({
                "scope": scope,
                "file": name,
                "ordinal": ordinal,
                "line": node.lineno,
                "kind": "silent_swallow",
                "detail": ",".join(type(s).__name__ for s in node.body)[:60],
            })
    return findings, scanned, files


def scan(only=None):
    """扫所有作用域。返回 (findings, scanned, per_scope)。

    `only` 传作用域名可只扫一处（测试用）。
    """
    findings = []
    scanned = 0
    per_scope = {}
    for scope, rel in SCAN_SCOPES:
        if only is not None and scope != only:
            continue
        base = os.path.join(ROOT, rel) if rel != "." else ROOT
        f, n, files = scan_scope(scope, base)
        findings.extend(f)
        scanned += n
        per_scope[scope] = {"files": files, "scanned": n,
                            "silent": len(f)}
    return findings, scanned, per_scope


def scope_audit(per_scope=None):
    """判据的覆盖范围本身要对得上账。

    检查两件事：
      1. 每个声明的作用域目录都存在（漏建 = 判据悄悄变窄）；
      2. 每个作用域至少真的读到文件（空目录会让 scanned 归零而 VERDICT 仍是 CLEAN,
         ——「通过了」与「没跑」在输出上等价的另一个形态）。
    """
    if per_scope is None:
        _f, _n, per_scope = scan()
    problems = []
    for scope, rel in SCAN_SCOPES:
        base = os.path.join(ROOT, rel) if rel != "." else ROOT
        if not os.path.isdir(base):
            problems.append("%s: 目录不存在（%s）" % (scope, rel))
            continue
        got = per_scope.get(scope) or {}
        want = _scope_files(base)
        if list(got.get("files") or []) != want:
            problems.append("%s: 扫描文件集与实际不一致" % scope)
        if not want:
            problems.append("%s: 一个 .py 都没扫到" % scope)
    return problems


def stale_allowlist(findings, per_scope=None):
    live = {_scope_key(f.get("scope", ""), f["file"], f["ordinal"])
            for f in findings}
    if per_scope is None:
        _f, _n, per_scope = scan()
    stale = []
    for key in ALLOWED:
        head = key.split(":")[0]
        rel_map = dict(SCAN_SCOPES)
        if head not in rel_map:
            stale.append({"key": key, "why": "作用域名不在 SCAN_SCOPES 里"})
            continue
        rel = rel_map[head]
        base = os.path.join(ROOT, rel) if rel != "." else ROOT
        fn = key.split(":", 1)[1].split("#")[0]
        if not os.path.isfile(os.path.join(base, fn)):
            stale.append({"key": key, "why": "文件不存在"})
        elif key not in live:
            stale.append({"key": key, "why": "该处理器已不再静默，或序号已移动"})
    return stale


def report(only=None):
    findings, scanned, per_scope = scan(only=only)
    stale = stale_allowlist(findings, per_scope)
    scope_problems = scope_audit(per_scope) if only is None else []
    unallowed = [f for f in findings
                 if _scope_key(f.get("scope", ""), f["file"], f["ordinal"])
                 not in ALLOWED]
    return {
        "criterion": CRITERION,
        "schema": SCHEMA,
        "scopes": [s for s, _r in SCAN_SCOPES],
        "per_scope": {k: {"files": len(v["files"]), "scanned": v["scanned"],
                          "silent": v["silent"]}
                      for k, v in per_scope.items()},
        "scanned": scanned,
        "allowed": len(ALLOWED),
        "silent_total": len(findings),
        "findings": unallowed,
        "stale_allowlist": stale,
        "scope_problems": scope_problems,
        "verdict": ("SILENT_SWALLOW_CLEAN"
                    if not unallowed and not stale and not scope_problems
                    else "SILENT_SWALLOW_FINDINGS"),
    }


def check():
    rep = report()
    for f in rep["findings"]:
        print("  [silent] %s:%s#%d (line %d)  %s"
              % (f["scope"], f["file"], f["ordinal"], f["line"], f["detail"]))
    for s in rep["stale_allowlist"]:
        print("  [stale ] %s  %s" % (s["key"], s["why"]))
    for p in rep["scope_problems"]:
        print("  [scope ] %s" % p)
    print("scopes=%s" % ",".join("%s(%d files/%d handlers/%d silent)"
                                 % (k, v["files"], v["scanned"], v["silent"])
                                 for k, v in rep["per_scope"].items()))
    print("scanned=%d silent_total=%d allowed=%d unallowed=%d stale=%d scope_problems=%d"
          % (rep["scanned"], rep["silent_total"], rep["allowed"],
             len(rep["findings"]), len(rep["stale_allowlist"]),
             len(rep["scope_problems"])))
    print("VERDICT %s" % rep["verdict"])
    return 0 if rep["verdict"] == "SILENT_SWALLOW_CLEAN" else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--json" in argv:
        print(json.dumps(report(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return check()


if __name__ == "__main__":
    sys.exit(main())
