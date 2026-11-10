"""
winproc.py —— 纯 ctypes 的 Windows 进程小工具（不依赖 psutil）

用途：让本地服务知道「是哪个浏览器把我拉起来的」，浏览器一退出就跟着收工。

  * parent_pid(pid)        取父进程 PID
  * process_name(pid)      取进程名（chrome.exe / msedge.exe …）
  * ancestors(pid)         由近到远的祖先链
  * find_browser_pid()     从当前进程往上找最顶层的浏览器进程
  * is_alive(pid, names)   PID 是否还活着（可选校验进程名，防 PID 复用）

都不是 Windows 时，全部退化为「不支持」，调用方自行兜底。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

IS_WIN = os.name == "nt"

BROWSER_NAMES = {
    "chrome.exe", "msedge.exe", "chromium.exe", "brave.exe",
    "vivaldi.exe", "opera.exe", "360chrome.exe", "360se.exe", "qqbrowser.exe",
}

if IS_WIN:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_SYNCHRONIZE = 0x00100000
    _STILL_ACTIVE = 259
    _WAIT_TIMEOUT = 0x102
    _MAX_PATH = 260

    class _PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * _MAX_PATH),
        ]

    def _snapshot():
        """返回 {pid: (ppid, name)}。"""
        out = {}
        snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snap == ctypes.c_void_p(-1).value or snap is None:
            return out
        try:
            entry = _PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
            ok = _k32.Process32First(snap, ctypes.byref(entry))
            while ok:
                name = entry.szExeFile.decode("mbcs", "replace").lower()
                out[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), name)
                ok = _k32.Process32Next(snap, ctypes.byref(entry))
        finally:
            _k32.CloseHandle(snap)
        return out

    def _query_name(pid: int) -> str:
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(_MAX_PATH)
            size = wt.DWORD(_MAX_PATH)
            if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            _k32.CloseHandle(h)
        return ""

    def _alive(pid: int) -> bool:
        if pid <= 0:
            return False
        h = _k32.OpenProcess(_PROCESS_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
                             False, pid)
        if not h:
            return False
        try:
            return _k32.WaitForSingleObject(h, 0) == _WAIT_TIMEOUT
        finally:
            _k32.CloseHandle(h)

    def parent_pid(pid: int) -> int:
        return _snapshot().get(pid, (0, ""))[0]

    def process_name(pid: int) -> str:
        name = _snapshot().get(pid, (0, ""))[1]
        return name or _query_name(pid)

    def ancestors(pid: int, limit: int = 12):
        table = _snapshot()
        chain = []
        cur = pid
        for _ in range(limit):
            ppid, _name = table.get(cur, (0, ""))
            if not ppid or ppid == cur:
                break
            chain.append(ppid)
            cur = ppid
        return chain

else:  # pragma: no cover - 只在非 Windows 上走到
    def parent_pid(pid: int) -> int:
        return 0

    def process_name(pid: int) -> str:
        return ""

    def ancestors(pid: int, limit: int = 12):
        return []

    def _alive(pid: int) -> bool:
        return False


def is_alive(pid: int, expect_names=None) -> bool:
    """PID 活着（可选：进程名必须属于 expect_names，避免 PID 被复用后误判）。"""
    if not _alive(pid):
        return False
    if expect_names:
        name = process_name(pid)
        if name and name not in {n.lower() for n in expect_names}:
            return False
    return True


def find_browser_pid(start_pid: int = None) -> int:
    """从 start_pid（默认当前进程）往上找**最顶层**的浏览器进程 PID。

    返回 0 表示没找到（比如从 cmd 手动启动）。
    """
    pid = start_pid or os.getpid()
    best = 0
    for anc in ancestors(pid):
        if process_name(anc) in BROWSER_NAMES:
            best = anc
    return best


if __name__ == "__main__":
    me = os.getpid()
    print(f"当前进程 {me} ({process_name(me)})")
    for a in ancestors(me):
        print(f"  ← {a}  {process_name(a)}")
    print("浏览器根进程:", find_browser_pid(), process_name(find_browser_pid()))
