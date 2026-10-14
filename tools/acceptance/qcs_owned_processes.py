"""Shared Windows process jobs for owned acceptance-runner child processes."""
import ctypes
from ctypes import wintypes
import time

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

class WindowsProcesses:
    """Own-job cleanup and Toolhelp snapshots, never global process-name kills."""
    def __init__(self):
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        class BasicLimit(ctypes.Structure):
            _fields_ = [("processTime", ctypes.c_int64), ("jobTime", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("minWorking", ctypes.c_size_t),
                        ("maxWorking", ctypes.c_size_t), ("activeLimit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("readOps", "writeOps", "otherOps", "readBytes", "writeBytes", "otherBytes")]
        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("basic", BasicLimit), ("io", IO), ("processMemory", ctypes.c_size_t),
                        ("jobMemory", ctypes.c_size_t), ("peakProcess", ctypes.c_size_t), ("peakJob", ctypes.c_size_t)]
        class Accounting(ctypes.Structure):
            _fields_ = [("user", ctypes.c_int64), ("kernel", ctypes.c_int64),
                        ("periodUser", ctypes.c_int64), ("periodKernel", ctypes.c_int64),
                        ("faults", wintypes.DWORD), ("total", wintypes.DWORD),
                        ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]
        class Process(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                        ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                        ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                        ("exe", wintypes.WCHAR * 260)]
        class Thread(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("tid", wintypes.DWORD),
                        ("owner", wintypes.DWORD), ("priority", wintypes.LONG),
                        ("delta", wintypes.LONG), ("flags", wintypes.DWORD)]
        self.ExtendedLimit, self.Accounting, self.Process, self.Thread = ExtendedLimit, Accounting, Process, Thread
        for name, arguments, returns in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("IsProcessInJob", [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            ("QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            ("CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            ("Process32FirstW", [wintypes.HANDLE, ctypes.POINTER(Process)], wintypes.BOOL),
            ("Process32NextW", [wintypes.HANDLE, ctypes.POINTER(Process)], wintypes.BOOL),
            ("Thread32First", [wintypes.HANDLE, ctypes.POINTER(Thread)], wintypes.BOOL),
            ("Thread32Next", [wintypes.HANDLE, ctypes.POINTER(Thread)], wintypes.BOOL),
            ("OpenThread", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("GetProcessIdOfThread", [wintypes.HANDLE], wintypes.DWORD),
            ("ResumeThread", [wintypes.HANDLE], wintypes.DWORD),
        ):
            function = getattr(self.k, name)
            function.argtypes, function.restype = arguments, returns

    def job(self, process):
        handle = self.k.CreateJobObjectW(None, None)
        require(bool(handle), "Cannot create owned process job")
        info = self.ExtendedLimit()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            require(self.k.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)), "Cannot set owned job limits")
            require(self.k.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle))), "Cannot attach owned subprocess job")
            return handle
        except BaseException:
            try:
                process.terminate()
                process.wait(timeout=20)
            finally:
                self.k.CloseHandle(handle)
            raise

    def active(self, handle):
        info = self.Accounting()
        require(self.k.QueryInformationJobObject(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None), "Cannot inspect owned process job")
        return int(info.active)

    def resume(self, process):
        # Popen used CREATE_SUSPENDED. Assign the job before any application
        # code can spawn descendants, then resume only its initial thread.
        snapshot_handle = self.k.CreateToolhelp32Snapshot(4, 0)
        require(snapshot_handle not in (None, ctypes.c_void_p(-1).value), "Cannot locate owned suspended process thread")
        threads = []
        try:
            row = self.Thread()
            row.size = ctypes.sizeof(row)
            valid = self.k.Thread32First(snapshot_handle, ctypes.byref(row))
            while valid:
                if row.owner == process.pid:
                    threads.append(int(row.tid))
                valid = self.k.Thread32Next(snapshot_handle, ctypes.byref(row))
        finally:
            self.k.CloseHandle(snapshot_handle)
        require(len(threads) == 1, "Expected exactly one suspended initial thread in the owned subprocess")
        handle = self.k.OpenThread(0x0802, False, threads[0])
        require(bool(handle), "Cannot open owned suspended initial thread")
        try:
            require(self.k.GetProcessIdOfThread(handle) == process.pid, "The suspended thread no longer belongs to this owned subprocess")
            require(self.k.ResumeThread(handle) == 1, "Owned initial thread did not have the expected suspend count")
        finally:
            self.k.CloseHandle(handle)

    def close(self, handle):
        self.k.CloseHandle(handle)

    def stop(self, handle):
        if self.active(handle):
            require(self.k.TerminateJobObject(handle, 1), "Cannot stop this runner's owned process job")
        until = time.monotonic() + 20
        while self.active(handle) and time.monotonic() < until:
            time.sleep(.02)
        return self.active(handle)

    def contains(self, job, pid):
        process = self.k.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            member = wintypes.BOOL()
            return bool(self.k.IsProcessInJob(process, job, ctypes.byref(member)) and member.value)
        finally:
            self.k.CloseHandle(process)

    def image(self, pid):
        process = self.k.OpenProcess(0x1000, False, pid)
        if not process:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            return buffer.value if self.k.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)) else None
        finally:
            self.k.CloseHandle(process)

    def all(self):
        handle = self.k.CreateToolhelp32Snapshot(2, 0)
        require(handle not in (None, ctypes.c_void_p(-1).value), "Cannot observe actual concurrent child processes")
        result = {}
        try:
            entry = self.Process()
            entry.size = ctypes.sizeof(entry)
            valid = self.k.Process32FirstW(handle, ctypes.byref(entry))
            while valid:
                result[int(entry.pid)] = {"parent": int(entry.parent), "name": str(entry.exe)}
                valid = self.k.Process32NextW(handle, ctypes.byref(entry))
        finally:
            self.k.CloseHandle(handle)
        return result
