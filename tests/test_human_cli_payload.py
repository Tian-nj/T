"""human_cli 的 payload 形状：spec 说要顶层，就别只放进 args。

真事：`escalate_resolve`（E18）的 spec 写着 required_payload=['task','verdict']，
而 `emit()` 把所有非 task/to 的键一律塞进 `args`。于是：

    wb_core.SpecViolation: E18 escalate_resolve missing required payload keys ['verdict']

verdict 明明就在 args 里。这类错误最坏的地方不是崩，是它**只崩一部分 op**：
必填键恰好叫 task / to 的（E21 approve、E23 override）一路顺畅，
其余的（E18 verdict、E22 reason_code、E24 fields、E44 children）永远跑不通，
而且每条报错都长得像"参数没传"。

本测试锁三件事：
  1. spec 声明为顶层必填的键会被提升；
  2. 缺这个键时报 BAD_ARGS（一句人话），而不是让 SpecViolation 带着
     traceback 从 append_event 里穿出去；
  3. 提升是**按 spec 驱动**的 —— 不写死 op 表，否则下一个加进来的 op 又漏。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".workbench", "scripts"))

R = []


def t(name, got, want):
    ok = got == want
    R.append(ok)
    print("%-56s %s  got=%r want=%r" % (name, "ok" if ok else "FAIL", got, want))


class FakeStore:
    def __init__(self, *a, **k):
        self.events = []
        self.transitions = []

    def current_state(self, tid):
        return "escalated"

    def check_transition(self, frm, to, actor):
        return "T15"

    def append_event(self, kind, payload, actor, task=None, meta=None):
        # 复刻真 wb_core 的校验：必填顶层键缺一个就抛 SpecViolation。
        # 不在这里复刻，测试就会对真实现的缺口视而不见。
        import wb_core
        req = wb_core._required_payload(kind)
        missing = [k for k in req if k not in payload]
        if missing:
            raise wb_core.SpecViolation("%s missing required payload keys %r"
                                        % (kind, missing))
        ev = {"kind": kind, "payload": payload, "actor": actor, "task": task,
              "seq": len(self.events) + 1}
        self.events.append(ev)
        return ev

    def transition(self, tid, to, actor, reason_event=None, **meta):
        self.transitions.append({"tid": tid, "to": to})
        return {"seq": 900, "from": "escalated", "to": to}


def main():
    import human_cli as hc

    hc.wb_core.Store = FakeStore
    hc.sign_manifest.read_uuid_from_config = lambda: "0000-uuid"
    hc.record_nonce = lambda *a, **k: None

    member = {"principal": "yg"}

    # ---------- 1. E18：verdict 必须提升到顶层 ----------
    out = hc.emit(member, "escalate_resolve",
                  {"task": "T-1", "verdict": "reverted", "to": "done"}, "n1", 1)
    biz = out["events"][0]
    t("E18 写了事件", biz["kind"], "escalate_resolve")
    t("E18 一并写了 state", out["events"][1]["kind"], "state")

    # ---------- 2. 直接检查 _required_for_kind 与提升逻辑 ----------
    t("spec 里 E18 要 verdict", "verdict" in hc._required_for_kind("escalate_resolve"), True)
    t("spec 里 E21 只要 task", hc._required_for_kind("human_approve"), ["task"])
    t("未知 kind 返回空表", hc._required_for_kind("no_such_kind"), [])

    # 用一个能留住实例的假 Store 再跑一遍，检查真实 payload 形状
    captured = {}

    class Capture(FakeStore):
        def __init__(self, *a, **k):
            FakeStore.__init__(self, *a, **k)
            captured["st"] = self

    hc.wb_core.Store = Capture
    hc.emit(member, "escalate_resolve",
            {"task": "T-2", "verdict": "reverted", "to": "done"}, "n2", 2)
    pl = captured["st"].events[0]["payload"]
    t("E18 payload 顶层有 verdict", pl.get("verdict"), "reverted")
    t("E18 payload 仍保留 args", "verdict" in (pl.get("args") or {}), True)
    t("E18 payload 有 task", pl.get("task"), "T-2")

    # ---------- 3. E22：reason_code 同理 ----------
    hc.wb_core.Store = Capture
    hc.emit(member, "human_reject",
            {"task": "T-3", "reason_code": "HUMAN_REJECTED"}, "n3", 3)
    pl3 = captured["st"].events[0]["payload"]
    t("E22 payload 顶层有 reason_code", pl3.get("reason_code"), "HUMAN_REJECTED")

    # ---------- 4. 缺必填键 → BAD_ARGS，不是 SpecViolation ----------
    hc.wb_core.Store = FakeStore
    try:
        hc.emit(member, "escalate_resolve", {"task": "T-4", "to": "done"}, "n4", 4)
        code = "NO_RAISE"
    except hc.Reject as exc:
        code = exc.code
    except Exception as exc:                       # noqa: BLE001
        code = type(exc).__name__
    t("缺 verdict 时报 BAD_ARGS", code, "BAD_ARGS")

    # ---------- 5. 不写死 op 表：新加一个必填键多的 op 也自动生效 ----------
    # 这里直接用 spec 里 required 最长的事件验证"提升是数据驱动的"：
    # E26 config_change 要 key/old/new 三个顶层键。
    hc.wb_core.Store = Capture
    try:
        hc.emit(member, "human_amend_task",
                {"task": "T-5", "fields": {"title": "x"}}, "n5", 5)
        pl5 = captured["st"].events[0]["payload"]
        got5 = pl5.get("fields")
    except hc.Reject as exc:
        got5 = "REJECT:%s" % exc.code
    t("E24 payload 顶层有 fields", got5, {"title": "x"})

    ok = all(R)
    print("\nhuman_cli_payload: %s (%d/%d)" % ("PASS" if ok else "FAIL", sum(R), len(R)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
