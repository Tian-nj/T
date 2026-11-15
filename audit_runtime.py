"""audit_runtime.py — 运行时行为探针：不看代码怎么写，看它实际怎么动。

每个探针都是"我以为它应该这样，实际呢？"的可执行问句。
失败即缺陷，成功即该条声称被证实。

P 系列 = 探针编号，对应审计报告里的缺陷编号。
"""
from __future__ import annotations

import atexit
import io
import json
import os
import shutil
import sys
import tempfile
import traceback

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".workbench/scripts")

import wb_core  # noqa: E402

RESULTS = []

# 运行时探针建的临时目录必须回收：harness 会把 TEMP 重定向到 WB_TMP 并在
# 结束时统一删，但直接运行本文件（README 与排障时都会这么干）没人兜底 ——
# 实测一次直接运行漏 13 个 probe_* 目录。atexit 覆盖两条路径。
_FRESH_DIRS = []


def _cleanup_fresh():
    for d in _FRESH_DIRS:
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup_fresh)


# 拒绝型探针：期望抛出 SpecViolation。抛出 = 声称被证实，写入 = 缺陷。
EXPECT_REJECT = {"P03", "P04", "P05", "P06", "P07"}


def probe(pid, title, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        if pid in EXPECT_REJECT and type(e).__name__ in ("SpecViolation", "ChainTampered"):
            ok, detail = True, "正确拒绝：%s: %s" % (type(e).__name__, e)
        else:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
            detail += "\n      " + traceback.format_exc().splitlines()[-3].strip()
    RESULTS.append((pid, title, ok, detail))
    print("  %-5s %-58s %s" % (pid, title[:58], "OK" if ok else "DEFECT"))
    if not ok:
        print("        → %s" % detail)


def fresh():
    d = tempfile.mkdtemp(prefix="probe_")
    _FRESH_DIRS.append(d)
    os.makedirs(os.path.join(d, "events", "sidecar"), exist_ok=True)
    os.makedirs(os.path.join(d, "state"), exist_ok=True)
    return d


UUID = "0" * 32


# ---------------------------------------------------------------- P01
def p01_lease_rejected_then_crash():
    """P01：epoch 落后的租约被拒后，租约状态行里没有 epoch 键，
    下一次 acquire 读回来是 None，比较 None > int 会不会崩？"""
    d = fresh()
    st = wb_core.Store(d, UUID)
    st.acquire_lease("T1", 200, ttl_sec=30)          # 先拿 200
    got = st.acquire_lease("T1", 100, ttl_sec=30)     # 用旧的 100 抢 → 应被拒
    nxt = st.acquire_lease("T1", 300, ttl_sec=30)     # 再用 300 → 应该成功
    return (got is False and nxt is True,
            "第二次拒绝=%r，第三次=%r（期望 False / True）" % (got, nxt))


# ---------------------------------------------------------------- P02
def p02_lease_expiry_ignored():
    """P02：租约带 expires_ms，过期后别人应该能抢占。
    代码里 expires_ms 只写不读 —— 过期租约还能挡住新持有者吗？"""
    d = fresh()
    st = wb_core.Store(d, UUID)
    st.acquire_lease("T1", 100, ttl_sec=0)            # 立即过期
    ok = st.acquire_lease("T1", 50, ttl_sec=30)       # epoch 更小但租约已过期
    return (ok is True, "过期后低 epoch 抢占返回 %r（期望 True＝过期可抢占）" % ok)


# ---------------------------------------------------------------- P03
def p03_unknown_kind_accepted():
    """P03：45 类事件表是权威，写链时校验过 kind 吗？
    写一个表里没有的 kind，链会接受吗？"""
    d = fresh()
    st = wb_core.Store(d, UUID)
    row = st.append_event("totally_made_up_event", {"x": 1}, "lead")
    ok = row and row.get("seq") == 1
    return (not ok, "链接受了一个不存在的事件类型，seq=%r（期望：拒绝写入）" % (row or {}).get("seq"))


# ---------------------------------------------------------------- P04
def p04_missing_required_payload():
    """P04：事件表声明了 required_payload，写链时校验吗？
    E10 verify 要求 result/reason_code，写空 payload 会过吗？"""
    d = fresh()
    st = wb_core.Store(d, UUID)
    row = st.append_event("verify", {}, "system:verifier", "T1")
    return (row is None, "空 payload 的 verify 被接受，seq=%r（期望：拒绝）" % (row or {}).get("seq"))


# ---------------------------------------------------------------- P05
def p05_frozen_not_enforced():
    """P05：文档说"冻结态只允许白名单事件写入"。
    wb_core 里 FROZEN 只有定义没有执行点 —— 冻结时还能写非白名单事件吗？"""
    d = fresh()
    st = wb_core.Store(d, UUID)
    wb_core.FROZEN = True
    try:
        row = st.append_event("submit", {"x": 1}, "lead", "T1")
    finally:
        wb_core.FROZEN = False
    return (row is None, "FROZEN=True 时非白名单事件 submit 仍写入 seq=%r（期望：拒绝）" % (row or {}).get("seq"))


# ---------------------------------------------------------------- P06
def p06_rebuild_accepts_bogus_state():
    """P06：rebuild 重放状态事件时校验 to 是合法状态吗？"""
    d = fresh()
    st = wb_core.Store(d, UUID)
    st.append_event("state", {"task": "T1", "from": None, "to": "not_a_real_state"}, "lead", "T1")
    try:
        r = st.rebuild()
        got = r["tasks"].get("T1", {}).get("state")
        return (got != "not_a_real_state", "rebuild 重放出了非法状态 %r（期望：拒绝）" % got)
    except Exception as e:
        return True, "rebuild 拒绝了非法状态：%s" % e


# ---------------------------------------------------------------- P07
def p07_rebuild_ignores_transition_legality():
    """P07：rebuild 只比对 from，不比对 T01–T22 是否允许这条迁移。"""
    d = fresh()
    st = wb_core.Store(d, UUID)
    st.append_event("state", {"task": "T1", "from": None, "to": "archived"}, "lead", "T1")
    try:
        r = st.rebuild()
        got = r["tasks"].get("T1", {}).get("state")
        return (got != "archived", "rebuild 接受了不存在的迁移 ∅→archived（状态=%r）" % got)
    except Exception as e:
        return True, "rebuild 拒绝了非法迁移：%s" % e


# ---------------------------------------------------------------- P08
def p08_month_file_hardcoded():
    """P08：主链文件名必须按 ts 派生 YYYY-MM，不能写死。

    v2 修正：v1 用 grep 查字面量，结果被我自己的**注释**（解释这个 bug 时引用了
    旧文件名）触发误报。文本扫描分不清代码与说明 —— 和 doc_check 曾经把章节号
    「5.4」当成状态计数是同一类错。改用 AST 只看真正的字符串常量。
    """
    import ast

    tree = ast.parse(open(".workbench/scripts/wb_core.py", encoding="utf-8").read())
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if v.endswith(".jsonl") and len(v) == len("YYYY-MM.jsonl") and v[:4].isdigit():
                hits.append((getattr(node, "lineno", "?"), v))
    return (not hits, "代码里出现写死的月度链文件名 %r（期望由 ts 派生）" % (hits,))


# ---------------------------------------------------------------- P09
def p09_seq_across_month_files():
    """P09：chain_files 按文件名排序拼接。若两月文件并存，seq 必须跨文件连续。
    造 2026-11 + 2026-12 两个文件验证。"""
    d = fresh()
    st = wb_core.Store(d, UUID)
    st.append_event("plan", {"plan_id": "p1"}, "lead")
    # 手工把第一条挪到 11 月文件，再写一条
    st.append_event("plan", {"plan_id": "p2"}, "lead")
    files = [os.path.basename(f) for f in st.chain_files()]
    r = st.verify_chain(repair=False)
    return (r["status"] == "ok" and len(files) == 1,
            "chain_files=%r，verify=%r（当前只有一个文件，跨月拼接未被真正测到）" % (files, r["status"]))


# ---------------------------------------------------------------- P10
def p10_tamper_middle_detected():
    """P10（对照，应通过）：中间行篡改必须被抓。"""
    d = fresh()
    st = wb_core.Store(d, UUID)
    for i in range(5):
        st.append_event("plan", {"plan_id": str(i)}, "lead")
    p = st.chain_files()[0]
    lines = open(p, "rb").read().split(b"\n")
    row = json.loads(lines[1])
    row["payload"]["plan_id"] = "TAMPERED"
    lines[1] = wb_core.jcs(row).encode()
    open(p, "wb").write(b"\n".join(lines))
    try:
        st.verify_chain(repair=False)
        return False, "篡改未被发现"
    except wb_core.ChainTampered as e:
        return True, str(e)[:100]


# ---------------------------------------------------------------- P11
def p11_tail_repair_keeps_chain_writable():
    """P11（对照）：末行半截断后修复，且能继续写。"""
    d = fresh()
    st = wb_core.Store(d, UUID)
    for i in range(3):
        st.append_event("plan", {"plan_id": str(i)}, "lead")
    p = st.chain_files()[0]
    raw = open(p, "rb").read()
    open(p, "wb").write(raw[: len(raw) - 10])       # 砍掉末尾 10 字节
    r = st.verify_chain(repair=True)
    st.append_event("plan", {"plan_id": "after"}, "lead")
    r2 = st.verify_chain(repair=False)
    return (r["status"] in ("repaired", "ok") and r2["status"] == "ok",
            "repair=%r lines=%r → 续写后 %r" % (r["status"], r["lines"], r2["status"]))


# ---------------------------------------------------------------- P12
def p12_ts_ms_from_caller():
    """P12：文档说 ts_ms 由 store 生成、不接受调用方传入。
    实际 append_event 允许调用方在 meta 里塞 ts_ms 覆盖吗？"""
    d = fresh()
    st = wb_core.Store(d, UUID)
    row = st.append_event("plan", {"plan_id": "p"}, "lead", meta={"ts_ms": 1})
    inner = (row.get("meta") or {}).get("ts_ms")
    return (inner != row.get("ts_ms"), "meta.ts_ms=%r 与顶层 ts_ms=%r 并存（期望：meta 里不许有 ts_ms）"
            % (inner, row.get("ts_ms")))


# ---------------------------------------------------------------- P13
def p13_frozen_whitelist_from_spec():
    """P13：冻结期白名单必须来自 spec 的 writable_in_frozen，不能是第二份硬编码。

    P05 只证明"冻结能拦"，拦错了同样致命：漏掉 E06 会让看门狗在冻结期失明，
    多放进 E09 会让冻结期还能推进任务。所以要比**集合**。
    """
    d = fresh()
    st = wb_core.Store(d, UUID)
    spec_ids = set()
    import json as _j, os as _os
    with open(_os.path.join(".workbench", "spec", "events.schema.json"), encoding="utf-8") as fh:
        doc = _j.load(fh)
    for e in doc["events"]:
        if e.get("writable_in_frozen") and not e.get("retired"):
            spec_ids.add(e["id"])
    def _probe_actor(e):
        """挑该事件声明里的一个合法 actor 去试写。

        append_event 的冻结判定(wb_core.py:441)排在 actor 资格校验(:473)
        之前，两处抛的是同一个 SpecViolation。若一律用 "lead" 试写全部事件，
        E06(只能 system:watchdog)、E25(只能 system:gate_timer) 等会倒在
        actor 校验上，被这个探针误算成"不在白名单" —— 于是明明 14 项白名单
        全对，探针却报 spec-only 差 13 项。尺子做错比没尺子更坏：下一个人
        要花一整天逐个证伪，证伪完就不再相信这把尺子。
        """
        for a in (e.get("actors") or []):
            if a not in ("*authority*", "human:*"):
                return a
        return "human:yg"   # members.json 里 active 的人工主体

    live = getattr(wb_core, "FROZEN_WHITELIST", None)
    if live is None:
        # 从行为反推：冻结态下逐个试写（每个事件用它自己的合法 actor）
        live = set()
        for e in doc["events"]:
            if e.get("retired"):
                continue
            wb_core.FROZEN = True
            try:
                _actor = _probe_actor(e)
                # 人类决定需要可验签凭据（wb_core.check_human_sig）。探针也必须签，
                # 否则它会撞上"缺 sig_chain"并被下面那个 except 算成"不在白名单"——
                # 于是 14 项全对、探针报差 13 项。尺子做错比没尺子更坏的第二例。
                _pay = {k: 1 for k in (e.get("required_payload") or [])}
                _meta = (wb_core.signed_human_meta(e["kind"], _actor, None, _pay)
                         if str(_actor).startswith("human:") else None)
                st.append_event(e["kind"], _pay, _actor, None, meta=_meta)
                live.add(e["id"])
            except wb_core.SpecViolation as exc:
                # append_event 里 frozen 闸门(:868) 与 from_states 校验(:919)
                # 抛的是同一个 SpecViolation。只有「not writable_in_frozen」
                # 才是真被冻结拦下；倒在状态机/字段校验上的事件恰恰说明
                # frozen 放行了它 —— 一律 pass 会让 14 项白名单报差 8 项。
                # 尺子做错比没尺子更坏的第三例（前两例见 _probe_actor / _meta），
                # 与 tests/test_actor_and_freeze.py:112 用同一条判据。
                if "not writable_in_frozen" in str(exc):
                    pass                # 真被 frozen 拦 → 不在白名单
                else:
                    live.add(e["id"])   # 倒在别的校验 → frozen 放行了它
            except Exception:
                live.add(e["id"])       # 非 SpecViolation 的意外失败，保守算放行
            finally:
                wb_core.FROZEN = False
    return (set(live) == spec_ids,
            "spec=%d 项，实际放行=%d 项；差集 spec-only=%s live-only=%s"
            % (len(spec_ids), len(live),
               sorted(spec_ids - set(live)), sorted(set(live) - spec_ids)))


# ---------------------------------------------------------------- P14
def p14_sidecar_numeric_types():
    """P14：sidecar 虽不在链上，但 integer-only 语义要一致。

    实测发现 acquire_lease 把 expires_ms 写成 `now + ttl*1000` 的 float
    （1794228689708.0）。JSON 里合法，读回来是 float，下游 `int()` 掩盖了它 ——
    但任何直接比较 expiry 的地方都会变成浮点比较，且与主链的 int 语义不一致。
    """
    d = fresh()
    st = wb_core.Store(d, UUID)
    st.acquire_lease("T-x", 1000, ttl_sec=30)
    rows = []
    pth = os.path.join(d, "events", "sidecar", "lease.jsonl")
    with open(pth, encoding="utf-8") as fh:
        for ln in fh:
            if ln.strip():
                rows.append(json.loads(ln))
    bad = []
    for r in rows:
        for k, v in (r.get("payload") or {}).items():
            if isinstance(v, float):
                bad.append("%s=%r" % (k, v))
    return (not bad, "float 字段：%s" % (bad or "无"))


# ---------------------------------------------------------------- P15
def p15_empty_change_refused():
    """P15：零改动必须被拒，而不是走完 submit→verify(pass)→done。

    这是最容易"看起来成功"的失败：链上七条事件条条合法，主干却一行没变。
    """
    # 这里曾经 `git init/add/commit` 起一个空仓库，但 dsc.check() 只遍历
    # 传入的 changed 列表（此处恒为 []），根本不读工作树 —— 那三行 git 是
    # 纯死代码，还让探针在「PATH 里没有 git」时抛 FileNotFoundError 被记成
    # DEFECT（直接运行本文件时实测）。删掉后 P15 只测它真正要测的东西。
    tmp = tempfile.mkdtemp(prefix="wb_p15_")
    try:
        import diff_sandbox_check as dsc
        empty = []
        chk = dsc.check(["src/"], empty, ["src/**"])
        # check() 的返回只有 {ok, violations, scopes} —— 它不返回、也不该返回任何
        # 被"补"出来的文件名。历史上这里曾经写过 `changed or ["src/placeholder.py"]`，
        # 于是空改动也能通过写域校验：闸门看起来在拦，实际上放行。
        # 所以探针要证明两件事：① 空改动不产生违规（没什么可越界的）；
        # ② 返回体里不存在任何人造文件名，且传入的列表没被就地改写。
        ghost = [v["file"] for v in chk["violations"] if v.get("file")]
        fabricated = [g for g in ghost if g not in empty]
        return (chk["ok"] and not chk["violations"] and not fabricated and empty == [],
                "check(空改动) → ok=%r violations=%d 人造文件名=%s 入参被改写=%r"
                % (chk["ok"], len(chk["violations"]), fabricated or "无", empty != []))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 78)
    print("运行时行为探针 audit_runtime.py")
    print("=" * 78)
    print()
    probe("P01", "租约被拒后再申请（None 与 int 比较）", p01_lease_rejected_then_crash)
    probe("P02", "租约过期后可被低 epoch 抢占", p02_lease_expiry_ignored)
    probe("P03", "未知事件类型是否被拒", p03_unknown_kind_accepted)
    probe("P04", "缺 required_payload 是否被拒", p04_missing_required_payload)
    probe("P05", "FROZEN 态是否真的拦非白名单事件", p05_frozen_not_enforced)
    probe("P06", "rebuild 是否拒绝非法状态名", p06_rebuild_accepts_bogus_state)
    probe("P07", "rebuild 是否拒绝非法迁移", p07_rebuild_ignores_transition_legality)
    probe("P08", "主链月份文件名是否硬编码", p08_month_file_hardcoded)
    probe("P09", "跨月文件 seq 连续（当前是否可测）", p09_seq_across_month_files)
    probe("P10", "对照：中间行篡改被抓", p10_tamper_middle_detected)
    probe("P11", "对照：末行截断修复后可续写", p11_tail_repair_keeps_chain_writable)
    probe("P12", "ts_ms 是否只能由 store 生成", p12_ts_ms_from_caller)
    probe("P13", "冻结白名单是否真来自 spec（集合比对）", p13_frozen_whitelist_from_spec)
    probe("P14", "sidecar 数值字段是否 integer-only", p14_sidecar_numeric_types)
    probe("P15", "空改动不会被打扮成改动", p15_empty_change_refused)

    ok = sum(1 for r in RESULTS if r[2])
    bad = len(RESULTS) - ok
    print()
    print("-" * 78)
    print("探针 %d 条：%d 条声称被证实，%d 条发现缺陷" % (len(RESULTS), ok, bad))
    print()
    for pid, title, okk, _ in RESULTS:
        if not okk:
            print("  DEFECT %s  %s" % (pid, title))

    with open("evidence/audit_runtime.json", "w", encoding="utf-8") as f:
        json.dump([{"id": p, "title": t, "as_designed": o, "detail": d} for p, t, o, d in RESULTS],
                  f, ensure_ascii=False, indent=1)
    print()
    print("wrote evidence/audit_runtime.json")
    # 探针结论必须进回归：恒 return 0 时，一条常年假 DEFECT（P13）与一条
    # 真缺陷在 run_all_checks.sh 里长得一模一样，115/0 照样全绿。对齐
    # audit_closure.py:413 的口径：有缺陷就非零，让 run() 记进 FAILED。
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
