"""Windows process helpers for agent_resume.py (standard library + ctypes only).

Replaces what the Unix version reads from /proc: parent pid, image name, start time,
liveness, waiting for an exit, the boot identity and a process's command line.
Every function returns None / False instead of raising when the process is gone or
can't be opened, so callers treat "unreadable" like "not there".
"""
import ctypes
import os
import sys
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - the module is only imported on Windows
    raise ImportError("winproc is Windows-only")

TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0
ERROR_ACCESS_DENIED = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
STATUS_BUFFER_OVERFLOW = 0x80000005
STATUS_BUFFER_TOO_SMALL = 0xC0000023
PROCESS_COMMAND_LINE_INFORMATION = 60
SYSTEM_TIME_OF_DAY_INFORMATION = 3

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_nt = ctypes.WinDLL("ntdll")
_sh32 = ctypes.WinDLL("shell32")


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_nt.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                          ctypes.POINTER(ctypes.c_ulong)]
_nt.NtQueryInformationProcess.restype = ctypes.c_ulong
_nt.NtQuerySystemInformation.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                         ctypes.POINTER(ctypes.c_ulong)]
_nt.NtQuerySystemInformation.restype = ctypes.c_ulong
_sh32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
_sh32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
_k32.LocalFree.argtypes = [ctypes.c_void_p]


def snapshot():
    """{pid: (ppid, image name)} of every process right now, or {} when it can't be read."""
    handle = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if handle in (None, INVALID_HANDLE_VALUE):
        return {}
    table = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _k32.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            ok = _k32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(handle)
    return table


def _filetime(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def start_time(pid):
    """Creation time of the process (100 ns ticks since 1601), or None. Together with the
    pid this identifies one process even when Windows reuses the number."""
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not _k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return _filetime(created)
    finally:
        _k32.CloseHandle(handle)


def alive(pid):
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        _k32.CloseHandle(handle)


def wait_exit(pid, poll=2.0):
    """Block until `pid` exits."""
    import time
    handle = _k32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        while alive(pid):
            time.sleep(poll)
        return
    try:
        while _k32.WaitForSingleObject(handle, 0xFFFFFFFF) != WAIT_OBJECT_0:
            pass
    finally:
        _k32.CloseHandle(handle)


def boot_id():
    """The kernel's boot time as text, or None. It is a 100 ns value the kernel fixes at
    boot, so it does not drift like "now minus uptime". (Fast Startup keeps it across a
    shutdown; callers also compare the shell's own start time for that case.)"""
    buffer = (ctypes.c_ubyte * 48)()
    length = ctypes.c_ulong()
    status = _nt.NtQuerySystemInformation(SYSTEM_TIME_OF_DAY_INFORMATION, buffer, 48, ctypes.byref(length))
    if status != 0:
        return None
    return "winboot:" + str(int.from_bytes(bytes(buffer[:8]), "little"))


def command_line(pid):
    """The process's argv, or None when it can't be read (gone, or another user's)."""
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = 1024
        for _ in range(3):
            buffer = ctypes.create_string_buffer(size)
            needed = ctypes.c_ulong()
            status = _nt.NtQueryInformationProcess(handle, PROCESS_COMMAND_LINE_INFORMATION, buffer, size,
                                                   ctypes.byref(needed))
            if status in (STATUS_INFO_LENGTH_MISMATCH, STATUS_BUFFER_OVERFLOW, STATUS_BUFFER_TOO_SMALL):
                size = max(needed.value, size * 2)
                continue
            if status != 0:
                return None
            # UNICODE_STRING { USHORT Length; USHORT MaximumLength; PWSTR Buffer } with the text after it.
            length = int.from_bytes(buffer.raw[0:2], "little")
            pointer = int.from_bytes(buffer.raw[8:16], "little")
            offset = pointer - ctypes.addressof(buffer)
            if length == 0 or offset < 0 or offset + length > size:
                return None
            return split_command_line(buffer.raw[offset:offset + length].decode("utf-16-le", "replace"))
        return None
    finally:
        _k32.CloseHandle(handle)


def split_command_line(text):
    """Windows' own argv splitting (CommandLineToArgvW), so quotes and spaces match what
    the program saw."""
    count = ctypes.c_int()
    parts = _sh32.CommandLineToArgvW(text, ctypes.byref(count))
    if not parts:
        return None
    try:
        return [parts[i] for i in range(count.value)]
    finally:
        _k32.LocalFree(parts)


def children(pid, table=None):
    """Direct child processes of `pid` as [(pid, image name)]."""
    table = snapshot() if table is None else table
    return [(child, name) for child, (parent, name) in table.items() if parent == pid and child != pid]
