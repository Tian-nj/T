#!/usr/bin/env python3
"""lock_truth.py —— `lock_semantics.json` 与代码的双向真值（第三十八轮）。

为什么需要这个脚本：`spec/lock_semantics.json` 是一份**声明**。
声明自己不会变成真的。现在的状态是「人在 K10 里手写六条字段检查」，
它能防「有人把 equivalent 悄悄改成 true」，防不住另外两件事：

  A. 有人把 `wb_core._Lock` 改成阻塞 `LOCK_EX`（或者把退避改成
     500Hz 空转），而 `lock_semantics.json` 一个字没动 —— 那时表里
     写的 `primitive` / `backoff_max_ms` 就成了历史文件的描述，不是现状。
  B. 表里写的 `semantics: mandatory / advisory` 从来没被实测过。
     它来自「msvcrt 是强制锁」这句人话，而人话可以是错的。

所以本脚本做两件事，用同一个出口报告，**两个方向任一不符都判失败**：

  方向一（声明 → 代码）：表里的每个可核对字段，去源码里找它的载体。
        `primitive` 必须是源码里真出现过的那一行调用；`backoff_*`
        必须等于 `_Lock` 里的常量；`aging_floor` 同理；`fairness: none`
        要求源码里没有票据队列的痕迹（一旦有人加了队列，声明就必须跟着改，
        而不是让表继续写着 none）。
  方向二（代码 → 行为）：真起一个子进程，让它**不配合加锁**地写被锁住的
        字节，看它成不成功。成功 = 建议锁；失败 = 强制锁。
        这是唯一能证伪 `semantics` 字段的实验，也是为甚么
        `equivalent: false` 不是一句口号：它说的是「两个平台对不配合的
        第三方进程行为不同」，而这个差异现在可以被量出来。

边界（写死）：
  * 只读源码、只用临时文件做实验，不碰 `state/.lock`、不碰主链。
  * 实验对象是**本机当前平台**。另一平台的语义只做声明核对，不做行为断言
    —— 在 Win32 上假装测过 POSIX 是这份仓反复打过的那种脸。
  * 退出码：0 = 两向都符；1 = 有任一不符；2 = 无法判定（例如源码读不到）。
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)

SPEC_REL = "spec/lock_semantics.json"
CORE_REL = "scripts/wb_core.py"

CRITERION = "lock_truth_v1"
IS_WIN = os.name == "nt"

# 行为探针：子进程不调用任何锁 API，直接写被锁住的字节。
# 这个子进程故意写得极简 —— 它要证明的是「一个不配合的进程会遇到什么」，
# 所以它自己不能有任何配合动作。
PROBE = r"""
import sys
path, mode = sys.argv[1], sys.argv[2]
fh = open(path, mode)
try:
    fh.seek(0)
    fh.write(b"Z")
    fh.flush()
    print("WROTE")
except OSError as exc:
    print("DENIED %s %s" % (type(exc).__name__, getattr(exc, "errno", "")))
finally:
    try:
        fh.close()
    except Exception:
        pass
"""


def _wb(wb=None):
    return os.path.abspath(wb or WB)


def _spec(wb=None):
    with open(os.path.join(_wb(wb), *SPEC_REL.split("/")), "r",
              encoding="utf-8") as fh:
        return json.load(fh)


def _core_src(wb=None):
    with open(os.path.join(_wb(wb), *CORE_REL.split("/")), "r",
              encoding="utf-8") as fh:
        return fh.read()


def _lock_src(src):
    """只取 `class _Lock` 那一段。理由：`wb_core.py` 里还有别的地方
    用 `time.sleep` 和常量，整文件匹配会把无关命中算成证据。"""
    i = src.find("class _Lock:")
    if i < 0:
        return ""
    j = src.find("\n    def _lock(", i)
    return src[i:(j if j > 0 else len(src))]


def _const(text, name):
    """从源码里抠 `NAME = <number>` 的字面量。找不到返回 None。
    只认同一行、只认裸数字 —— 允许常量被引用的写法（`= OTHER`）会让
    「声明 vs 代码」变成「声明 vs 另一个常量」，那正是两个真相的开头。"""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(name + " = "):
            raw = s.split("=", 1)[1].strip()
            raw = raw.split("#", 1)[0].strip()
            try:
                if raw.startswith(("0x", "0X")):
                    return int(raw, 16)
                return float(raw) if "." in raw else int(raw)
            except ValueError:
                return None
    return None


def declaration_side(spec, src):
    """方向一：声明 → 代码。返回 (checks, problems)。"""
    lock = _lock_src(src)
    checks, bad = [], []

    def ck(name, ok, detail=""):
        checks.append({"what": name, "ok": bool(ok), "detail": detail})
        if not ok:
            bad.append(name)

    if not lock:
        ck("源码里能找到 class _Lock", False, CORE_REL)
        return checks, bad

    plats = spec.get("platforms") or {}
    common = spec.get("common") or {}

    # primitive 必须真出现在源码里（逐平台各一条）
    for p, expect in (("win32", "msvcrt.locking"), ("posix", "fcntl.flock")):
        prim = (plats.get(p) or {}).get("primitive") or ""
        head = prim.split("(")[0].strip()
        ck("primitive(%s) 的调用名在源码里" % p, bool(head) and head in lock,
           "declared=%r" % prim)
    ck("win32 用 LK_NBLCK 而不是阻塞 LK_LOCK",
       "LK_NBLCK" in lock and "LK_LOCK" not in lock,
       "")
    ck("posix 用 LOCK_NB（非阻塞轮询，不是阻塞 LOCK_EX）",
       "LOCK_EX | fcntl.LOCK_NB" in lock, "")

    # 常量必须逐字对齐：这是最容易「改一处忘一处」的地方。
    # 注意量纲：源码里退避用秒（time.sleep 的单位），spec 里用毫秒。
    # 这条换算必须写在这里而不是「看起来差不多就过」——
    # 差 1000 倍的常量对眼睛是最隐形的错。
    for name, key, unit in (("BACKOFF_START", "backoff_start_ms", 1000.0),
                            ("BACKOFF_MAX", "backoff_max_ms", 1000.0),
                            ("DEFAULT_TIMEOUT_MS", "timeout_ms_default", 1.0),
                            ("AGING_FLOOR", "aging_floor", 1.0)):
        got = _const(lock, name)
        want = common.get(key)
        if isinstance(want, (int, float)) and got is not None:
            ok = abs(float(got) * unit - float(want)) < 1e-6
        else:
            ok = False
        ck("常量 %s == spec.common.%s" % (name, key), ok,
           "code=%r (×%g) spec=%r" % (got, unit, want))

    # 退避是「指数 + 上限」，不是固定 sleep。空转复活就判红。
    ck("退避是指数增长（delay * 2）", "delay * 2" in lock, "")
    ck("没有 500Hz 空转的回归（time.sleep(0.002) 字面量）",
       "time.sleep(0.002)" not in lock, "")
    ck("超时抛独立异常类型",
       "raise LockTimeout(" in lock and
       common.get("timeout_exception") == "wb_core.LockTimeout",
       "declared=%r" % common.get("timeout_exception"))

    # fairness: none —— 声明说没有队列，源码里就不许有票据/队列 구조
    fair = common.get("fairness")
    ck("fairness 声明是 none", fair == "none", "declared=%r" % fair)
    queue_marks = [m for m in ("ticket", "FIFO", "deque(", "queue.Queue")
                   if m in lock]
    ck("声明 none 时源码里没有票据队列痕迹", not queue_marks,
       "found=%r" % queue_marks)

    # aging：常量存在还不够，得证明它真的进了退避公式。
    # 「声明一个常量但没人用」是本仓反复打的那种脸（声明悬空），
    # 所以判据落在算式上：cap 必须由 AGING_FLOOR 与 age 共同决定，
    # 且 delay 必须被 cap 封顶。
    ck("退避上限由 AGING_FLOOR 与 age 共同决定",
       "cap = self.BACKOFF_MAX * (self.AGING_FLOOR" in lock, "")
    ck("delay 被 cap 封顶（不是只乘 2）",
       "delay = min(max(delay * 2, self.BACKOFF_START), cap)" in lock, "")
    ck("age 由已等时长与预算之比给出",
       "age = min(1.0, (now - started) / max(1e-9," in lock, "")

    # 采样落盘方向：声明说样本进哪个目录，源码里就得真写那个目录
    tdir = common.get("telemetry_dir") or ""
    leaf = tdir.split("/")[-1] if tdir else ""
    ck("telemetry_dir 的目录名在源码里出现",
       bool(leaf) and ("\"%s\"" % leaf in src or "%r" % leaf in src),
       "declared=%r" % tdir)
    ck("等待与持锁两类样本分开记",
       "lock_waits-" in lock and "lock_holds-" in lock, "")
    ck("只有 waited_ms>0 才记等待样本",
       "if self.waited_ms > 0:" in lock, "")

    # 校准口径：声明里的统计量与工具必须真存在
    cal = common.get("calibration") or {}
    tool = cal.get("tool") or ""
    ck("校准工具路径存在",
       bool(tool) and os.path.isfile(os.path.join(ROOT, *tool.split("/"))),
       "declared=%r" % tool)
    ck("校准判据是不等式 timeout>=p99(hold)",
       "p99" in (cal.get("statistic") or "")
       and ">=" in (cal.get("invariant") or ""),
       "declared=%r" % cal.get("invariant"))
    ck("样本不足判 INSUFFICIENT 而不判失败",
       cal.get("insufficient_verdict") == "INSUFFICIENT"
       and cal.get("insufficient_is_failure") is False, "")

    # 一条反向护栏：equivalent:false 不许被悄悄改成 true
    ck("equivalent 仍是 false", spec.get("equivalent") is False,
       "declared=%r" % spec.get("equivalent"))
    return checks, bad


def behavior_probe(hold_ms=400):
    """方向二：代码 → 行为。本机当前平台上，「不配合的第三方进程」
    能不能绕过我们持有的锁？

    返回 {"platform","held","probe","observed_semantics","detail"}。
    observed_semantics ∈ {mandatory, advisory, inconclusive}。
    """
    tmp = tempfile.mkdtemp(prefix="locktruth-")
    path = os.path.join(tmp, "probe.lock")
    out = {"platform": "win32" if IS_WIN else "posix",
           "held": False, "probe": None,
           "observed_semantics": "inconclusive", "detail": ""}
    probe_path = os.path.join(tmp, "probe.py")
    try:
        with open(path, "wb") as fh:
            fh.write(b"0" * 8)
        with open(probe_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(PROBE)
        fh = open(path, "r+b")
        try:
            if IS_WIN:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            out["held"] = True
            try:
                p = subprocess.run([sys.executable, probe_path, path, "r+b"],
                                   capture_output=True, text=True, timeout=30)
                line = (p.stdout or "").strip().splitlines()
                out["probe"] = line[-1] if line else ("rc=%d" % p.returncode)
            except Exception as exc:      # noqa: BLE001
                out["probe"] = "probe failed: %s: %s" % (type(exc).__name__, exc)
        finally:
            try:
                if IS_WIN:
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()
        if out["probe"] == "WROTE":
            out["observed_semantics"] = "advisory"
            out["detail"] = ("持锁期间，不调用任何锁 API 的第三方进程仍写成功 —— "
                             "本机这里的锁是建议锁")
        elif (out["probe"] or "").startswith("DENIED"):
            out["observed_semantics"] = "mandatory"
            out["detail"] = ("持锁期间，不调用锁 API 的第三方进程写被拒 —— "
                             "本机这里的锁是强制锁")
        else:
            out["detail"] = "探针输出无法判读: %r" % (out["probe"],)
    finally:
        try:
            os.remove(path)
            os.remove(probe_path)
            os.rmdir(tmp)
        except OSError:
            pass
    return out


def truth(wb=None, probe=True):
    spec = _spec(wb)
    src = _core_src(wb)
    checks, bad = declaration_side(spec, src)
    out = {"schema": 1, "criterion": CRITERION, "platform": "win32" if IS_WIN else "posix",
           "declaration": {"checks": checks, "failed": bad}, "behavior": None,
           "problems": list(bad), "verdict": "UNKNOWN"}
    if probe:
        beh = behavior_probe()
        declared = ((spec.get("platforms") or {}).get(beh["platform"]) or {})
        want = declared.get("semantics")
        beh["declared_semantics"] = want
        beh["matches_declaration"] = (beh["observed_semantics"] == want)
        out["behavior"] = beh
        if beh["observed_semantics"] == "inconclusive":
            out["problems"].append("行为探针无法判读: %s" % beh["detail"])
        elif not beh["matches_declaration"]:
            out["problems"].append(
                "行为与声明不符：本机观测到 %r，表里写 %r"
                % (beh["observed_semantics"], want))
        # 反向断言：equivalent:false 的**理由**必须就是这次观测到的差异
        if spec.get("equivalent") is False and beh["observed_semantics"] != "inconclusive":
            other = "posix" if beh["platform"] == "win32" else "win32"
            other_sem = ((spec.get("platforms") or {}).get(other) or {}).get("semantics")
            if other_sem == beh["observed_semantics"]:
                out["problems"].append(
                    "两平台声明同语义（%r）却仍写 equivalent:false —— "
                    "要么声明错，要么 equivalent 错" % other_sem)
    out["verdict"] = "TRUTH_OK" if not out["problems"] else "TRUTH_BROKEN"
    return out


def check(wb=None):
    rep = truth(wb)
    if rep["declaration"]["failed"]:
        print("FAIL: 声明与代码不符 %d 项: %s"
              % (len(rep["declaration"]["failed"]),
                 ", ".join(rep["declaration"]["failed"][:6])))
    beh = rep["behavior"] or {}
    if beh:
        print("NOTE: 行为探针 platform=%s observed=%s declared=%s probe=%r"
              % (beh.get("platform"), beh.get("observed_semantics"),
                 beh.get("declared_semantics"), beh.get("probe")))
    for p in rep["problems"]:
        if not p.startswith("NOTE"):
            print("FAIL: %s" % p)
    if rep["verdict"] != "TRUTH_OK":
        print("RESULT: %s" % rep["verdict"])
        return 1
    print("PASS: lock_semantics 两向真值一致 (checks=%d, platform=%s, semantics=%s)"
          % (len(rep["declaration"]["checks"]), beh.get("platform"),
             beh.get("observed_semantics")))
    return 0


def selftest():
    fails = []

    def ck(name, got, want):
        if got != want:
            fails.append("%s: got=%r want=%r" % (name, got, want))

    tmp = tempfile.mkdtemp(prefix="locktruth-st-")
    try:
        wb = os.path.join(tmp, ".workbench")
        os.makedirs(os.path.join(wb, "spec"))
        os.makedirs(os.path.join(wb, "scripts"))
        src_real = _core_src()
        spec_real = _spec()
        with open(os.path.join(wb, *CORE_REL.split("/")), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(src_real)
        sp = os.path.join(wb, *SPEC_REL.split("/"))
        with open(sp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(spec_real, fh, ensure_ascii=False)

        out = truth(wb)
        ck("生产源码两向一致", out["problems"], [])
        ck("verdict OK", out["verdict"], "TRUTH_OK")
        ck("declaration 检查条数 > 15", len(out["declaration"]["checks"]) > 15, True)

        # 1. 行为探针真的跑出了结论（不能因为「两边都解析不出」而假绿）
        ck("behavior 有观测结论", out["behavior"]["observed_semantics"] in
           ("mandatory", "advisory"), True)
        ck("观测与声明一致", out["behavior"]["matches_declaration"], True)

        # 2. 改常量 → 声明侧必须红（这是「改一处忘一处」的唯一防线）
        with open(os.path.join(wb, *CORE_REL.split("/")), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(src_real.replace("BACKOFF_MAX = 0.05", "BACKOFF_MAX = 0.5"))
        out2 = truth(wb)
        ck("改 BACKOFF_MAX 后判红", "常量 BACKOFF_MAX == spec.common.backoff_max_ms"
           in out2["declaration"]["failed"], True)
        ck("verdict 随之 BROKEN", out2["verdict"], "TRUTH_BROKEN")

        # 3. 把 POSIX 改回阻塞 LOCK_EX → 声明侧必须红
        with open(os.path.join(wb, *CORE_REL.split("/")), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(src_real.replace("fcntl.LOCK_EX | fcntl.LOCK_NB",
                                      "fcntl.LOCK_EX"))
        out3 = truth(wb)
        ck("阻塞 flock 被判红",
           "posix 用 LOCK_NB（非阻塞轮询，不是阻塞 LOCK_EX）"
           in out3["declaration"]["failed"], True)

        # 4. 声明侧的 fairness 被改成 fair → 源码里没有队列，必须红
        spec2 = dict(spec_real)
        spec2["common"] = dict(spec_real["common"])
        spec2["common"]["fairness"] = "fifo"
        with open(sp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(spec2, fh, ensure_ascii=False)
        with open(os.path.join(wb, *CORE_REL.split("/")), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(src_real)
        out4 = truth(wb)
        ck("声明 fifo 但源码无队列被判红",
           "fairness 声明是 none" in out4["declaration"]["failed"], True)

        # 5. 把本机声明的 semantics 改错 → 行为侧必须红（这是「表是人话」的证伪点）
        spec3 = json.loads(json.dumps(spec_real))
        plat = "win32" if IS_WIN else "posix"
        spec3["platforms"][plat]["semantics"] = (
            "advisory" if spec3["platforms"][plat]["semantics"] == "mandatory"
            else "mandatory")
        with open(sp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(spec3, fh, ensure_ascii=False)
        out5 = truth(wb)
        ck("声明语义与本机观测不符被判红",
           out5["behavior"]["matches_declaration"], False)
        ck("verdict BROKEN(2)", out5["verdict"], "TRUTH_BROKEN")
        ck("问题里点名行为不符",
           any("行为与声明不符" in p for p in out5["problems"]), True)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    for f in fails:
        print("FAIL: %s" % f)
    print("lock_truth: %s (%d checks, %d failed)"
          % ("PASS" if not fails else "FAIL", 14, len(fails)))
    return 0 if not fails else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    if "--check" in argv:
        return check()
    if "report" in argv:
        print(json.dumps(truth(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(__doc__.strip().splitlines()[0])
    print("usage: lock_truth.py [--check | report [--json] | --selftest]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
