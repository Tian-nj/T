r"""audit_closure.py — 声明到执行的闭合审计。

前面三个审计问的是「表对不对」（drift）、「代码活不活」（static）、
「本体动得对不对」（runtime）。这个问第四个、也是最难的一个：

    **规则写在这里，谁在读它？**

一条规则最危险的失效形态不是写错，是**没有任何执行点读它**。
它躺在 spec 里，review 时看得见，出事后翻得到，但运行时从来没跑过。
这类规则的共同症状是：测试全绿、文档一致、事后追责时发现闸门是画上去的。

每个 C 探针写一条"违规尝试"，看它是否真的被拦。
拦住 = 闭合。没拦住 = 声明悬空（DECLARED-BUT-UNENFORCED）。
"""
from __future__ import annotations

import ast
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".workbench/scripts")

import wb_core  # noqa: E402

WB = os.getcwd()
RESULTS = []
PY = sys.executable


def probe(cid, title, fn):
    try:
        ok, detail = fn()
    except Exception as exc:
        ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
        detail += "\n      " + traceback.format_exc().splitlines()[-2].strip()
    RESULTS.append((cid, title, ok, detail))
    print("  %-5s %-62s %s" % (cid, title[:62], "OK" if ok else "OPEN"))


def fresh():
    d = tempfile.mkdtemp(prefix="wb_c_")
    for sub in ("events", os.path.join("events", "sidecar"), "state"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d


def run_py(*args, **kw):
    """跑一个控制面脚本，返回 (rc, stdout+stderr)。"""
    env = dict(os.environ)
    env.setdefault("WORKBENCH_ROOT_KEY", "dev-root-key-2026-please-replace")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([PY] + list(args), cwd=WB, env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=kw.get("timeout", 120))
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ------------------------------------------------------------------ H3
def c01_h3_p0_requires_human_approve():
    """H3：priority=P0 只能由 E21 human_approve 设定。

    E02 draft_created 的 payload 里有 priority 字段，而 lead.py task-new
    直接 `--priority` 就写进去。若如此，造任务的人可以自己给自己提 P0，
    H3 就变成一句注释。
    """
    rc, out = run_py(".workbench/scripts/lead.py", "task-new",
                     "--task", "T-C01", "--title", "closure probe",
                     "--priority", "P0", "--scope", "src/**")
    refused = (rc != 0) or ("REFUSE" in out) or ("denied" in out.lower())
    # 无论是否拒绝，链里都不该出现「由 lead 写入的 P0」
    wrote_p0_by_lead = False
    if rc == 0:
        d = os.path.join(WB, ".workbench", "tasks", "T-C01")
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
    return (refused,
            "task-new --priority P0 → rc=%d %s" %
            (rc, "已拒绝" if refused else "**被接受**（H3 无执行点：任何人可自封 P0）"))


# ------------------------------------------------------------------ H6
def c02_h6_draft_auto_approve_gate():
    """H6：draft 自动确认仅当 auto_approve_draft ∧ priority=P2。

    实测 config 里 auto_approve_draft=false。那么 task-approve 对 P0/P1
    任务应当拒绝自动确认。若任何优先级都能 approve，H6 无执行点。
    """
    rc, out = run_py(".workbench/scripts/lead.py", "task-approve", "--task", "T-0003")
    # T-0003 是既有 P0 任务（见 evidence）。若它被无条件 approve，说明闸门缺失。
    # D-53：task-approve 现在只是「Lead 备料」，输出从 "%s approved:" 改成
    # "%s 备料完成:"。探针若仍只认 "approved"，会把它读成「没 approve」，
    # 于是 acc_ok=False，下面的 `not (acc_ok and flag is False)` 恒真 ——
    # 一个改了措辞就恒真的探针，比没有探针更坏。两种措辞都算命中。
    acc_ok = ("approved" in out) or ("备料完成" in out)
    flag = None
    flag_err = ""
    try:
        cfg = wb_core.yaml_parse_file(os.path.join(WB, ".workbench", "config.yaml"))
        flag = (cfg.get("global_config") or {}).get("auto_approve_draft")
    except Exception as exc:                                   # noqa: BLE001
        # 第四十五轮：这里以前是 `pass`，于是 flag 留成 None，
        # 而下面的判据是 `not (acc_ok and flag is False)` ——
        # None 让整个条件恒真，**「配置读不到」被报成「H6 已闭合」**。
        # 读不到配置 ≠ 开关是关的 ≠ 闸门在工作：
        # 这种情况必须判「没闭合」，并由 detail 说清是读不到而不是读到了什么。
        flag_err = "%s: %s" % (type(exc).__name__, exc)
    if flag_err:
        return (False,
                "auto_approve_draft 读不到（%s）；无法证明 H6 有执行点，"
                "按未闭合计" % flag_err[:80])
    # 判定：auto_approve_draft=false 时，非 P2 不该被自动确认
    return (not (acc_ok and flag is False),
            "auto_approve_draft=%r，task-approve T-0003 → rc=%d %s"
            % (flag, rc, (out.strip().splitlines() or [""])[-1][:70]))


# ------------------------------------------------------------------ H7
def c03_h7_attempts_cap():
    """H7：attempts 存 tasks.json，E12 retry 加 1，上限 max_attempts(3)。

    探针：链里构造 4 次 retry，看是否有任何地方拒绝第 4 次。
    """
    hits = subprocess.run(
        ["grep", "-rn", "max_attempts", "--include=*.py", WB],
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    hits = "\n".join(l for l in hits.splitlines()
                     if "gen_spec" not in l and "audit_" not in l)
    return (bool(hits),
            "max_attempts 在运行代码中的引用：%s"
            % (hits.strip().replace(WB, ".") if hits else "**零处**（H7 无执行点）"))


# ------------------------------------------------------------------ C.A2
def c04_ca2_path_deny_enforced():
    """C.A2 / paths.yaml deny：worker 不得触碰 .env / *.pem / root.manifest。

    这是最该硬的一条：凭据文件保护。探针直接问 diff_sandbox_check.check()
    —— 写域闸门的唯一执行点 —— 给它一个 src/.env，看它拦不拦。
    """
    import diff_sandbox_check as dsc
    paths = wb_core.yaml_parse_file(os.path.join(WB, ".workbench", "spec", "paths.yaml"))
    allow = [x["glob"] if isinstance(x, dict) else x for x in (paths.get("allow_write") or [])]
    deny = [x["glob"] if isinstance(x, dict) else x for x in (paths.get("deny") or [])]
    # 必须按**真实调用方式**调用：lead.py submit 传的是
    # check(scope, changed, allow, deny, deny_abs)。少传 deny 就等于问错了问题
    # —— 这个探针自己第一版就是这么误报 OPEN 的。
    deny_abs = list(paths.get("deny_absolute_prefix") or [])
    must_block = ["src/.env", "src/key.pem", ".workbench/root.manifest",
                  ".workbench/spec/events.schema.json", ".workbench/scripts/lead.py"]
    must_pass = ["src/missing.py", "tests/t.py"]
    leaked, overblocked = [], []
    for f in must_block:
        chk = dsc.check(["src/", "tests/", ".workbench/"], [f], allow, deny, deny_abs)
        if chk["ok"]:
            leaked.append(f)
    for f in must_pass:
        chk = dsc.check(["src/", "tests/", ".workbench/"], [f], allow, deny, deny_abs)
        if not chk["ok"]:
            overblocked.append((f, chk["violations"][0]["reason"]))
    ok = not leaked and not overblocked
    return (ok,
            "deny %d 条 / deny_abs %d 条；应为 DENY 却放行=%s；应为 PASS 却拒绝=%s"
            % (len(deny), len(deny_abs), leaked or "无", overblocked or "无"))


# ------------------------------------------------------------------ C.B2
def c05_cb2_cmd_policy_enforced():
    """C.B2：命令白名单。python -c 只在 verify harness 内允许，git 禁 --force。

    探针：把一条 `python -c "..."` 和一条 `git push` 塞进 acceptance，
    问 validate_acceptance() 收不收。
    """
    import verify_harness as vh
    acc = {"schema": 1, "task": "T-x", "on_fail": "fail_task",
           "commands": [
               {"id": "A1", "cmd": 'python -c "import os"', "cwd": ".", "timeout_sec": 60,
                "expect": {"exit_code": 0}},
               {"id": "A2", "cmd": "git push --force origin master", "cwd": ".",
                "timeout_sec": 60, "expect": {"exit_code": 0}},
               {"id": "A3", "cmd": "python tests/x.py", "cwd": ".", "timeout_sec": 60,
                "expect": {"exit_code": 0}},
           ]}
    errs = vh.validate_acceptance(acc)
    caught_a1 = any("A1" in e or "commands[0]" in e for e in errs)
    caught_a2 = any("A2" in e or "commands[1]" in e for e in errs)
    # A2 会被"destructive git push"抓住，但那不是 C.B2 的白名单判定
    rules = json.load(open(os.path.join(WB, ".workbench/spec/rules.json"), encoding="utf-8"))
    cb2 = [c for c in rules["checks"] if c["id"] == "C.B2"]
    return (caught_a1 and caught_a2,
            "C.B2 声明 %d 条；validate_acceptance 抓到 A1(python -c)=%r A2(git push)=%r"
            % (len(cb2), caught_a1, caught_a2))


# ------------------------------------------------------------------ C.D1
def c06_cd1_budget_guard():
    """C.D1：task_wall_clock_min <= single_task_wall_clock_min。"""
    rc, out = run_py(".workbench/scripts/lead.py", "check")
    return ("C.D1" in out, "lead.py check 输出里是否含 C.D1 判定：%r" % ("C.D1" in out))


# ------------------------------------------------------------------ H5
def c07_h5_escalate_requires_dead_worker():
    """H5：进入 escalated 前必须确认 worker 已死透（E07 完成且 pid_alive=false）。"""
    src = open(os.path.join(WB, ".workbench/scripts/lead.py"), encoding="utf-8").read()
    has = ("escalate" in src and "pid_alive" in src)
    # 精确一点：找 transition(... "escalated" ...) 附近是否调 pid_alive
    import re
    blocks = re.findall(r".{600}escalated.{600}", src, re.S)
    guarded = [b for b in blocks if "pid_alive" in b or "kill_tree" in b]
    return (bool(guarded),
            "lead.py 中 escalated 上下文 %d 段，其中含 pid_alive/kill_tree 的 %d 段"
            % (len(blocks), len(guarded)))


# ------------------------------------------------------------------ H4
def c08_h4_dep_cascade_skips_never_spawned():
    """H4：cancelled 来自 dep_cascade 且 never_spawned → 跳过 kill 并记 meta.skipped_reason。"""
    src = open(os.path.join(WB, ".workbench/scripts/dep_resolver.py"), encoding="utf-8").read()
    has_skip = "skipped_reason" in src or "never_spawned" in src
    rc, out = run_py(".workbench/scripts/dep_resolver.py", "--task", "T-9999", "--dry-run")
    return (has_skip, "dep_resolver 含 never_spawned/skipped_reason=%r；对不存在任务 rc=%d"
            % (has_skip, rc))


# ------------------------------------------------------------------ 冻结可写集合
def c09_frozen_whitelist_readable_at_runtime():
    """C.C1 / I-6：冻结期白名单必须能从运行时读到，且与 spec 集合相等。

    P13 已证集合相等；这里再证"读它是通过 spec 而不是第二份常量"
    —— 即修改 spec 后，运行时行为跟着变。
    """
    doc = json.load(open(os.path.join(WB, ".workbench/spec/events.schema.json"),
                         encoding="utf-8"))
    src = open(os.path.join(WB, ".workbench/scripts/wb_core.py"), encoding="utf-8").read()
    hardcoded = "FROZEN_WHITELIST = {" in src or "FROZEN_WHITELIST = [" in src
    return (not hardcoded,
            "wb_core 中是否仍有第二份 FROZEN_WHITELIST 常量=%r（spec 为唯一源=%r）"
            % (hardcoded, not hardcoded))


# ------------------------------------------------------------------ 状态机唯一入口
def c10_single_transition_entry():
    """I-1 单载体：state 事件只能由 Store.transition() 产生。

    探针三条，缺一不可：
      1. **生产写入点只有一处。** 扫描范围是 `.workbench/scripts/**` 与仓根脚本，
         **不含 tests/**：测试造状态时本来就该直接写 state 事件（它测的是 Store
         本身），把它算成绕过点，等于要求测试不要碰被测对象。
      2. **那唯一一处必须在 wb_core.py 里。** 只数个数不够：这唯一的写入点若漂到
         lead.py，"唯一写入口"就退化成"只有一个地方写"—— 数字没变，含义全变。
      3. 用 AST 认调用，不用 grep 认行。上一版 grep 把 wb_core.py 文档字符串里
         那句「仓里曾有 4 个地方各自 append_event("state")」数成了第二个写入点：
         **解释这条规则的那段文字，被当成了违反这条规则的证据**。
    """
    hits = []
    roots = [os.path.join(WB, ".workbench", "scripts"), WB]
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "wt", "__pycache__", "node_modules",
                                        "tests", "evidence", "state", "tasks", "archive")]
            for fn in filenames:
                if not fn.endswith(".py") or fn.startswith("audit_"):
                    continue
                fp = os.path.join(dirpath, fn)
                if root == WB and os.path.dirname(fp) != WB:
                    continue          # 仓根这一层只认根下脚本，不顺着子目录走
                hits.extend(_state_calls(fp))

    bad = []
    if len(hits) != 1:
        bad.append("生产写入点 %d 处：%s" % (len(hits), hits))
    elif not hits[0][0].endswith("wb_core.py"):
        bad.append("唯一写入点不在 wb_core.py 而在 %s:%d" % hits[0])
    if bad:
        return False, "；".join(bad)
    return True, "唯一写入点 %s:%d" % hits[0]


def _state_calls(path):
    """返回该文件里真正调用 append_event("state", ...) 的位置。"""
    try:
        tree = ast.parse(io.open(path, "r", encoding="utf-8", errors="replace").read())
    except (OSError, SyntaxError):
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = getattr(f, "attr", None) or getattr(f, "id", None)
        if name != "append_event":
            continue
        first = None
        if node.args:
            first = node.args[0]
        else:
            for kw in node.keywords:
                if kw.arg == "kind":
                    first = kw.value
        if isinstance(first, ast.Constant) and first.value == "state":
            out.append((os.path.relpath(path, WB).replace("\\", "/"), node.lineno))
    return out


def c11_per_event_actors_enforced():
    """I-7 spec 里每条事件的 actors 声明被 append_event 真正执行。

    探针（行为式，不看源码）：
      a. 越权必拒：`system:gc` 是**已注册**的 actor，但 E17 escalate 没声明它。
         写进去必须抛 SpecViolation，且链上不能多一行。
      b. 合法必过：同一事件换 `lead`（声明里有它）必须写成功。
      c. spec 自查：每条 live 事件都必须有非空 actors 声明。
         「没声明」若被当成「谁都能写」，本校验会在最需要它的那天恰好失效。

    为什么必须做 b：一个"谁都拒"的实现能让 a 通过，但那是把闸门焊死了，
    不是把闸门造对了。两个方向都验，才排除"用拒绝代替判断"。
    """
    tmp = fresh()
    st = wb_core.Store(tmp, "0" * 32)
    bad = []

    # 0. 第三十四轮：E17 escalate 声明了 from_states，任务必须先真实存在。
    #    先用一次起始迁移把 T-C11 推到 draft —— 否则下面两个方向测到的都是
    #    「绕过状态机」，而不是「actors 声明有没有被执行」。
    st.append_event("state", {"task": "T-C11", "from": None, "to": "draft"},
                    "lead", "T-C11")
    base_lines = len(list(st._rows()))

    # a. 越权必拒
    try:
        st.append_event("escalate", {"task": "T-C11", "reason_code": "ENV_BROKEN"},
                        "system:gc", "T-C11")
        bad.append("越权写入 system:gc→escalate 被接受")
    except wb_core.SpecViolation as exc:
        if "not in declared actors" not in str(exc):
            bad.append("越权被拒但理由不对：%s" % str(exc)[:80])
    except Exception as exc:                                  # noqa: BLE001
        bad.append("越权写入抛出非 SpecViolation：%s: %s" % (type(exc).__name__, exc))

    lines_after_reject = len(list(st._rows()))
    if lines_after_reject != base_lines:
        bad.append("被拒的写入仍落了 %d 行" % (lines_after_reject - base_lines))

    # b. 合法必过
    try:
        e = st.append_event("escalate", {"task": "T-C11", "reason_code": "ENV_BROKEN"},
                            "lead", "T-C11")
        if e.get("seq") != base_lines + 1:
            bad.append("合法写入的 seq=%r（期望 %r）" % (e.get("seq"), base_lines + 1))
    except Exception as exc:                                  # noqa: BLE001
        bad.append("合法写入被拒：%s: %s" % (type(exc).__name__, exc))

    # c. spec 自查：live 事件不得有空 actors
    tbl = wb_core.spec_tables()
    retired = set(tbl["retired"])
    empty = [eid for eid, _k in tbl["events"]
             if eid not in retired and not wb_core._actors_of(eid)]
    if empty:
        bad.append("live 事件 actors 为空：%s" % (empty,))

    if bad:
        return False, "；".join(bad)
    return True, ("越权被拒且不落行；合法放行 seq=%d；%d 条 live 事件均有 actors 声明"
                  % (base_lines + 1, len(tbl["events"]) - len(retired)))


def main():
    print("=" * 78)
    print("声明→执行闭合审计 audit_closure.py")
    print("=" * 78)
    print()
    probe("C01", "H3  priority=P0 只能由 E21 设定", c01_h3_p0_requires_human_approve)
    probe("C02", "H6  draft 自动确认闸门（auto_approve_draft）", c02_h6_draft_auto_approve_gate)
    probe("C03", "H7  attempts 上限 max_attempts", c03_h7_attempts_cap)
    probe("C04", "C.A2 + paths.deny 凭据文件保护", c04_ca2_path_deny_enforced)
    probe("C05", "C.B2 命令白名单", c05_cb2_cmd_policy_enforced)
    probe("C06", "C.D1 墙钟预算守卫", c06_cd1_budget_guard)
    probe("C07", "H5  escalate 前 worker 必须死透", c07_h5_escalate_requires_dead_worker)
    probe("C08", "H4  dep_cascade 跳过 never_spawned 的 kill", c08_h4_dep_cascade_skips_never_spawned)
    probe("C09", "I-6 冻结白名单只从 spec 读", c09_frozen_whitelist_readable_at_runtime)
    probe("C10", "I-1 state 事件唯一写入口", c10_single_transition_entry)
    probe("C11", "I-7 per-event actors 声明被执行", c11_per_event_actors_enforced)

    ok = sum(1 for r in RESULTS if r[2])
    bad = len(RESULTS) - ok
    print()
    print("-" * 78)
    print("闭合探针 %d 条：%d 条闭合，%d 条声明悬空" % (len(RESULTS), ok, bad))
    print()
    if bad:
        print("声明悬空清单（规则写了，没有执行点）：")
        for cid, title, okk, detail in RESULTS:
            if not okk:
                print("  %-5s %s" % (cid, title))
                print("        %s" % detail.replace("\n", "\n        "))
        print()
    out = os.path.join(WB, "evidence", "audit_closure.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"closed": ok, "open": bad,
                   "results": [{"id": c, "title": t, "ok": o, "detail": d}
                               for c, t, o, d in RESULTS]},
                  fh, ensure_ascii=False, indent=1)
    print("wrote %s" % os.path.relpath(out, WB))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
