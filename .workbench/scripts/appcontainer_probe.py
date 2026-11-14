#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
appcontainer_probe.py —— 第二十五轮：Windows AppContainer 真装配探针 / 回滚器 / 平台矩阵

性质：
  1. --inspect   只读导出探测，不创建 profile、不改 ACL、不起进程。
  2. --assemble  唯一会触碰系统状态的入口：创建一次性 AppContainer profile、
     最小 DACL、STARTUPINFOEX 子进程探针、失败注入与绕过尝试，finally 清理。
  3. --cleanup   独立回滚器，删除指定 profile 并检查残留。
  4. --check     回归门禁：只读检查 matrix / 运行台账 / EXCLUDE / 源码接线真值，
     不跑 --assemble，避免每次全量回归改动系统状态。

诚实边界：
  * 本探针不参与 W4 三证，不进 capability_evidence/ledger.json，不把 os_sandbox 涂成 enforced。
  * state/appcontainer_probe.jsonl 是运行态台账，必须进 sign_manifest.EXCLUDE。
  * capability_evidence/appcontainer/matrix.json 是稳定证据，进可信根；易变 profile/SID 留在运行台账。
  * 非 Windows 平台只写 NOT_WINDOWS / UNTESTED，不把别的平台结论伪装成通过。
  * AppContainer 真装配会创建 profile / SID / capability SID / ACL / 进程 token；清理失败即残留红。
'''
import ast
import ctypes as ct
import hashlib
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get('WB_HOME') or os.path.dirname(HERE)
CAPDIR_REL = 'capability_evidence/appcontainer'
MATRIX_REL = CAPDIR_REL + '/matrix.json'
LOG_REL = 'state/appcontainer_probe.jsonl'
SCHEMA = 1
CRITERION = 'appcontainer_matrix_v1'
CONFIRM = 'ASSEMBLE'

CAPABILITY_SIDS = {
    'internetClient': 'S-1-15-3-1',
    'internetClientServer': 'S-1-15-3-2',
    'privateNetworkClientServer': 'S-1-15-3-3',
    'picturesLibrary': 'S-1-15-3-4',
    'videosLibrary': 'S-1-15-3-5',
    'musicLibrary': 'S-1-15-3-6',
    'documentsLibrary': 'S-1-15-3-7',
    'enterpriseAuthentication': 'S-1-15-3-8',
    'sharedUserCertificates': 'S-1-15-3-9',
    'removableStorage': 'S-1-15-3-10',
    'appointments': 'S-1-15-3-11',
    'contacts': 'S-1-15-3-12',
}

# Win32 常量
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
SE_GROUP_ENABLED = 0x00000004
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
GRANT_ACCESS = 1
TRUSTEE_IS_SID = 0
TRUSTEE_IS_UNKNOWN = 0
SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
FILE_ALL_ACCESS = 0x001F01FF
WAIT_TIMEOUT = 258
TOKEN_QUERY = 0x0008
TOKEN_IS_APP_CONTAINER_CLASS = 29
TOKEN_APP_CONTAINER_SID_CLASS = 31
HRESULT_FROM_WIN32_ERROR_NOT_FOUND = 0x80070490
# AppContainer profile 的 per-user 注册映射键；DeleteAppContainerProfile 成功后该键应消失。
# 用它判「profile 是否仍注册」，不用 DeriveAppContainerSidFromAppContainerName（那只是名称→SID 纯派生，删了也恒成功）。
APPCONTAINER_MAPPINGS_KEY = r'Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer\Mappings'

# ctypes 结构：不用 ctypes.wintypes，保证非 Windows import 不炸。
DWORD = ct.c_uint32
WORD = ct.c_uint16
BOOL = ct.c_int
HRESULT = ct.c_int32
HANDLE = ct.c_void_p
LPCWSTR = ct.c_wchar_p
LPWSTR = ct.c_wchar_p
SIZE_T = ct.c_size_t
DWORD_PTR = ct.c_size_t


class SID_AND_ATTRIBUTES(ct.Structure):
    _fields_ = [('Sid', ct.c_void_p), ('Attributes', DWORD)]


class SECURITY_CAPABILITIES(ct.Structure):
    _fields_ = [
        ('AppContainerSid', ct.c_void_p),
        ('Capabilities', ct.POINTER(SID_AND_ATTRIBUTES)),
        ('CapabilityCount', DWORD),
        ('Reserved', DWORD),
    ]


class STARTUPINFOW(ct.Structure):
    _fields_ = [
        ('cb', DWORD), ('lpReserved', LPWSTR), ('lpDesktop', LPWSTR), ('lpTitle', LPWSTR),
        ('dwX', DWORD), ('dwY', DWORD), ('dwXSize', DWORD), ('dwYSize', DWORD),
        ('dwXCountChars', DWORD), ('dwYCountChars', DWORD), ('dwFillAttribute', DWORD),
        ('dwFlags', DWORD), ('wShowWindow', WORD), ('cbReserved2', WORD),
        ('lpReserved2', ct.c_void_p), ('hStdInput', HANDLE), ('hStdOutput', HANDLE),
        ('hStdError', HANDLE),
    ]


class STARTUPINFOEXW(ct.Structure):
    _fields_ = [('StartupInfo', STARTUPINFOW), ('lpAttributeList', ct.c_void_p)]


class PROCESS_INFORMATION(ct.Structure):
    _fields_ = [
        ('hProcess', HANDLE), ('hThread', HANDLE),
        ('dwProcessId', DWORD), ('dwThreadId', DWORD),
    ]


class TOKEN_APPCONTAINER_INFORMATION(ct.Structure):
    _fields_ = [('TokenAppContainer', ct.c_void_p)]


class TRUSTEE_W(ct.Structure):
    _fields_ = [
        ('pMultipleTrustee', ct.c_void_p), ('MultipleTrusteeOperation', ct.c_int),
        ('TrusteeForm', ct.c_int), ('TrusteeType', ct.c_int), ('ptstrName', ct.c_void_p),
    ]


class EXPLICIT_ACCESS_W(ct.Structure):
    _fields_ = [
        ('grfAccessPermissions', DWORD), ('grfAccessMode', ct.c_int),
        ('grfInheritance', DWORD), ('Trustee', TRUSTEE_W),
    ]


def _now_ms():
    return int(time.time() * 1000)


def _u32(x):
    return int(x) & 0xffffffff


def _wb(wb=None):
    return wb or WB


def _capdir(wb=None):
    return os.path.join(_wb(wb), CAPDIR_REL.replace('/', os.sep))


def _matrix_path(wb=None):
    return os.path.join(_wb(wb), MATRIX_REL.replace('/', os.sep))


def _log_path(wb=None):
    return os.path.join(_wb(wb), LOG_REL.replace('/', os.sep))


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\n')
    return path


def _read_json(path):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def _read_text(path, default=''):
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return fh.read()
    except OSError:
        return default


def _clip(s, n=800):
    s = str(s)
    return s if len(s) <= n else s[:n] + '...<clip>'


def record(kind, body, wb=None, now_ms=None):
    '''best-effort 运行态台账；不抛异常，不进可信根，w4_participates 恒 false。'''
    row = {
        'schema': SCHEMA,
        'ts_ms': int(now_ms if now_ms is not None else _now_ms()),
        'kind': kind,
        'w4_participates': False,
        'data': body,
    }
    p = _log_path(wb)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'a', encoding='utf-8', newline='\n') as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
        return {'ok': True, 'path': LOG_REL}
    except Exception as exc:
        return {'ok': False, 'path': LOG_REL, 'error': '%s: %s' % (type(exc).__name__, exc)}


def rows(wb=None):
    p = _log_path(wb)
    good, bad = [], []
    if not os.path.isfile(p):
        return good, bad
    with open(p, encoding='utf-8', errors='replace') as fh:
        for i, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                good.append(json.loads(line))
            except ValueError as exc:
                bad.append({'line': i, 'error': '%s: %s' % (type(exc).__name__, exc), 'text': _clip(line.strip())})
    return good, bad


def report(wb=None, limit=20):
    good, bad = rows(wb)
    last = good[-limit:] if good else []
    residual = [r for r in good if r.get('data', {}).get('residual') is True or r.get('data', {}).get('verdict') == 'PROFILE_RESIDUAL']
    return {
        'exists': os.path.isfile(_log_path(wb)),
        'total': len(good),
        'bad_lines': bad,
        'residual_rows': len(residual),
        'last': last,
        'w4_participates': False,
        'note': '运行台账不是事件链；空台账不代表真装配已完成，坏行/残留必须红。',
    }


class _Win(object):
    '''延迟加载 Win32 API；非 win32 主机不构造。'''

    def __init__(self):
        if sys.platform != 'win32':
            raise RuntimeError('NOT_WINDOWS: AppContainer 是 Windows 原生原语')
        self.k32 = ct.WinDLL('kernel32', use_last_error=True)
        self.userenv = ct.WinDLL('userenv', use_last_error=True)
        self.advapi32 = ct.WinDLL('advapi32', use_last_error=True)
        try:
            self.kernelbase = ct.WinDLL('kernelbase', use_last_error=True)
        except OSError:
            self.kernelbase = None
        self._decl()

    def _decl(self):
        psa = ct.POINTER(SID_AND_ATTRIBUTES)
        self.userenv.CreateAppContainerProfile.argtypes = [LPCWSTR, LPCWSTR, LPCWSTR, psa, DWORD, ct.POINTER(ct.c_void_p)]
        self.userenv.CreateAppContainerProfile.restype = HRESULT
        self.userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [LPCWSTR, ct.POINTER(ct.c_void_p)]
        self.userenv.DeriveAppContainerSidFromAppContainerName.restype = HRESULT
        self.userenv.DeleteAppContainerProfile.argtypes = [LPCWSTR]
        self.userenv.DeleteAppContainerProfile.restype = HRESULT
        try:
            self.userenv.GetAppContainerFolderPath.argtypes = [LPCWSTR, ct.POINTER(ct.c_void_p)]
            self.userenv.GetAppContainerFolderPath.restype = HRESULT
        except AttributeError:
            pass
        self.advapi32.ConvertStringSidToSidW.argtypes = [LPCWSTR, ct.POINTER(ct.c_void_p)]
        self.advapi32.ConvertStringSidToSidW.restype = BOOL
        self.advapi32.ConvertSidToStringSidW.argtypes = [ct.c_void_p, ct.POINTER(ct.c_void_p)]
        self.advapi32.ConvertSidToStringSidW.restype = BOOL
        self.advapi32.GetNamedSecurityInfoW.argtypes = [LPCWSTR, ct.c_int, DWORD, ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_void_p)]
        self.advapi32.GetNamedSecurityInfoW.restype = DWORD
        self.advapi32.SetNamedSecurityInfoW.argtypes = [LPCWSTR, ct.c_int, DWORD, ct.c_void_p, ct.c_void_p, ct.c_void_p, ct.c_void_p]
        self.advapi32.SetNamedSecurityInfoW.restype = DWORD
        self.advapi32.SetEntriesInAclW.argtypes = [DWORD, ct.POINTER(EXPLICIT_ACCESS_W), ct.c_void_p, ct.POINTER(ct.c_void_p)]
        self.advapi32.SetEntriesInAclW.restype = DWORD
        self.advapi32.OpenProcessToken.argtypes = [HANDLE, DWORD, ct.POINTER(HANDLE)]
        self.advapi32.OpenProcessToken.restype = BOOL
        self.advapi32.GetTokenInformation.argtypes = [HANDLE, ct.c_int, ct.c_void_p, DWORD, ct.POINTER(DWORD)]
        self.advapi32.GetTokenInformation.restype = BOOL
        self.k32.LocalFree.argtypes = [ct.c_void_p]
        self.k32.LocalFree.restype = ct.c_void_p
        self.k32.InitializeProcThreadAttributeList.argtypes = [ct.c_void_p, DWORD, DWORD, ct.POINTER(SIZE_T)]
        self.k32.InitializeProcThreadAttributeList.restype = BOOL
        self.k32.UpdateProcThreadAttribute.argtypes = [ct.c_void_p, DWORD, DWORD_PTR, ct.c_void_p, SIZE_T, ct.c_void_p, ct.POINTER(SIZE_T)]
        self.k32.UpdateProcThreadAttribute.restype = BOOL
        self.k32.DeleteProcThreadAttributeList.argtypes = [ct.c_void_p]
        self.k32.DeleteProcThreadAttributeList.restype = None
        self.k32.CreateProcessW.argtypes = [LPCWSTR, LPWSTR, ct.c_void_p, ct.c_void_p, BOOL, DWORD, ct.c_void_p, LPCWSTR, ct.c_void_p, ct.POINTER(PROCESS_INFORMATION)]
        self.k32.CreateProcessW.restype = BOOL
        self.k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
        self.k32.WaitForSingleObject.restype = DWORD
        self.k32.GetExitCodeProcess.argtypes = [HANDLE, ct.POINTER(DWORD)]
        self.k32.GetExitCodeProcess.restype = BOOL
        self.k32.TerminateProcess.argtypes = [HANDLE, DWORD]
        self.k32.TerminateProcess.restype = BOOL
        self.k32.CloseHandle.argtypes = [HANDLE]
        self.k32.CloseHandle.restype = BOOL
        # 权威 token 证据：OpenProcessToken + GetTokenInformation。
        # whoami /user 与 WindowsIdentity.User.Value 是可读辅助证据；groups 只看 group SIDs，
        # 会漏掉 AppContainer package SID。三者一起看，避免拿文本输出冒充 token 真值。

    @staticmethod
    def _has(dll, name):
        try:
            getattr(dll, name)
            return True
        except AttributeError:
            return False

    def api(self):
        api = {
            'userenv': {n: self._has(self.userenv, n) for n in ('CreateAppContainerProfile', 'DeriveAppContainerSidFromAppContainerName', 'DeleteAppContainerProfile', 'GetAppContainerFolderPath')},
            'kernel32': {n: self._has(self.k32, n) for n in ('CreateProcessW', 'InitializeProcThreadAttributeList', 'UpdateProcThreadAttribute', 'DeleteProcThreadAttributeList', 'WaitForSingleObject', 'GetExitCodeProcess', 'CloseHandle')},
            'advapi32': {n: self._has(self.advapi32, n) for n in ('ConvertStringSidToSidW', 'ConvertSidToStringSidW', 'GetNamedSecurityInfoW', 'SetNamedSecurityInfoW', 'SetEntriesInAclW', 'OpenProcessToken', 'GetTokenInformation')},
            'kernelbase': {n: self._has(self.kernelbase, n) for n in ('DeriveCapabilitySidsFromName',)} if self.kernelbase else {},
            'registry': {'profile_key': 'HKCU\\' + APPCONTAINER_MAPPINGS_KEY},
        }
        required = [
            api['userenv']['CreateAppContainerProfile'], api['userenv']['DeriveAppContainerSidFromAppContainerName'], api['userenv']['DeleteAppContainerProfile'],
            api['kernel32']['CreateProcessW'], api['kernel32']['InitializeProcThreadAttributeList'], api['kernel32']['UpdateProcThreadAttribute'],
            api['advapi32']['ConvertSidToStringSidW'], api['advapi32']['GetNamedSecurityInfoW'], api['advapi32']['SetNamedSecurityInfoW'], api['advapi32']['SetEntriesInAclW'],
            api['advapi32']['OpenProcessToken'], api['advapi32']['GetTokenInformation'],
        ]
        api['ready'] = all(required)
        return api

    def sid_to_string(self, sid):
        out = ct.c_void_p()
        if not sid:
            return ''
        ok = self.advapi32.ConvertSidToStringSidW(sid, ct.byref(out))
        if not ok or not out.value:
            return ''
        return ct.wstring_at(out.value)

    def string_to_sid(self, sid_text):
        p = ct.c_void_p()
        ok = self.advapi32.ConvertStringSidToSidW(sid_text, ct.byref(p))
        if not ok or not p.value:
            raise OSError('ConvertStringSidToSidW failed for %s: %s' % (sid_text, ct.get_last_error()))
        return p

    def capability_sids(self, names):
        out = []
        for name in names:
            if name not in CAPABILITY_SIDS:
                raise ValueError('UNKNOWN_CAPABILITY: %s' % name)
            sid_text = CAPABILITY_SIDS[name]
            out.append({'name': name, 'sid_text': sid_text, 'sid': self.string_to_sid(sid_text)})
        return out

    def create_profile(self, name, caps):
        cap_arr = None
        cap_ptr = None
        if caps:
            cap_arr = (SID_AND_ATTRIBUTES * len(caps))()
            for i, cap in enumerate(caps):
                cap_arr[i].Sid = cap['sid']
                cap_arr[i].Attributes = SE_GROUP_ENABLED
            cap_ptr = ct.cast(cap_arr, ct.POINTER(SID_AND_ATTRIBUTES))
        sid = ct.c_void_p()
        hr = self.userenv.CreateAppContainerProfile(name, name, 'workbench one-shot probe', cap_ptr, len(caps), ct.byref(sid))
        return _u32(hr), sid, cap_arr, cap_ptr

    def derive_profile(self, name):
        sid = ct.c_void_p()
        hr = self.userenv.DeriveAppContainerSidFromAppContainerName(name, ct.byref(sid))
        return _u32(hr), sid

    def profile_folder(self, name):
        try:
            out = ct.c_void_p()
            hr = self.userenv.GetAppContainerFolderPath(name, ct.byref(out))
            if _u32(hr) == 0 and out.value:
                return ct.wstring_at(out.value)
        except Exception:
            return None
        return None

    def delete_profile(self, name):
        hr = self.userenv.DeleteAppContainerProfile(name)
        return _u32(hr)

    def profile_exists(self, name):
        """查 HKCU AppContainer Mappings 判断 profile 是否仍注册。

        DeriveAppContainerSidFromAppContainerName 是名称→SID 的纯派生：
        删除 profile 后仍可能返回成功，只能作观察值，不能证明注册残留。
        读注册表失败时按「存在」处理（fail-closed），避免把未知判成干净。
        """
        hr, sid = self.derive_profile(name)
        sid_text = self.sid_to_string(sid)
        exists = False
        self.profile_exists_error = None
        if sys.platform == 'win32':
            key = None
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPCONTAINER_MAPPINGS_KEY)
                count = winreg.QueryInfoKey(key)[0]
                for i in range(count):
                    sub = winreg.EnumKey(key, i)
                    if sub == name or (sid_text and sub == sid_text):
                        exists = True
                        break
                    sk = None
                    try:
                        sk = winreg.OpenKey(key, sub)
                        nvals = winreg.QueryInfoKey(sk)[1]
                        vals = [str(winreg.EnumValue(sk, j)[1]) for j in range(nvals)]
                        if any(name == v or name in v for v in vals):
                            exists = True
                            break
                    except OSError:
                        continue
                    finally:
                        if sk is not None:
                            try:
                                winreg.CloseKey(sk)
                            except OSError:
                                pass
            except FileNotFoundError:
                exists = False
            except OSError as exc:
                self.profile_exists_error = '%s: %s' % (type(exc).__name__, exc)
                exists = True
            finally:
                if key is not None:
                    try:
                        winreg.CloseKey(key)
                    except OSError:
                        pass
        return exists, hr, sid_text


def _win_api_inspect():
    if sys.platform != 'win32':
        return {'platform': sys.platform, 'verdict': 'NOT_WINDOWS', 'api': {'ready': False}}
    try:
        win = _Win()
        api = win.api()
        return {
            'platform': 'win32',
            'windows_version': str(sys.getwindowsversion()),
            'api': api,
            'capability_sids': CAPABILITY_SIDS,
            'verdict': 'APPCONTAINER_EXPORT_INSPECT_ONLY' if api.get('ready') else 'APPCONTAINER_API_MISSING',
            'assembly_tested': False,
            'note': '只查导出符号，不创建 profile；真装配必须显式 --assemble --confirm-token ASSEMBLE。',
        }
    except Exception as exc:
        return {'platform': 'win32', 'verdict': 'APPCONTAINER_API_MISSING', 'api': {'ready': False}, 'error': '%s: %s' % (type(exc).__name__, exc)}


def _default_matrix():
    return {
        'schema': SCHEMA,
        'criterion': CRITERION,
        'w4_participates': False,
        'rows': [
            {'platform': 'win32', 'verdict': 'INSPECT_ONLY', 'assembly_tested': False, 'api_ready': False, 'capabilities_tested': [], 'residual': False, 'unresolved_gaps': [], 'evidence': [], 'note': '当前节点待 inspect/assemble。'},
            {'platform': 'linux', 'verdict': 'UNTESTED', 'assembly_tested': False, 'api_ready': False, 'capabilities_tested': [], 'residual': False, 'unresolved_gaps': ['需要 Linux runner：namespace/seccomp/Landlock/AppArmor 对照实验'], 'evidence': [], 'note': 'Win32 主机不产出 Linux 实机结论。'},
            {'platform': 'macos', 'verdict': 'UNTESTED', 'assembly_tested': False, 'api_ready': False, 'capabilities_tested': [], 'residual': False, 'unresolved_gaps': ['需要 macOS runner：App Sandbox / Seatbelt / entitlement 对照实验'], 'evidence': [], 'note': 'Win32 主机不产出 macOS 实机结论。'},
        ],
        'note': '平台矩阵不是 W4 三证；Linux/macOS 的 UNTESTED 不得改写成 PASS。',
    }


def _row_by_platform(matrix, platform):
    for row in matrix.get('rows', []):
        if row.get('platform') == platform:
            return row
    row = {'platform': platform, 'verdict': 'UNTESTED', 'assembly_tested': False, 'api_ready': False, 'capabilities_tested': [], 'residual': False, 'unresolved_gaps': [], 'evidence': []}
    matrix.setdefault('rows', []).append(row)
    return row


def update_matrix(inspect=None, assembly=None, wb=None):
    p = _matrix_path(wb)
    try:
        matrix = _read_json(p)
    except (OSError, ValueError):
        matrix = _default_matrix()
    if not matrix.get('rows'):
        matrix = _default_matrix()
    matrix['schema'] = SCHEMA
    matrix['criterion'] = CRITERION
    matrix['w4_participates'] = False
    matrix['note'] = '平台矩阵不是 W4 三证；Linux/macOS 的 UNTESTED 不得改写成 PASS。'
    if inspect is not None:
        row = _row_by_platform(matrix, inspect.get('platform', sys.platform))
        row['api_ready'] = bool(inspect.get('api', {}).get('ready'))
        row['windows_version'] = inspect.get('windows_version')
        row['inspect_verdict'] = inspect.get('verdict')
        row['capability_sids'] = inspect.get('capability_sids', CAPABILITY_SIDS)
        if row['verdict'] == 'INSPECT_ONLY' and inspect.get('verdict') == 'APPCONTAINER_EXPORT_INSPECT_ONLY':
            row['verdict'] = 'INSPECT_ONLY'
        row['note'] = '只读导出探测完成；真装配结论以 assemble 证据为准。'
    if assembly is not None:
        row = _row_by_platform(matrix, 'win32')
        caps = sorted(set(list(row.get('capabilities_tested', [])) + list(assembly.get('capabilities_tested', []))))
        row['capabilities_tested'] = caps
        row['assembly_tested'] = bool(assembly.get('assembly_tested'))
        row['residual'] = bool(assembly.get('residual'))
        row['verdict'] = assembly.get('verdict', 'PROBE_INCONCLUSIVE')
        row['unresolved_gaps'] = sorted(set(assembly.get('unresolved_gaps', [])))
        row['evidence'] = sorted(set(assembly.get('evidence', [])))
        row['api_ready'] = True
        row['note'] = 'Windows 真装配证据落在 capability_evidence/appcontainer；易变 profile/SID 在排除的 state/appcontainer_probe.jsonl。'
    _write_json(p, matrix)
    return matrix


def inspect(write=False, wb=None):
    result = _win_api_inspect()
    if write:
        update_matrix(inspect=result, wb=wb)
    return result


def _grant_fixture_dacl(win, path, app_sid):
    old_sd = ct.c_void_p()
    old_dacl = ct.c_void_p()
    rc = win.advapi32.GetNamedSecurityInfoW(path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, ct.byref(old_dacl), None, ct.byref(old_sd))
    if rc != 0:
        return {'ok': False, 'error': 'GetNamedSecurityInfoW failed: %s' % rc, 'restore': None}
    ea = EXPLICIT_ACCESS_W()
    ea.grfAccessPermissions = FILE_ALL_ACCESS
    ea.grfAccessMode = GRANT_ACCESS
    ea.grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT
    ea.Trustee.pMultipleTrustee = None
    ea.Trustee.MultipleTrusteeOperation = 0
    ea.Trustee.TrusteeForm = TRUSTEE_IS_SID
    ea.Trustee.TrusteeType = TRUSTEE_IS_UNKNOWN
    ea.Trustee.ptstrName = app_sid
    new_dacl = ct.c_void_p()
    rc = win.advapi32.SetEntriesInAclW(1, ct.byref(ea), old_dacl, ct.byref(new_dacl))
    if rc != 0:
        if old_sd.value:
            win.k32.LocalFree(old_sd)
        return {'ok': False, 'error': 'SetEntriesInAclW failed: %s' % rc, 'restore': None}
    rc = win.advapi32.SetNamedSecurityInfoW(path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, new_dacl, None)
    if new_dacl.value:
        win.k32.LocalFree(new_dacl)
    if rc != 0:
        if old_sd.value:
            win.k32.LocalFree(old_sd)
        return {'ok': False, 'error': 'SetNamedSecurityInfoW grant failed: %s' % rc, 'restore': None}
    return {
        'ok': True,
        'detail': 'fixture DACL 已临时授权给一次性 AppContainer SID',
        'restore': {
            'path': path,
            'old_sd': old_sd.value,
            'old_dacl': old_dacl.value,
        },
    }


def _restore_fixture_dacl(win, restore):
    if not restore:
        return {'ok': True, 'detail': 'no DACL change'}
    old_dacl = ct.c_void_p(restore.get('old_dacl'))
    rc = win.advapi32.SetNamedSecurityInfoW(restore['path'], SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, old_dacl, None)
    old_sd = ct.c_void_p(restore.get('old_sd'))
    if old_sd.value:
        win.k32.LocalFree(old_sd)
    if rc != 0:
        return {'ok': False, 'error': 'SetNamedSecurityInfoW restore failed: %s' % rc}
    return {'ok': True, 'detail': 'DACL restored'}


def _find_bin(name):
    root = os.environ.get('SystemRoot') or r'C:\Windows'
    candidates = [
        os.path.join(root, 'System32', name),
        os.path.join(root, 'System32', 'WindowsPowerShell', 'v1.0', name),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _copy_bins(fixture_dir):
    copied = {}
    for name in ('cmd.exe', 'whoami.exe', 'curl.exe', 'powershell.exe'):
        src = _find_bin(name)
        if not src:
            copied[name] = False
            continue
        try:
            shutil.copy2(src, os.path.join(fixture_dir, name))
            copied[name] = True
        except OSError:
            copied[name] = False
    return copied


class _OKHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'OK')

    def log_message(self, fmt, *args):
        return


def _start_http_server():
    srv = http.server.HTTPServer(('127.0.0.1', 0), _OKHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, th, srv.server_address[1]


def _env_block(fixture_dir):
    root = os.environ.get('SystemRoot') or r'C:\Windows'
    env = {
        'SystemRoot': root,
        'windir': root,
        'PATH': os.path.join(root, 'System32') + ';' + fixture_dir,
        'PATHEXT': '.COM;.EXE;.BAT;.CMD',
        'TEMP': fixture_dir,
        'TMP': fixture_dir,
        'USERPROFILE': fixture_dir,
        'APPDATA': fixture_dir,
        'LOCALAPPDATA': fixture_dir,
        'PYTHONIOENCODING': 'utf-8',
    }
    sep = chr(0)
    envstr = sep.join(['%s=%s' % (k, v) for k, v in env.items()]) + sep + sep
    arr = (ct.c_wchar * (len(envstr) + 2))()
    for i, ch in enumerate(envstr):
        arr[i] = ch
    arr[len(envstr)] = sep
    arr[len(envstr) + 1] = sep
    return arr, ct.cast(arr, ct.c_void_p)


def _token_info(win, process_handle):
    info = {'ok': False, 'is_appcontainer': False, 'appcontainer_sid': ''}
    htok = HANDLE()
    if not win.advapi32.OpenProcessToken(process_handle, DWORD(TOKEN_QUERY), ct.byref(htok)):
        info['error'] = 'OpenProcessToken failed: %s' % ct.get_last_error()
        return info
    try:
        is_ac = DWORD(0)
        need = DWORD(ct.sizeof(is_ac))
        ok = win.advapi32.GetTokenInformation(
            htok, ct.c_int(TOKEN_IS_APP_CONTAINER_CLASS),
            ct.cast(ct.byref(is_ac), ct.c_void_p),
            DWORD(ct.sizeof(is_ac)), ct.byref(need))
        if ok:
            info['is_appcontainer'] = bool(is_ac.value)
        else:
            info['is_appcontainer_error'] = 'GetTokenInformation(TokenIsAppContainer) failed: %s' % ct.get_last_error()
        need = DWORD(0)
        sized = win.advapi32.GetTokenInformation(
            htok, ct.c_int(TOKEN_APP_CONTAINER_SID_CLASS),
            None, DWORD(0), ct.byref(need))
        size_err = ct.get_last_error()
        if not sized and need.value == 0:
            info['appcontainer_sid_error'] = 'GetTokenInformation(TokenAppContainerSid) size failed: %s' % size_err
        else:
            length = need.value or ct.sizeof(TOKEN_APPCONTAINER_INFORMATION)
            buf = ct.create_string_buffer(length)
            need2 = DWORD(length)
            ok2 = win.advapi32.GetTokenInformation(
                htok, ct.c_int(TOKEN_APP_CONTAINER_SID_CLASS),
                ct.cast(buf, ct.c_void_p), need2, ct.byref(need2))
            if ok2:
                taci = ct.cast(buf, ct.POINTER(TOKEN_APPCONTAINER_INFORMATION)).contents
                sid_ptr = taci.TokenAppContainer
                info['appcontainer_sid_pointer'] = bool(sid_ptr)
                if sid_ptr:
                    info['appcontainer_sid'] = win.sid_to_string(sid_ptr)
            else:
                info['appcontainer_sid_error'] = 'GetTokenInformation(TokenAppContainerSid) failed: %s' % ct.get_last_error()
        info['ok'] = ('is_appcontainer_error' not in info and 'appcontainer_sid_error' not in info)
    finally:
        win.k32.CloseHandle(htok)
    return info


def _create_process(win, app_path, cmdline, cwd, env_ptr, startupinfo, extended=False, timeout_ms=15000):
    si = startupinfo
    pi = PROCESS_INFORMATION()
    cmd_buf = ct.create_unicode_buffer(cmdline, len(cmdline) + 2)
    flags = CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW
    if extended:
        flags |= EXTENDED_STARTUPINFO_PRESENT
    # 微软 Launch an AppContainer 示例用 lpApplicationName=NULL + 完整命令行；
    # 传非 NULL app_path 时本机实测子 token 没有变成 AppContainer，这里按文档形态对齐。
    ok = win.k32.CreateProcessW(None, cmd_buf, None, None, False, flags, env_ptr, cwd, ct.byref(si), ct.byref(pi))
    if not ok:
        return {'ok': False, 'error': 'CreateProcessW failed: %s' % ct.get_last_error(), 'exit_code': None}
    wait = win.k32.WaitForSingleObject(pi.hProcess, DWORD(timeout_ms))
    timed_out = wait == WAIT_TIMEOUT
    if timed_out:
        win.k32.TerminateProcess(pi.hProcess, DWORD(1))
    code = DWORD(0)
    win.k32.GetExitCodeProcess(pi.hProcess, ct.byref(code))
    token = _token_info(win, pi.hProcess)
    win.k32.CloseHandle(pi.hThread)
    win.k32.CloseHandle(pi.hProcess)
    return {'ok': True, 'exit_code': int(code.value), 'timed_out': timed_out, 'wait_rc': int(wait), 'token': token}


def _container_startupinfo(win, app_sid, cap_ptr, cap_count):
    size = SIZE_T(0)
    ok = win.k32.InitializeProcThreadAttributeList(None, DWORD(1), DWORD(0), ct.byref(size))
    if ok or size.value == 0:
        if not size.value:
            raise OSError('InitializeProcThreadAttributeList size failed: %s' % ct.get_last_error())
    attr_buf = ct.create_string_buffer(size.value)
    ok = win.k32.InitializeProcThreadAttributeList(attr_buf, DWORD(1), DWORD(0), ct.byref(size))
    if not ok:
        raise OSError('InitializeProcThreadAttributeList failed: %s' % ct.get_last_error())
    sec = SECURITY_CAPABILITIES()
    sec.AppContainerSid = app_sid
    sec.Capabilities = cap_ptr
    sec.CapabilityCount = DWORD(cap_count)
    sec.Reserved = DWORD(0)
    ok = win.k32.UpdateProcThreadAttribute(attr_buf, DWORD(0), DWORD_PTR(PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES), ct.cast(ct.byref(sec), ct.c_void_p), SIZE_T(ct.sizeof(sec)), None, None)
    if not ok:
        win.k32.DeleteProcThreadAttributeList(attr_buf)
        raise OSError('UpdateProcThreadAttribute failed: %s' % ct.get_last_error())
    si = STARTUPINFOEXW()
    si.StartupInfo.cb = DWORD(ct.sizeof(si))
    si.lpAttributeList = ct.cast(attr_buf, ct.c_void_p)
    return si, attr_buf, sec


def _plain_startupinfo():
    si = STARTUPINFOW()
    si.cb = DWORD(ct.sizeof(si))
    return si


def _bad_extended_startupinfo():
    si = STARTUPINFOEXW()
    si.StartupInfo.cb = DWORD(ct.sizeof(si))
    si.lpAttributeList = None
    return si


def _child_inner(port):
    parts = [
        'whoami.exe /groups > token.txt 2>&1',
        'whoami.exe /user > user.txt 2>&1',
        'type allowed.txt > read.txt 2>&1',
        'echo WRITE_OK > allowed_write.txt',
        'echo OUTSIDE_WRITE > ../outside/outside_write.txt 2> outside_err.txt',
        'copy "%SystemRoot%/win.ini" "../outside/copy.ini" > copy_out.txt 2>&1',
        'curl.exe -sS --max-time 2 http://127.0.0.1:%d/ > net.txt 2>&1' % int(port),
        'powershell.exe -NoProfile -Command "[Security.Principal.WindowsIdentity]::GetCurrent().Groups.Value" > ps_groups.txt 2>&1',
        'powershell.exe -NoProfile -Command "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value" > ps_user.txt 2>&1',
    ]
    return ' & '.join(parts)


def _cap_array(caps):
    if not caps:
        return None, None, 0
    arr = (SID_AND_ATTRIBUTES * len(caps))()
    for i, cap in enumerate(caps):
        arr[i].Sid = cap['sid']
        arr[i].Attributes = SE_GROUP_ENABLED
    return ct.cast(arr, ct.POINTER(SID_AND_ATTRIBUTES)), arr, len(caps)


def _safe_remove_tree(path):
    if not path:
        return True
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        return not os.path.exists(path)
    except Exception:
        return not os.path.exists(path)


def _cleanup_common(win, profile_name, created_by_us, profile_folder, restore, probe_root, wb=None):
    delete_hr = 0
    if created_by_us and win is not None:
        delete_hr = win.delete_profile(profile_name)
    exists, derive_hr, derive_sid = (False, 0, '')
    profile_exists_error = None
    if win is not None:
        exists, derive_hr, derive_sid = win.profile_exists(profile_name)
        profile_exists_error = getattr(win, 'profile_exists_error', None)
    folder_left = False
    if profile_folder and os.path.exists(profile_folder):
        # 只删自己这个一次性 profile 的目录；路径里必须带唯一名或 Wb 前缀。
        if profile_name in profile_folder or os.path.basename(profile_folder).startswith('WbAppContainerProbe'):
            _safe_remove_tree(profile_folder)
        folder_left = os.path.exists(profile_folder)
    dacl = {'ok': True, 'detail': 'no DACL change'}
    if win is not None and restore:
        dacl = _restore_fixture_dacl(win, restore)
    fixture_removed = _safe_remove_tree(probe_root)
    residual = bool(exists or folder_left or not dacl.get('ok') or not fixture_removed or profile_exists_error)
    detail = {
        'profile_name': profile_name,
        'delete_hr': '0x%08x' % _u32(delete_hr),
        'derive_after_delete_hr': '0x%08x' % _u32(derive_hr),
        'derive_after_delete_sid': derive_sid,
        'profile_mapping_exists_after_delete': exists,
        'profile_exists_error': profile_exists_error,
        'profile_folder': profile_folder,
        'profile_folder_left': folder_left,
        'dacl_restore': dacl,
        'probe_root': probe_root,
        'fixture_removed': fixture_removed,
        'residual': residual,
    }
    record('cleanup', detail, wb=wb)
    return detail


def _write_evidence(wb, real, injection, bypass, cleanup):
    d = _capdir(wb)
    os.makedirs(d, exist_ok=True)
    paths = []
    for name, obj in (('real.json', real), ('injection.json', injection), ('bypass.json', bypass), ('cleanup.json', cleanup)):
        p = os.path.join(d, name)
        _write_json(p, obj)
        paths.append(CAPDIR_REL + '/' + name)
    return paths


def assemble(capnames=None, profile_name=None, confirm=None, wb=None):
    capnames = list(capnames or [])
    if confirm != CONFIRM:
        return {'ok': False, 'verdict': 'CONFIRM_REQUIRED', 'reason': '真装配会改系统状态，必须显式 --confirm-token ASSEMBLE'}
    if sys.platform != 'win32':
        update_matrix(assembly={'assembly_tested': False, 'verdict': 'NOT_WINDOWS', 'capabilities_tested': capnames, 'residual': False, 'unresolved_gaps': ['非 Windows 主机未实机验证'], 'evidence': []}, wb=wb)
        return {'ok': True, 'verdict': 'NOT_WINDOWS', 'reason': '非 Windows 主机不创建 AppContainer profile'}
    profile_name = profile_name or ('WbAppContainerProbe-%s-%d' % (uuid.uuid4().hex[:12], _now_ms()))
    probe_root = tempfile.mkdtemp(prefix='wb-appcontainer-')
    fixture_dir = os.path.join(probe_root, 'allowed')
    outside_dir = os.path.join(probe_root, 'outside')
    os.makedirs(fixture_dir, exist_ok=True)
    os.makedirs(outside_dir, exist_ok=True)
    with open(os.path.join(fixture_dir, 'allowed.txt'), 'w', encoding='utf-8') as fh:
        fh.write('ALLOWED_READ' + '\n')
    copied = _copy_bins(fixture_dir)
    cmd_path = os.path.join(fixture_dir, 'cmd.exe')
    win = None
    created_by_us = False
    profile_folder = None
    restore = None
    app_sid = None
    cap_objs = []
    cap_ptr = None
    cap_arr = None
    cap_count = 0
    real = {'ok': False}
    injection = {'ok': False}
    bypass = {'ok': False, 'bypass_reached': True}
    cleanup_detail = {'residual': True, 'reason': 'cleanup not reached'}
    verdict = 'PROBE_INCONCLUSIVE'
    unresolved = []
    srv = None
    th = None
    port = 0
    try:
        win = _Win()
        api = win.api()
        if not api.get('ready'):
            verdict = 'APPCONTAINER_API_MISSING'
            unresolved.append('API 不齐：%s' % json.dumps(api, ensure_ascii=False, sort_keys=True))
            raise RuntimeError(verdict)
        try:
            cap_objs = win.capability_sids(capnames)
        except Exception as exc:
            unresolved.append('capability SID 转换失败，降级为空 capability: %s' % exc)
            cap_objs = []
        cap_ptr, cap_arr, cap_count = _cap_array(cap_objs)
        hr, app_sid, create_arr, create_ptr = win.create_profile(profile_name, cap_objs)
        create_cap_ptr = create_ptr or cap_ptr
        create_cap_arr = create_arr or cap_arr
        cap_count = len(cap_objs)
        cap_ptr = create_cap_ptr
        cap_arr = create_cap_arr
        if _u32(hr) != 0 and cap_objs:
            unresolved.append('带 capability 创建失败(0x%08x)，降级为空 capability' % _u32(hr))
            cap_objs = []
            cap_ptr, cap_arr, cap_count = None, None, 0
            hr, app_sid, create_arr, create_ptr = win.create_profile(profile_name, [])
        if _u32(hr) != 0:
            verdict = 'PROBE_INCONCLUSIVE'
            unresolved.append('CreateAppContainerProfile failed: 0x%08x' % _u32(hr))
            raise RuntimeError(unresolved[-1])
        created_by_us = True
        app_sid_str = win.sid_to_string(app_sid)
        dhr, derived_sid = win.derive_profile(profile_name)
        derived_sid_str = win.sid_to_string(derived_sid)
        profile_folder = win.profile_folder(profile_name)
        if not app_sid_str:
            unresolved.append('ConvertSidToStringSidW 无法读出 AppContainer SID')
        if derived_sid_str and app_sid_str and derived_sid_str != app_sid_str:
            unresolved.append('Create/Derive SID 不一致')
        acl = _grant_fixture_dacl(win, fixture_dir, app_sid)
        if not acl.get('ok'):
            unresolved.append('fixture DACL grant failed: %s' % acl.get('error'))
            raise RuntimeError(unresolved[-1])
        restore = acl.get('restore')
        if not copied.get('cmd.exe') or not os.path.isfile(cmd_path):
            unresolved.append('cmd.exe 未复制成功')
            raise RuntimeError(unresolved[-1])
        env_arr, env_ptr = _env_block(fixture_dir)
        srv, th, port = _start_http_server()
        inner = _child_inner(port)
        cmdline = '"{}" /c {}'.format(cmd_path, inner)
        si, attr_buf, sec = _container_startupinfo(win, app_sid, cap_ptr, cap_count)
        child = _create_process(win, cmd_path, cmdline, fixture_dir, env_ptr, si, extended=True, timeout_ms=15000)
        try:
            win.k32.DeleteProcThreadAttributeList(attr_buf)
        except Exception:
            pass
        token_txt = _read_text(os.path.join(fixture_dir, 'token.txt'))
        user_txt = _read_text(os.path.join(fixture_dir, 'user.txt'))
        ps_txt = _read_text(os.path.join(fixture_dir, 'ps_groups.txt'))
        ps_user_txt = _read_text(os.path.join(fixture_dir, 'ps_user.txt'))
        token_texts = (token_txt, user_txt, ps_txt, ps_user_txt)
        child_token = child.get("token") or {}
        token_is_appcontainer = bool(child_token.get("is_appcontainer"))
        token_appcontainer_sid = child_token.get("appcontainer_sid") or ""
        token_sid_seen = bool(app_sid_str and (any(app_sid_str in t for t in token_texts) or (token_is_appcontainer and token_appcontainer_sid == app_sid_str)))
        cap_seen = {c['name']: any(c['sid_text'] in t for t in token_texts) for c in cap_objs}
        fixture_write_ok = 'WRITE_OK' in _read_text(os.path.join(fixture_dir, 'allowed_write.txt'))
        outside_write = os.path.isfile(os.path.join(outside_dir, 'outside_write.txt'))
        outside_copy = os.path.isfile(os.path.join(outside_dir, 'copy.ini'))
        net_txt = _read_text(os.path.join(fixture_dir, 'net.txt'))
        loopback_connected = ('OK' in net_txt and 'curl:' not in net_txt)
        loopback_untested = ('curl.exe' in net_txt and 'not recognized' in net_txt) or not net_txt.strip()
        # 失败注入：丢掉 EXTENDED_STARTUPINFO_PRESENT 或扩展属性，普通 token 不应带 AppContainer SID。
        inj_si = _plain_startupinfo()
        inj_cmdline = '"{}" /c whoami.exe /user > inject_user.txt 2>&1 & whoami.exe /groups > inject_token.txt 2>&1'.format(cmd_path)
        inj = _create_process(win, cmd_path, inj_cmdline, fixture_dir, env_ptr, inj_si, extended=False, timeout_ms=10000)
        inj_token = _read_text(os.path.join(fixture_dir, 'inject_token.txt'))
        inj_user = _read_text(os.path.join(fixture_dir, 'inject_user.txt'))
        inj_token_api = inj.get("token") or {}
        inj_normal_is_appcontainer = bool(inj_token_api.get("is_appcontainer"))
        inj_normal_has_sid = bool(app_sid_str and (app_sid_str in inj_token or app_sid_str in inj_user)) or inj_normal_is_appcontainer
        bad_si = _bad_extended_startupinfo()
        bad_cmdline = '"{}" /c whoami.exe /user > inject_bad_user.txt 2>&1 & whoami.exe /groups > inject_bad.txt 2>&1'.format(cmd_path)
        bad = _create_process(win, cmd_path, bad_cmdline, fixture_dir, env_ptr, bad_si, extended=True, timeout_ms=10000)
        bad_token = _read_text(os.path.join(fixture_dir, 'inject_bad.txt'))
        bad_user = _read_text(os.path.join(fixture_dir, 'inject_bad_user.txt'))
        bad_token_api = bad.get("token") or {}
        bad_is_appcontainer = bool(bad_token_api.get("is_appcontainer"))
        bad_has_sid = bool(bad.get("ok") and (bad_is_appcontainer or (app_sid_str and (app_sid_str in bad_token or app_sid_str in bad_user))))
        injection = {
            'ok': bool(inj.get('ok') and not inj_normal_has_sid and (not bad.get('ok') or not bad_has_sid)),
            'plain_process': inj,
            'plain_token_contains_appcontainer_sid': inj_normal_has_sid,
            'plain_user_tail': _clip(inj_user),
            'plain_token': inj_token_api,
            'plain_token_is_appcontainer': inj_normal_is_appcontainer,
            'bad_extended_process': bad,
            'bad_extended_token_contains_appcontainer_sid': bad_has_sid,
            'bad_extended_user_tail': _clip(bad_user),
            'bad_extended_token': bad_token_api,
            'bad_extended_token_is_appcontainer': bad_is_appcontainer,
            'verdict': 'INJECTION_DETECTED' if (inj.get('ok') and not inj_normal_has_sid and (not bad.get('ok') or not bad_has_sid)) else 'INJECTION_INCONCLUSIVE',
            'reason': '缺 EXTENDED_STARTUPINFO_PRESENT 或空 attribute list 必须不再产生 AppContainer token。',
        }
        bypass_reached = bool(outside_write or outside_copy or bad_has_sid)
        bypass = {
            'ok': not bypass_reached,
            'bypass_reached': bypass_reached,
            'outside_write': outside_write,
            'outside_copy': outside_copy,
            'loopback_connected': loopback_connected,
            'loopback_untested': loopback_untested,
            'loopback_capability_context': [c['name'] for c in cap_objs],
            'curl_output': _clip(net_txt),
            'verdict': 'BYPASS_BLOCKED' if not bypass_reached else 'APPCONTAINER_GAP',
            'reason': '越界写/复制成功或坏扩展启动仍带 AppContainer SID 即为绕过；loopback 在 internetClient 上下文下单独观察，不冒充 W4 通过。',
        }
        real = {
            'ok': bool(child.get('ok') and token_is_appcontainer and token_sid_seen and fixture_write_ok and not outside_write and not outside_copy),
            'profile_name': profile_name,
            'profile_sid': app_sid_str,
            'derived_sid': derived_sid_str,
            'create_hr': '0x%08x' % _u32(hr),
            'derive_hr': '0x%08x' % _u32(dhr),
            'profile_folder': profile_folder,
            'capabilities': [{'name': c['name'], 'sid': c['sid_text'], 'seen_in_token': cap_seen.get(c['name'], False)} for c in cap_objs],
            'copied_bins': copied,
            'child': child,
            'token_sid_seen': token_sid_seen,
            'token_is_appcontainer': token_is_appcontainer,
            'token_appcontainer_sid': token_appcontainer_sid,
            'token_api': child_token,
            'fixture_write_ok': fixture_write_ok,
            'outside_write': outside_write,
            'outside_copy': outside_copy,
            'loopback_connected': loopback_connected,
            'loopback_untested': loopback_untested,
            'token_tail': _clip(token_txt),
            'user_tail': _clip(user_txt),
            'ps_groups_tail': _clip(ps_txt),
            'ps_user_tail': _clip(ps_user_txt),
            'verdict': 'APPCONTAINER_TOKEN_REAL' if (child.get('ok') and token_is_appcontainer and token_sid_seen) else 'APPCONTAINER_TOKEN_UNPROVEN',
        }
        if loopback_untested:
            unresolved.append('curl loopback probe UNTESTED（curl 不可运行或输出为空）')
        if loopback_connected and not cap_objs:
            unresolved.append('空 capability 下 loopback 可连：需单独复核网络隔离')
    except Exception as exc:
        unresolved.append('%s: %s' % (type(exc).__name__, exc))
    finally:
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        cleanup_detail = _cleanup_common(win, profile_name, created_by_us, profile_folder, restore, probe_root, wb=wb)
        if cleanup_detail.get('residual'):
            verdict = 'PROFILE_RESIDUAL'
        elif bypass.get('bypass_reached') or bypass.get('verdict') == 'APPCONTAINER_GAP':
            verdict = 'APPCONTAINER_GAP'
        elif not real.get('ok') or not injection.get('ok'):
            verdict = 'PROBE_INCONCLUSIVE'
            unresolved.append('real/injection 未成套：real.ok=%r injection.ok=%r' % (real.get('ok'), injection.get('ok')))
        else:
            verdict = 'APPCONTAINER_ASSEMBLED_VALIDATED'
    evidence_paths = _write_evidence(wb, real, injection, bypass, cleanup_detail)
    result = {
        'ok': verdict == 'APPCONTAINER_ASSEMBLED_VALIDATED',
        'verdict': verdict,
        'profile_name': profile_name,
        'capabilities_requested': capnames,
        'capabilities_used': [c['name'] for c in cap_objs],
        'real': real,
        'injection': injection,
        'bypass': bypass,
        'cleanup': cleanup_detail,
        'unresolved_gaps': sorted(set(unresolved)),
        'evidence': evidence_paths,
        'w4_participates': False,
    }
    record('assemble', {'verdict': verdict, 'ok': result['ok'], 'residual': cleanup_detail.get('residual'), 'profile_name': profile_name, 'profile_sid': real.get('profile_sid'), 'probe_root': probe_root, 'capabilities': result['capabilities_used']}, wb=wb)
    update_matrix(assembly={'assembly_tested': result['ok'], 'verdict': verdict, 'capabilities_tested': result['capabilities_used'], 'residual': cleanup_detail.get('residual'), 'unresolved_gaps': result['unresolved_gaps'], 'evidence': evidence_paths}, wb=wb)
    return result


def cleanup(profile_name=None, wb=None):
    good, bad = rows(wb)
    if not profile_name or profile_name == 'auto':
        for r in reversed(good):
            d = r.get('data', {})
            if r.get('kind') == 'assemble' and d.get('profile_name'):
                profile_name = d['profile_name']
                break
    if not profile_name:
        return {'ok': False, 'verdict': 'NO_PROFILE', 'reason': '没有 profile 名，也没有可回滚的运行台账'}
    probe_root = None
    profile_folder = None
    for r in reversed(good):
        d = r.get('data', {})
        if d.get('profile_name') == profile_name:
            probe_root = d.get('probe_root') or probe_root
            profile_folder = d.get('profile_folder') or profile_folder
    win = None
    if sys.platform == 'win32':
        try:
            win = _Win()
            if not profile_folder:
                profile_folder = win.profile_folder(profile_name)
        except Exception:
            win = None
    detail = _cleanup_common(win, profile_name, True, profile_folder, None, probe_root, wb=wb)
    detail['verdict'] = 'PROFILE_RESIDUAL' if detail.get('residual') else 'CLEANUP_OK'
    record('cleanup_standalone', detail, wb=wb)
    if detail.get('residual'):
        update_matrix(assembly={'assembly_tested': True, 'verdict': 'PROFILE_RESIDUAL', 'capabilities_tested': [], 'residual': True, 'unresolved_gaps': ['standalone cleanup still sees residual'], 'evidence': []}, wb=wb)
    return detail


def _source_truth_exclude():
    p = os.path.join(HERE, 'sign_manifest.py')
    tree = ast.parse(open(p, encoding='utf-8').read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == 'EXCLUDE':
                    try:
                        return LOG_REL in list(ast.literal_eval(node.value))
                    except Exception:
                        return False
    return False


def source_truth():
    src = open(__file__, encoding='utf-8').read()
    tree = ast.parse(src)
    fn = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn.add(node.name)
    cleanup_in_finally = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and node.finalbody:
            blob = ''.join(ast.dump(x) for x in node.finalbody)
            if any(k in blob for k in ('cleanup', 'restore', 'DeleteAppContainerProfile', 'delete_profile', 'safe_remove')):
                cleanup_in_finally = True
    po = open(os.path.join(HERE, 'platform_ops.py'), encoding='utf-8').read()
    return {
        'exclude_ok': _source_truth_exclude(),
        'has_inspect': 'inspect' in fn,
        'has_assemble': 'assemble' in fn,
        'has_cleanup': 'cleanup' in fn,
        'has_check': 'check' in fn,
        'cleanup_in_finally': cleanup_in_finally,
        'create_profile_called': 'CreateAppContainerProfile' in src,
        'delete_profile_called': 'DeleteAppContainerProfile' in src,
        'dacl_restore_called': '_restore_fixture_dacl' in src,
        'w4_participates_false': "'w4_participates': False" in src,
        'not_wired_into_spawn_group': 'CreateAppContainerProfile' not in po,
        'matrix_w4_false': "'w4_participates': False" in src,
    }


def check(wb=None):
    truth = source_truth()
    required = ('exclude_ok', 'has_inspect', 'has_assemble', 'has_cleanup', 'has_check', 'cleanup_in_finally', 'create_profile_called', 'delete_profile_called', 'dacl_restore_called', 'w4_participates_false', 'not_wired_into_spawn_group')
    bad = [k for k in required if not truth.get(k)]
    if bad:
        print('FAIL: appcontainer source truth broken: %s' % ','.join(bad))
        return 1
    good, bad_lines = rows(wb)
    if bad_lines:
        print('FAIL: appcontainer runtime log has bad lines: %s' % json.dumps(bad_lines, ensure_ascii=False))
        return 1
    residual_rows = [r for r in good if r.get('data', {}).get('residual') is True or r.get('data', {}).get('verdict') == 'PROFILE_RESIDUAL']
    if residual_rows:
        print('FAIL: appcontainer runtime log has residual rows: %d' % len(residual_rows))
        return 1
    latest_assemble = None
    for r in good:
        if r.get('kind') == 'assemble':
            latest_assemble = r
    fail_verdicts = {'PROFILE_RESIDUAL', 'APPCONTAINER_GAP', 'PROBE_INCONCLUSIVE', 'APPCONTAINER_API_MISSING', 'CONFIRM_REQUIRED'}
    if latest_assemble and (latest_assemble.get('data', {}).get('verdict') in fail_verdicts or latest_assemble.get('data', {}).get('ok') is False):
        print('FAIL: latest appcontainer assemble is not clean: %s' % latest_assemble.get('data', {}).get('verdict'))
        return 1
    try:
        matrix = _read_json(_matrix_path(wb))
    except (OSError, ValueError) as exc:
        print('FAIL: appcontainer matrix missing/unreadable: %s' % exc)
        return 1
    if matrix.get('w4_participates') is not False:
        print('FAIL: appcontainer matrix w4_participates must be false')
        return 1
    rows_by_pf = {r.get('platform'): r for r in matrix.get('rows', [])}
    if sys.platform == 'win32':
        w = rows_by_pf.get('win32') or {}
        if w.get('verdict') in fail_verdicts:
            print('FAIL: appcontainer matrix verdict=%s' % w.get('verdict'))
            return 1
        if w.get('assembly_tested') is not True:
            print('FAIL: Windows 真装配未在 matrix 里留证')
            return 1
        if w.get('residual') is True:
            print('FAIL: appcontainer matrix says residual')
            return 1
        for rel in w.get('evidence', []):
            p = os.path.join(_wb(wb), rel.replace('/', os.sep))
            if not (os.path.isfile(p) and os.path.getsize(p) > 0):
                print('FAIL: appcontainer evidence missing/empty: %s' % rel)
                return 1
    print('PASS: appcontainer matrix/check clean (wired=%s, log_rows=%d)' % (truth.get('has_assemble'), len(good)))
    return 0


def selftest():
    nchecks = [0]
    fails = []

    def ck(name, cond):
        nchecks[0] += 1
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix='wb-appcontainer-selftest-')
    try:
        ins = inspect(write=False)
        ck('inspect never assembles', ins.get('assembly_tested') is False)
        truth = source_truth()
        ck('source truth all green', all(truth.get(k) for k in ('exclude_ok', 'has_inspect', 'has_assemble', 'has_cleanup', 'cleanup_in_finally', 'not_wired_into_spawn_group')))
        # 临时 WB：复制生产 matrix 结构，避免依赖生产运行台账。
        matrix = _default_matrix()
        matrix['rows'][0]['verdict'] = 'APPCONTAINER_ASSEMBLED_VALIDATED'
        matrix['rows'][0]['assembly_tested'] = True
        matrix['rows'][0]['residual'] = False
        matrix['rows'][0]['evidence'] = []
        _write_json(_matrix_path(tmp), matrix)
        ck('empty runtime log does not fail', check(wb=tmp) == 0)
        record('assemble', {'verdict': 'APPCONTAINER_ASSEMBLED_VALIDATED', 'ok': True, 'residual': False, 'profile_name': 'selftest-profile'}, wb=tmp)
        ck('good row passes', check(wb=tmp) == 0)
        record('cleanup', {'residual': True, 'verdict': 'PROFILE_RESIDUAL'}, wb=tmp)
        ck('residual row red', check(wb=tmp) == 1)
        # 坏行检测：故意追加非法 JSON，check 必须红。
        with open(_log_path(tmp), 'a', encoding='utf-8') as fh:
            fh.write('{bad json' + '\n')
        ck('bad line red', check(wb=tmp) == 1)
        good, bad = rows(tmp)
        ck('rows captures bad line', len(bad) == 1)
        rep = report(tmp)
        ck('report says w4 separate', rep.get('w4_participates') is False)
        ck('matrix default w4 false', _read_json(_matrix_path(tmp)).get('w4_participates') is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('appcontainer_probe: PASS (%d checks, %d failed)' % (nchecks[0], len(fails)) if not fails else 'appcontainer_probe: FAIL %s' % fails)
    return 1 if fails else 0


def _value(argv, flag, default=None):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def _values(argv, flag):
    out = []
    for i, x in enumerate(argv):
        if x == flag and i + 1 < len(argv):
            out.extend([a for a in argv[i + 1].split(',') if a])
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    wb = _value(argv, '--wb')
    if '--selftest' in argv:
        return selftest()
    if '--inspect' in argv:
        res = inspect(write=('--write' in argv), wb=wb)
        if '--json' in argv:
            print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print('INSPECT %s api_ready=%s' % (res.get('verdict'), res.get('api', {}).get('ready')))
        return 0
    if '--check' in argv:
        return check(wb=wb)
    if 'report' in argv or '--report' in argv:
        print(json.dumps(report(wb=wb), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if '--assemble' in argv:
        caps = _values(argv, '--cap')
        profile = _value(argv, '--profile', 'auto')
        if profile == 'auto':
            profile = None
        res = assemble(capnames=caps, profile_name=profile, confirm=_value(argv, '--confirm-token'), wb=wb)
        if '--json' in argv:
            print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print('ASSEMBLE %s ok=%s caps=%s residual=%s' % (res.get('verdict'), res.get('ok'), res.get('capabilities_used'), res.get('cleanup', {}).get('residual')))
        return 0 if res.get('ok') else 1
    if '--cleanup' in argv:
        res = cleanup(_value(argv, '--profile', 'auto'), wb=wb)
        print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if not res.get('residual') else 1
    print('usage: appcontainer_probe.py [--inspect [--write]] [--assemble --confirm-token ASSEMBLE --cap internetClient] [--cleanup --profile NAME] [--check] [report] [--selftest] [--json]')
    return 0


if __name__ == '__main__':
    sys.exit(main())
