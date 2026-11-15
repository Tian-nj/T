#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""platform.py — 平台适配九项（v3.2 §9.1）。

★ 第 144 轮订正（阶段 9.1）：这里原来写的是「所有平台差异只在这一层出现。
  上层代码不许写 `if os.name == "nt"`」—— **这句话是假的**。实测全树有 26 处
  裸 `os.name` / `sys.platform` 比较，分布在 14 个文件里。它们不是「没抽象
  干净」的漏网之鱼，而是两类根本搬不进这一层的东西：

    (1) 真实 OS 原语。`msvcrt.locking` 在 Windows 上才存在，`fcntl.flock` 在
        POSIX 上才存在；CNG 只有 win32 有；`schtasks` / AppContainer / Job
        Object 是 Windows 专有的；`cmd.exe` 与 `/bin/sh`、`shlex` 方言、
        `os.symlink` 与 `.cmd` 转发脚本也都是真实 OS 事实。让这些代码改读
        `IS_WIN`，只会得到「声明说 posix、实际 import msvcrt」这种更坏的
        假象 —— 声明覆盖不能凭空造出一个不存在的库或 DLL。

    (2) 证据生产者必须读**真实**平台。`appcontainer_probe` 要如实报
        `NOT_WINDOWS`，`platform_runner_probe` 的 linux/macos 格必须保持
        `UNTESTED`，`bootstrap` 的 POSIX mode 检查在非 POSIX 上必须报
        `skipped` 而不是 `pass`。若让它们读可覆盖的 `platform.os`，就等于
        允许「在 Windows 上声明 posix」凭空伪造一条 Linux 证据 —— 这正是
        本仓一直在清理的自我确认。

  所以真正的边界不是「零处裸比较」，而是「**每一处都登记在案、理由只属于
  上面两类、且证据生产者不许用可覆盖的声明**」。`tests/test_platform_boundary.py`
  用 AST 把全树扫一遍与声明逐条对账：新加一处裸比较而不登记 → 红；登记了
  却删掉 → 也红。那个测试就是这句约束的读者 —— 之前它只是一句没人核的散文。

  **可模拟语义**（路径大小写 `case_sensitive()`/`normalize_path()`、进程组
  标志 `spawn_group()`、`free_bytes()`）在这一层，由可覆盖的 `IS_WIN`
  驱动；`_resolve_os()` 的 `platform.os` 覆盖只许驱动它们，不许进证据链。
  该覆盖能力此前**零测试**（全仓 grep 只有本文件自己读 `platform.os`），
  第 144 轮由 `tests/test_platform_boundary.py` 真跑两种声明值。

  `verify_harness.py` 的 shlex 方言、`_shell()`、`.cmd` 转发脚本仍用裸
  `os.name`：它们同样登记为 `real_os_primitive` —— `cmd.exe` / `/bin/sh`
  在这台机器上真实存在与否、win32 的 python.exe 依赖同目录 DLL，都不是
  声明能改的事实。本轮**不搬动**这些已签署的验收行为：没有真 POSIX 环境
  可验证搬动后的结果，硬搬等于用「看起来更整齐」换一个未验证的回归。

  真实 POSIX 运行期行为仍是 `UNTESTED`（属 9.2/9.3/9.4，暂定冻结），
  由 `spec/platform_boundary.json` 的 `posix_runtime.status` 登记，且
  S027 会把「把 UNTESTED 涂成 PASS」判红。本轮不声称跨 OS 已验证。

    from platform_ops import atomic_replace, fsync_dir, kill_tree, ...
（文件名 platform.py 会与标准库 platform 冲突，所以对外以 platform_ops 导入，
  本文件同时提供 `python platform_ops.py [selftest|--selftest]` 自检入口。）
"""
from __future__ import annotations

import errno
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
sys.path.insert(0, HERE)
import containment_telemetry  # noqa: E402  第二十四轮：运行时观测台账


def _cfg_platform() -> dict:
    """读 config.platform，失败返回空 dict。

    这里刻意不 import wb_core：platform_ops 是最底层模块，
    它被 wb_core 用、也被 selftest 单独跑，加一层依赖会让
    "只想用 normalize_path"的人被迫拉起整个 yaml 解析器。
    读不到就当未配置（platform.os=auto 的语义），不是错误。
    """
    try:
        import json            # noqa: F401  (仅用于类型提示一致性)
    except ImportError:
        return {}
    cfg_path = os.path.join(WB, "config.yaml")
    if not os.path.exists(cfg_path):
        return {}
    try:
        import wb_core
        return (wb_core.yaml_parse_file(cfg_path).get("platform") or {})
    except Exception:          # noqa: BLE001 - 配置坏了不该让 low-level 工具崩
        return {}


def _resolve_os() -> str:
    """`platform.os: auto|win32|posix` → `win32|posix`。

    写死的 `os.name == "nt"` 在真机上永远对，但它让
    `config.platform.os` 这个键从生下来就没有读者 ——
    于是"我在配里声明了 posix 语义"和"我什么都没配"跑出来一样。
    显式声明与实测不符时**以声明为准并警告**：交叉编译、WSL 挂载、
    以及在 CI 里模拟另一平台，都需要这条覆盖能力。
    """
    want = str((_cfg_platform() or {}).get("os") or "auto").strip().lower()
    real = "win32" if os.name == "nt" else "posix"
    if want in ("auto", ""):
        return real
    if want not in ("win32", "posix"):
        sys.stderr.write("WARN platform.os=%r unknown, using %s\n" % (want, real))
        return real
    if want != real:
        sys.stderr.write("WARN platform.os=%r overrides detected %s\n" % (want, real))
    return want


IS_WIN = _resolve_os() == "win32"


def case_sensitive(cfg: dict | None = None) -> bool:
    """`platform.case_sensitive_paths: auto|true|false`。

    这个键决定写域比较要不要 lower()。默认按 OS 猜，
    但 Git 在 Windows 上可以 `core.ignorecase=false`，
    而 WSL 挂 /mnt/c 时路径又确实是大小写不敏感的 ——
    猜错方向的两个后果不对称：该 lower 没 lower → 越界写入被放过；
    不该 lower 却 lower 了 → 两个不同文件被当成同一个，合法改动被拒。
    后者可观测（有人来投诉），前者不可观测，所以默认偏保守。
    """
    c = cfg if cfg is not None else (_cfg_platform() or {})
    v = str(c.get("case_sensitive_paths") or "auto").strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    return not IS_WIN

# Windows 错误码：目标被占用
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
_OCCUPIED_ERRNOS = {errno.EACCES, errno.EBUSY}
_OCCUPIED_WINERR = {ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION}


class OccupiedError(OSError):
    """目标被其他进程占用，重试后仍失败。调用方必须 p1_escalate，不许静默丢。"""


# ------------------------------------------------------------------ 1 atomic_replace
def atomic_replace(src: str, dst: str, backoff_ms=(100, 300, 900)) -> str:
    """原子替换 dst。Windows 用 MoveFileExW(REPLACE_EXISTING|WRITE_THROUGH)。

    占用判定：Windows 的 ERROR_SHARING_VIOLATION(32) / ERROR_ACCESS_DENIED(5)；
    POSIX 的 EACCES / EBUSY。退避 100/300/900ms 后仍失败 → 抛 OccupiedError。
    返回 "move" 或 "replace"，便于调用方记 evidence。
    """
    src, dst = os.path.abspath(src), os.path.abspath(dst)

    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        MOVEFILE_REPLACE_EXISTING = 0x1
        MOVEFILE_WRITE_THROUGH = 0x8
        flags = MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = kernel32.MoveFileExW
        fn.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        fn.restype = wintypes.BOOL
        existed = os.path.exists(dst)
        last = 0
        for delay in (0,) + tuple(backoff_ms):
            if delay:
                time.sleep(delay / 1000.0)
            if fn(src, dst, flags):
                # WRITE_THROUGH 只保证数据落到目标卷，不保证目录项持久。
                # POSIX 上 rename 之后必须 fsync 父目录，否则掉电后文件可能"消失"。
                fsync_dir(os.path.dirname(dst))
                return "replace" if existed else "move"
            last = ctypes.get_last_error()
            if last not in _OCCUPIED_WINERR:
                raise OSError(0, "MoveFileExW failed", dst, last)
        raise OccupiedError(0, "target occupied after retries (winerr=%d)" % last, dst)

    # POSIX：同盘 rename 即原子
    existed = os.path.exists(dst)
    last = None
    for delay in (0,) + tuple(backoff_ms):
        if delay:
            time.sleep(delay / 1000.0)
        try:
            os.replace(src, dst)
            return "replace" if existed else "move"
        except OSError as exc:
            last = exc
            if exc.errno not in _OCCUPIED_ERRNOS:
                raise
    raise OccupiedError(0, "target occupied after retries", dst) from last


# ------------------------------------------------------------------ 2 fsync
def fsync_fd(fd: int) -> None:
    os.fsync(fd)


def fsync_dir(path: str) -> None:
    """POSIX 上 fsync 目录以确保 rename 持久；Windows 无此语义，退化为 no-op。"""
    if IS_WIN:
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        # 拿不到目录 fd = 无法保证 rename 持久化。POSIX 上这是真实的丢数据风险，
        # 不能假装成功；但要区分"平台不支持"与"权限/占用"，所以只看 errno。
        import errno as _errno
        if exc.errno not in (_errno.EACCES, _errno.EPERM, _errno.EBUSY):
            sys.stderr.write("WARN fsync_dir(%s): %s\n" % (path, exc))
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ------------------------------------------------------------------ 3 kill_tree
def _win_creation_date(pid: int):
    """返回 (creation_date:str|None, command_line:str|None)。用于防 PID 复用误杀。"""
    ps = ("$p = Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" "
          "| Select-Object -First 1 CreationDate,CommandLine; "
          "if ($p) { \"$($p.CreationDate)`t$($p.CommandLine)\" }" % pid)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None, None
    line = (out.stdout or "").strip()
    if not line:
        return None, None
    parts = line.split("\t", 1)
    return parts[0], (parts[1] if len(parts) > 1 else None)


def process_creation_date(pid: int):
    """进程创建时间的字符串形式；取不到返回 None。

    为什么必须单独暴露这个：`spawn` 事件只记 pid，而 pid 会被操作系统复用。
    机器重启、或某个进程退出后 pid 回卷到同一个数值，原来的 pid 就指向
    一个完全无关的进程 —— 此时 `task-kill` 会杀掉无辜的它。
    `kill_tree` 早就支持 `expect_creation` 参数，但**没有人拿得到那个值**：
    唯一能取 creation_date 的 `_win_creation_date` 是私有的，且返回二元组。
    一个"支持但无法使用"的参数等于不存在。

    posix 读 `/proc/<pid>/stat` 第 22 列（starttime，单位 jiffies）；
    win32 走 WMI 的 CreationDate。两侧都返回字符串，只用于**相等比较**，
    不做算术 —— 跨平台的时钟语义不同，算出来的差值没有可比性。
    """
    try:
        if IS_WIN:
            cur, _ = _win_creation_date(pid)
            return cur
        with open("/proc/%d/stat" % int(pid), "rb") as fh:
            parts = fh.read().rsplit(b")", 1)[-1].split()
        return parts[19].decode("ascii", "replace") or None
    except Exception as exc:      # 进程可能刚退出；取不到就是取不到
        sys.stderr.write("WARN creation_date(%s) unavailable: %s\n" % (pid, exc))
        return None


def pid_alive(pid: int, expect_creation: str | None = None) -> bool:
    """进程是否活着。给了 expect_creation 时还要 creation_date 相符——防 PID 复用误杀。"""
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 第二十三轮：pid_alive 也要长跑，默认 restype=c_int 会截断 64 位
        # HANDLE。别把「刚才探针碰巧跑通」当成这里也安全。
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                                ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong(0)
            STILL_ACTIVE = 259
            if not kernel32.GetExitCodeProcess(wintypes.HANDLE(h), ctypes.byref(code)):
                return False
            if code.value != STILL_ACTIVE:
                return False
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h))
    else:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
    if expect_creation:
        cur, _ = _win_creation_date(pid) if IS_WIN else (None, None)
        if cur is not None and cur != expect_creation:
            return False
    return True


def kill_tree(pid: int, grace_sec: int = 5, expect_creation: str | None = None) -> dict:
    """杀进程树。先礼后兵：taskkill 不带 /F → 等 grace → 带 /F。

    返回 {killed: bool, method: str, pid_reused: bool|None, detail: str}。
    expect_creation 不符时**拒杀**并返回 pid_reused=True——这是防误杀的唯一手段。
    """
    if pid <= 0:
        return {"killed": True, "method": "noop", "pid_reused": None, "detail": "pid<=0"}
    if expect_creation and IS_WIN:
        cur, _ = _win_creation_date(pid)
        if cur is None:
            # 取不到当前 creation_date 时**不许往下杀**。旧版写的是
            # `if cur is not None and cur != expect_creation`，于是 WMI 抖动
            # 或 powershell 冷启动失败时，防护被静默跳过、照样 taskkill ——
            # 「防误杀的唯一手段」在最需要它的那一瞬失效，而输出看不出异常。
            # 调用方传了 expect_creation 就是想防误杀，那就 fail-closed：
            # 宁可不杀让人来看，也不要杀错。
            return {"killed": False, "method": "refused", "pid_reused": None,
                    "detail": "creation_date unavailable; refusing to kill "
                              "because expect_creation was given"}
        if cur != expect_creation:
            return {"killed": False, "method": "refused", "pid_reused": True,
                    "detail": "creation_date mismatch: expect %r got %r" % (expect_creation, cur)}

    if not pid_alive(pid):
        return {"killed": True, "method": "already_dead", "pid_reused": False, "detail": ""}

    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                       capture_output=True, text=True)
        deadline = time.time() + grace_sec
        while time.time() < deadline:
            if not pid_alive(pid):
                return {"killed": True, "method": "taskkill_graceful", "pid_reused": False, "detail": ""}
            time.sleep(0.1)
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True)
        deadline = time.time() + 5
        while time.time() < deadline:
            if not pid_alive(pid):
                return {"killed": True, "method": "taskkill_force", "pid_reused": False, "detail": ""}
            time.sleep(0.1)
        return {"killed": False, "method": "taskkill_force", "pid_reused": False,
                "detail": "still alive after force"}
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as exc:
                detail = "sigterm failed: %s" % exc
        deadline = time.time() + grace_sec
        while time.time() < deadline:
            if not pid_alive(pid):
                return {"killed": True, "method": "sigterm", "pid_reused": False, "detail": ""}
            time.sleep(0.1)
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError as exc:
                detail = "sigkill failed: %s" % exc
        time.sleep(0.2)
        alive = pid_alive(pid)
        return {"killed": not alive, "method": "sigkill", "pid_reused": False,
                "detail": "" if not alive else (detail or "still alive after SIGKILL")}


# ------------------------------------------------------------------ 4 set_readonly
def set_readonly(path: str, readonly: bool = True) -> None:
    mode = os.stat(path).st_mode
    if readonly:
        os.chmod(path, mode & ~0o222)
    else:
        os.chmod(path, mode | 0o200)


# ------------------------------------------------------------------ 4b 环境净化
# 定稿 §2.3.1（智能工作台-完整方案-v3.2（实现定稿）.md:128）原文：
#   「密钥：WORKBENCH_ROOT_KEY 环境变量注入……仅 Lead 进程与 human_cli.py
#     可见；worker 进程的环境不包含该变量（启动 worker 时显式从 env 里剔除）」
# 这一条刻意**不做成配置项**：能靠 config 关掉的隔离等于一段注释。
# 除了点名的根密钥，父进程还可能带着 CI token / 云 key / 数据库口令 ——
# 这些工作台一概用不到，worker 拿到了却能外带。所以按变量名模式剔除，
# 而不是维护一张"已知密钥"清单：清单永远漏掉下一个新加的变量。
ROOT_KEY_ENV = "WORKBENCH_ROOT_KEY"
SECRET_ENV_PATTERNS = (
    "SECRET", "PASSWORD", "PASSWD", "PRIVATE_KEY", "API_KEY", "ACCESS_KEY",
    "AUTH_TOKEN", "_TOKEN", "CREDENTIAL", "SESSION_KEY", "SIGNING_KEY",
)
# 正向代理变量不是密钥，但把父进程的 HTTP(S)/ALL/FTP_PROXY 递给 worker 等于
# 给它一条现成的外带通道（E4b 实测注入后子进程里仍看得见）。定稿 §9.3
# 「verify 环境无代理变量」此前只覆盖 verify_harness 的 base_env，worker 侧
# 漏了。这里按变量名精确剔除，刻意**不**放进 is_secret_env_name：
# 语义不同（一个凭据、一个网络出口），而且 NO_PROXY / no_proxy 是本地
# webhook（E5）要用的白名单，不能被误杀。
NETWORK_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY")


def is_secret_env_name(name: str) -> bool:
    """变量名是否属于不该递给 worker 的密钥类。"""
    k = str(name).upper()
    return k == ROOT_KEY_ENV or any(p in k for p in SECRET_ENV_PATTERNS)


def is_network_env_name(name: str) -> bool:
    """变量名是否属于不该递给 worker 的正向代理出口。"""
    return str(name).upper() in NETWORK_ENV_NAMES


def scrub_env(env) -> dict:
    """复制一份环境映射并剔除密钥类 + 代理类变量（不改入参）。

    放在平台适配层而不是派发脚本里，是因为「哪些进程算 worker」有两处：
    agent_runner.dispatch_job 起的执行体、verify_harness.run_acceptance 起的
    验收命令。两边共用同一条规则，才不会出现「执行体剔了、验收命令又
    用 env_allow 把根密钥要回来」的半截隔离。spawn_group 是唯一出口，
    所以它兜底这一层就同时覆盖两条 worker 路径，调用方不必各自记全。
    """
    return {k: v for k, v in dict(env).items()
            if not is_secret_env_name(k) and not is_network_env_name(k)}


# ------------------------------------------------------------------ 5 process_group
# 第二十三轮：把第二十二轮探明的 Job Object「进程遏制」以 **opt-in** 接入。
# 结构是平台差异，按本文件头「所有平台差异只在这一层出现」的契约落在这里，
# 不另起模块；job_object_probe.py 仍是独立诊断探针（它测原生原语能/不能干什么，
# 不参与执行链），两者不是同一决策的两份副本。
_JOB_HANDLES = []                          # 见 _contain_process：持有到控制面退出
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # 探针实测：Extended 结构 + 信息类 9 可用
_PROCESS_ALL = 0x0001 | 0x0100 | 0x0400    # TERMINATE | SET_QUOTA | QUERY_INFORMATION


def _contain_process(pid):
    """把已创建的 pid 收进一个新 Job（只设 KILL_ON_JOB_CLOSE）。

    为什么默认不接、此时才 opt-in 接：探针证明 Job Object **不隔离网络/文件**，
    默认全开会被误读成「沙箱就位」。生产只想要「更强的 kill-tree」——worker 的
    孙进程自动归属同一 Job，控制面退出时最后一个 handle 关闭即触发
    KILL_ON_JOB_CLOSE，不留孤儿；这比 taskkill /T 更可靠（后者追不到已脱离
    进程树的后代）。刻意**不**设 ActiveProcessLimit：探针里 limit=1 只是造差值
    信号，生产 worker 需要能 spawn 验收子进程，限 1 会把正常业务一起挡掉。

    返回 (applied, detail, hjob)。任何失败都返回 applied=False 并给原因，调用方
    据此降级为普通 Popen，**绝不假装已遏制**（声明悬空同型）。handle 存模块级
    list 不关闭：subprocess 没有「进程结束」回调，把 handle 挂在 Popen 上会被
    一次 GC 用 KILL_ON_JOB_CLOSE 误杀刚跑完的正常 worker；常驻到控制面退出，
    恰好是「控制面崩了就杀残留」的语义。
    """
    if not IS_WIN:
        return False, "NOT_WINDOWS: Job Object 不适用", None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # HANDLE 在 64 位上是 8 字节；ctypes 默认 restype=c_int(4 字节) 会把
        # 句柄高位截掉，短期「碰巧能跑」（句柄值小），长期持有必埋雷。探针
        # 是一次性诊断、用完即关；生产 handle 要常驻到控制面退出，这里必须
        # 显式声明参数与返回类型，不能照抄探针那种省略写法当「已验证」。
        #
        # 第二十三轮真跑纠正：BasicLimitInformation(2) + BASIC 结构在这台
        # Windows 上返回 87（ERROR_INVALID_PARAMETER）。第二十二轮诊断探针
        # 已实证 ExtendedLimitInformation(9) + EXTENDED 结构可用；生产改用
        # 同一组合，不再沿用一个会降级的猜测。只设 KILL_ON_JOB_CLOSE，
        # ActiveProcessLimit 保持 0（见函数 docstring）。
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                       ctypes.POINTER(wintypes.BOOL)]
        k32.IsProcessInJob.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IOCOUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC),
                        ("IoInfo", IOCOUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        hjob = k32.CreateJobObjectW(None, None)
        if not hjob:
            return False, "CreateJobObjectW failed: %s" % ctypes.get_last_error(), None
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
                wintypes.HANDLE(hjob), _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.c_void_p(ctypes.addressof(info)), ctypes.sizeof(info)):
            k32.CloseHandle(wintypes.HANDLE(hjob))
            return False, ("SetInformationJobObject(ExtendedLimitInformation) failed: %s"
                           % ctypes.get_last_error()), None
        hproc = k32.OpenProcess(_PROCESS_ALL, False, pid)
        if not hproc:
            k32.CloseHandle(wintypes.HANDLE(hjob))
            return False, "OpenProcess(%s) failed: %s" % (pid, ctypes.get_last_error()), None
        ok = bool(k32.AssignProcessToJobObject(wintypes.HANDLE(hjob),
                                               wintypes.HANDLE(hproc)))
        detail = ""
        if not ok:
            detail = ("AssignProcessToJobObject failed: %s（父可能已在别的 Job，"
                      "或进程已秒退）" % ctypes.get_last_error())
        else:
            inflag = wintypes.BOOL()
            k32.IsProcessInJob(wintypes.HANDLE(hproc), None, ctypes.byref(inflag))
            if not inflag.value:
                ok, detail = False, "IsProcessInJob 复核为 False"
            else:
                detail = "KILL_ON_JOB_CLOSE 已设，后代进程自动归属此 Job"
        k32.CloseHandle(wintypes.HANDLE(hproc))
        if ok:
            _JOB_HANDLES.append(hjob)      # 持有到控制面退出
            return True, detail, hjob
        k32.CloseHandle(wintypes.HANDLE(hjob))
        return False, detail, None
    except Exception as exc:               # noqa: BLE001 - 底层原语不该让业务崩
        return False, "%s: %s" % (type(exc).__name__, exc), None


def close_containment(p) -> dict:
    """显式关闭某次 spawn 的 Job handle → 触发 KILL_ON_JOB_CLOSE 杀其后代。

    生产刻意不调（要留着兜底孤儿）；供测试/显式回收验证 kill-on-close 真生效。
    """
    h = getattr(p, "_wb_job_handle", None)
    if not h:
        return {"closed": False, "reason": "no job handle（未 opt-in 或已降级）"}
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 与 _contain_process 同源：句柄是 64 位，CloseHandle 必须声明 argtypes，
        # 否则默认 c_int 会把高位截掉，关错句柄 → KILL_ON_JOB_CLOSE 不触发，
        # 而函数还会返回 closed=True（「没杀成」和「杀成了」输出等价）。
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        ok = bool(k32.CloseHandle(wintypes.HANDLE(h)))
        p._wb_job_handle = None
        if h in _JOB_HANDLES:
            _JOB_HANDLES.remove(h)
        return {"closed": ok, "reason": "KILL_ON_JOB_CLOSE 已触发" if ok
                else "CloseHandle failed: %s" % ctypes.get_last_error()}
    except Exception as exc:               # noqa: BLE001
        return {"closed": False, "reason": "%s: %s" % (type(exc).__name__, exc)}


def spawn_group(argv, cwd=None, env=None, stdout=None, stderr=None,
                contain=False) -> subprocess.Popen:
    """启动独立进程组，便于整体 kill。

    env 一律过 scrub_env：这里起的是 worker（执行体 / 验收命令），不是控制面。
    净化放在这一层兜底，是为了让「某个调用方忘了剔」不再等于「密钥漏出去」——
    调用方自己也要剔（agent_runner.dispatch_job、verify_harness.run_acceptance），
    两道都在，是因为 env_allow 那类"按需放行"很容易把根密钥又要回来。

    第十五轮：只有 env 带 WB_ISOLATE=1 的 worker 才注入 worker_shim/sitecustomize。
    这是真 enforcement，不是探针专用：network_deny 与 path_isolation 要挡住
    常规 socket.connect 与 open() 越界写。验收命令不带该标记，避免无差别把
    所有 spawn_group 都卷进 shim；用 -S/-E/-I 禁用 site/PYTHONPATH 的解释器
    启动一律拒绝，否则「换个 Python 参数就绕过闸门」会变成新的声明悬空。
    """
    e = scrub_env(env if env is not None else os.environ)
    if e.get("WB_ISOLATE") == "1":
        bad_flags = []
        for arg in list(argv)[1:]:
            s = str(arg)
            if s.startswith("-") and not s.startswith("--") and any(ch in s[1:] for ch in "EIS"):
                bad_flags.append(s)
        if bad_flags:
            raise RuntimeError("WB_ISOLATE refuses interpreter flags that disable sitecustomize: %s"
                               % bad_flags)
        shim_dir = os.path.join(HERE, "worker_shim")
        e["PYTHONPATH"] = shim_dir + (os.pathsep + e["PYTHONPATH"]
                                      if e.get("PYTHONPATH") else "")
        # 防止解释器在工作树之外写 .pyc：那个写动作会被 shim 当成越界拒绝，
        # 但它是启动副作用，不是执行体自己的业务写入，没必要让它制造噪声。
        e["PYTHONDONTWRITEBYTECODE"] = "1"
    kw = dict(cwd=cwd, env=e, stdout=stdout, stderr=stderr)
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(argv, **kw)
    # 第二十三轮：opt-in 进程遏制。contain=False 也必须留下显式 off 字段——
    # 「没接」和「没跑」在输出上等价，是这个仓反复踩过的坑。
    if not contain:
        p._wb_containment = {"applied": False, "detail": "contain=False",
                             "mode": "off", "active_process_limit": None}
        return p
    applied, detail, hjob = _contain_process(p.pid)
    p._wb_containment = {
        "applied": bool(applied),
        "detail": detail,
        "mode": "kill_on_job_close" if applied else "degraded",
        "active_process_limit": None,  # 生产刻意不设：worker 要 spawn 验收子进程
    }
    if applied:
        p._wb_job_handle = hjob
    # 第二十四轮：运行时观测台账。每次 contain=True 都留一行，成功/降级都记；
    # 只记结论字段与 argv0，不记完整命令（可能带路径/参数）。best-effort：
    # 写失败挂在 _wb_containment["telemetry"] 上，绝不阻断业务，也不让
    # 「没写成」伪装成「写成」。默认 off 分支在上面直接 return，不写文件。
    tele = containment_telemetry.record(
        p.pid, p._wb_containment, wb=WB,
        argv0=(argv[0] if argv else ""))
    p._wb_containment["telemetry"] = tele
    # 降级不抛：底层原语由于父进程已在别的 Job / 进程秒退 / 权限不足而失败时，
    # 业务仍按原来的 Popen 路径跑，失败原因挂在 Popen 上由调用方复核。
    return p


# ------------------------------------------------------------------ 6 normalize_path
def normalize_path(p: str, case_sensitive: bool | None = None) -> str:
    """统一 `/`、解 `./` 与 `../`、去重复斜杠、Windows 下小写。

    拒绝：绝对路径、盘符、以 `..` 越根、空段残留。
    """
    if case_sensitive is None:
        case_sensitive = not IS_WIN
    s = str(p).replace("\\", "/")
    if s.startswith("/") or (len(s) > 1 and s[1] == ":"):
        raise ValueError("absolute path not allowed: %r" % p)
    segs = []
    for seg in s.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not segs:
                raise ValueError("path escapes root: %r" % p)
            segs.pop()
            continue
        segs.append(seg)
    out = "/".join(segs)
    return out if case_sensitive else out.lower()


# ------------------------------------------------------------------ 7 archive
ARCHIVE_VOLUME_BYTES = 512 * 1024 * 1024


def archive_dir(src_dir: str, out_dir: str, base_name: str,
                volume_bytes: int = ARCHIVE_VOLUME_BYTES) -> list:
    """打包目录为分卷 tar + 每卷 .sha256。单卷超过 volume_bytes 即分卷（.partNN）。

    返回 [{path, bytes, sha256, part}]。
    """
    import hashlib
    import io as _io
    import tarfile

    os.makedirs(out_dir, exist_ok=True)
    # 分卷是为了「不把一个大文件写爆磁盘」，那么建卷之前就得知道还剩多少。
    # 不查空间的后果不是失败，是写到一半 ENOSPC —— 留下一堆半截卷，
    # 而 .sha256 只对写完整的卷生成，恢复时没人知道少了几卷。
    total = 0
    undersized = []
    for root_, _, files in os.walk(src_dir):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root_, f))
            except OSError as exc:
                # 统计不到大小 = 空间预检会低估。低估本身可容忍（预检只是提前量），
                # 但要让调用方知道估算不准。
                undersized.append("%s: %s" % (f, exc))
    avail = free_bytes(out_dir)
    if undersized:
        sys.stderr.write("WARN archive space estimate skipped %d file(s)\n" % len(undersized))
    if avail and total * 1.1 > avail:
        raise OSError(0, "not enough space for archive: need ~%d bytes, avail %d"
                         % (total, avail), out_dir)
    results = []
    part = 0
    buf = _io.BytesIO()
    cur = {"bytes": 0}

    def flush():
        nonlocal part, buf, cur
        if cur["bytes"] == 0 and part == 0:
            return
        part += 1
        name = "%s.tar.part%02d" % (base_name, part)
        path = os.path.join(out_dir, name)
        blob = buf.getvalue()
        with open(path, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        digest = hashlib.sha256(blob).hexdigest()
        with open(path + ".sha256", "w", encoding="utf-8", newline="\n") as fh:
            fh.write("%s  %s\n" % (digest, name))
        results.append({"path": path, "bytes": len(blob), "sha256": digest, "part": part})
        buf = _io.BytesIO()
        cur["bytes"] = 0

    class _Writer(_io.RawIOBase):
        def writable(self):
            return True

        def write(self, b):
            buf.write(b)
            cur["bytes"] += len(b)
            if cur["bytes"] >= volume_bytes:
                flush()
            return len(b)

    with tarfile.open(fileobj=_Writer(), mode="w|") as tf:
        for root, dirs, files in os.walk(src_dir):
            dirs.sort()
            for fn in sorted(files):
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, src_dir).replace("\\", "/")
                tf.add(full, arcname=arc, recursive=False)
    flush()
    return results


# ------------------------------------------------------------------ 8 free space
def free_bytes(path: str) -> int:
    st = os.statvfs(path) if hasattr(os, "statvfs") else None
    if st:
        return st.f_bavail * st.f_frsize
    import shutil
    return shutil.disk_usage(path).free


# ------------------------------------------------------------------ 9 git version
def git_version() -> tuple:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True)
    txt = (out.stdout or "").strip()
    import re
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", txt)
    if not m:
        return (0, 0, 0)
    return tuple(int(x) for x in m.groups())


# ------------------------------------------------------------------ selftest
def _selftest() -> int:
    import tempfile
    fails = []

    def chk(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + (("  " + detail) if detail and not cond else ""))
        if not cond:
            fails.append(name)

    td = tempfile.mkdtemp(prefix="plat_")

    # 1 atomic_replace
    a, b = os.path.join(td, "a.txt"), os.path.join(td, "b.txt")
    open(a, "w").write("new")
    open(b, "w").write("old")
    kind = atomic_replace(a, b)
    chk("atomic_replace", open(b).read() == "new" and kind == "replace", kind)

    # 6 normalize_path
    chk("norm basic", normalize_path("Src//A/./b.py") == ("src/a/b.py" if IS_WIN else "Src/A/b.py"))
    for bad in ("/abs/x", "C:/x", "../x", "a/../../x"):
        try:
            normalize_path(bad)
            chk("norm reject " + bad, False)
        except ValueError:
            chk("norm reject " + bad, True)
    chk("norm inner ..", normalize_path("a/b/../c") in ("a/c",))

    # 3 kill_tree + 5 process_group + pid_alive
    p = spawn_group([sys.executable, "-c", "import time; time.sleep(30)"])
    time.sleep(0.4)
    chk("pid_alive spawn", pid_alive(p.pid))
    res = kill_tree(p.pid, grace_sec=1)
    chk("kill_tree", res["killed"] and not pid_alive(p.pid), str(res))

    # pid 复用防护：伪造 creation date
    p2 = spawn_group([sys.executable, "-c", "import time; time.sleep(30)"])
    time.sleep(0.4)
    res2 = kill_tree(p2.pid, grace_sec=1, expect_creation="19990101000000.000000+000")
    # 断言的是「拒杀」这条不变量，不是某一种拒法：
    #   - 拿得到 creation_date 且不符 → method=refused, pid_reused=True
    #   - 拿不到（powershell 不在 PATH / WMI 抖动）→ 也必须 refused，
    #     只是 pid_reused=None、detail 说不可用。
    # 旧断言要求 pid_reused is True，于是把「取不到时间」这条合法拒法判成红，
    # 反而逼着实现去「默默放行」才绿 —— 那是把 fail-closed 当缺陷。
    chk("kill_tree refuses pid reuse",
        (res2["killed"] is False and res2["method"] == "refused"
         and pid_alive(p2.pid)), str(res2))
    kill_tree(p2.pid, grace_sec=1)

    # 7 archive 分卷
    src = os.path.join(td, "src")
    os.makedirs(src)
    for i in range(6):
        with open(os.path.join(src, "f%d.bin" % i), "wb") as fh:
            fh.write(b"x" * 400)
    vols = archive_dir(src, os.path.join(td, "out"), "t", volume_bytes=900)
    chk("archive volumes", len(vols) >= 2, "%d vols" % len(vols))
    chk("archive sha files", all(os.path.exists(v["path"] + ".sha256") for v in vols))

    # 4 set_readonly
    ro = os.path.join(td, "ro.txt")
    open(ro, "w").write("x")
    set_readonly(ro, True)
    w_ok = False
    try:
        open(ro, "a").write("y")
    except OSError:
        w_ok = True
    set_readonly(ro, False)
    chk("set_readonly", w_ok or IS_WIN, "windows allows write to read-only sometimes")

    # 9 git version
    gv = git_version()
    chk("git_version >= 2.38", gv >= (2, 38, 0), str(gv))

    print("\n%d checks, %d failed" % (9, len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    # P2-8 / E0：位置参数 selftest 与开关 --selftest 都要认。
    # 只认前者时，按其它脚本统一的 --selftest 习惯跑会 exit 2，看起来像
    # "平台自检不存在"，而不是"参数写错了" —— 一个把人引错方向的入口。
    arg = (sys.argv[1] if len(sys.argv) > 1 else "")
    if arg in ("selftest", "--selftest"):
        sys.exit(_selftest())
    print("usage: python platform_ops.py [selftest|--selftest]")
    sys.exit(2)
