"""OS-specific process-tree execution and memory-limit backends."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Protocol

Command = str | list[str]

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _JobBasicAccounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class _JobExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    for _name, _restype, _argtypes in (
        ("CreateJobObjectW", wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
        ("SetInformationJobObject", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]),
        (
            "QueryInformationJobObject",
            wintypes.BOOL,
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
        ),
        ("AssignProcessToJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
        ("TerminateJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
        ("TerminateProcess", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
        ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
    ):
        _fn = getattr(_kernel32, _name)
        _fn.restype = _restype
        _fn.argtypes = _argtypes
else:
    import resource


class ManagedProcess(Protocol):
    @property
    def args(self): ...

    @property
    def stdout(self): ...

    def has_exited(self) -> bool: ...

    def stop_soft(self) -> bool: ...

    def stop_hard(self) -> bool: ...

    def reap(self, patience: float = 5.0) -> int: ...


def _kill_group(group: int, sig: int = 9) -> None:
    """Send a signal to the process group a command leads (POSIX only).

    Linux implements killpg(group) as kill(-group), so group 1 means every
    process this user may signal and group 0 means mutator's own group. A
    command started in a new session leads a group above 1.
    """

    if group <= 1:
        raise ValueError(f"refusing to signal process group {group}")
    os.killpg(group, sig)


def _wait_until_gone(group: int, patience: float) -> None:
    """Wait until no process is left in the group, for at most `patience` seconds.

    The leader is reaped by now, so its id is no longer known to be the
    command's. Signal 0 sends nothing; it only asks whether the group exists.
    """

    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        try:
            _kill_group(group, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)


def _memory_limit_bytes(megabytes: int) -> int | None:
    """Byte cap for `megabytes` MiB, or None when disabled or larger than 64 bits."""

    if megabytes <= 0 or megabytes >= 2**44:
        return None
    return megabytes * 1024 * 1024


def _apply_posix_memory_limit(megabytes: int) -> None:
    """Lower RLIMIT_DATA (soft and hard) in the child process.

    Every process the command starts inherits the limit, and lowering the hard
    limit prevents raising it back up. An already-lower soft or hard limit stays,
    and the command still runs.
    """

    limit = _memory_limit_bytes(megabytes)
    if limit is None:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
        new_hard = hard if hard != resource.RLIM_INFINITY and hard < limit else limit
        new_soft = soft if soft != resource.RLIM_INFINITY and soft < new_hard else new_hard
        resource.setrlimit(resource.RLIMIT_DATA, (new_soft, new_hard))
    except (ValueError, OverflowError, OSError):
        pass


class _PosixProcess:
    """A POSIX process-group leader managed without reaping it early.

    Until the leader is reaped, even as a zombie, its pid stays the id of its
    group, so a signal to the group reaches only the command's processes.
    """

    def __init__(self, process: subprocess.Popen):
        self.process = process
        # Set before every call that can reap, so an exception inside one can't hide a reap.
        self._may_be_reaped = False

    @classmethod
    def spawn(
        cls,
        command: Command,
        cwd: Path,
        environment: dict[str, str],
        memory_limit: int = 0,
    ) -> _PosixProcess:
        preexec = (lambda: _apply_posix_memory_limit(memory_limit)) if _memory_limit_bytes(memory_limit) else None
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=isinstance(command, str),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=preexec,
            env=environment,
        )
        return cls(process)

    @property
    def args(self):
        return self.process.args

    @property
    def stdout(self):
        return self.process.stdout

    def signal(self, sig: int) -> bool:
        """Signal the command's group while its leader is surely unreaped. Return whether it did."""

        if self._may_be_reaped:
            return False
        _kill_group(self.process.pid, sig)
        return True

    def stop_soft(self) -> bool:
        return self.signal(signal.SIGTERM)

    def stop_hard(self) -> bool:
        return self.signal(signal.SIGKILL)

    def has_exited(self) -> bool:
        """Whether the leader has exited. Only where os.waitid is missing does this reap it."""

        if hasattr(os, "waitid"):  # macOS has it from Python 3.13
            return os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        self._may_be_reaped = True
        self._may_be_reaped = self.process.poll() is not None
        return self._may_be_reaped

    def reap(self, patience: float = 5.0) -> int:
        """Kill what is left in the group, reap the leader, and wait until the group is gone."""

        signalled = self.stop_hard()
        self._may_be_reaped = True
        code = self.process.wait()
        if signalled:
            _wait_until_gone(self.process.pid, patience)
        return code


def _create_job(memory_limit: int):
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    limit_bytes = 0
    if memory_limit > 0:
        candidate = memory_limit * 1024 * 1024
        max_size_t = (1 << (ctypes.sizeof(ctypes.c_size_t) * 8)) - 1
        if candidate <= max_size_t:
            flags |= _JOB_OBJECT_LIMIT_JOB_MEMORY
            limit_bytes = candidate
    limits = _JobExtendedLimits(LimitFlags=flags, JobMemoryLimit=limit_bytes)
    if not _kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        error = ctypes.get_last_error()
        _kernel32.CloseHandle(job)
        raise OSError(error, "SetInformationJobObject failed")
    return job


def _wait_job_empty(job, patience: float) -> None:
    deadline = time.monotonic() + patience
    accounting = _JobBasicAccounting()
    while time.monotonic() < deadline:
        ok = _kernel32.QueryInformationJobObject(
            job,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        )
        if not ok or accounting.ActiveProcesses == 0:
            return
        time.sleep(0.01)


def _resolve_windows_command(command: Command, environment: dict[str, str]) -> Command:
    """Resolve an argv program via PATH so .cmd/.bat shims run without shell=True."""

    if not isinstance(command, list) or not command:
        return command
    resolved = shutil.which(command[0], path=environment.get("PATH"))
    if resolved:
        return [resolved, *command[1:]]
    return command


class _WindowsProcess:
    """A Windows process tree managed through a Win32 Job Object."""

    def __init__(self, process: subprocess.Popen, job, assigned: bool):
        self.process = process
        self._job = job
        self._assigned = assigned

    @classmethod
    def spawn(
        cls,
        command: Command,
        cwd: Path,
        environment: dict[str, str],
        memory_limit: int = 0,
    ) -> _WindowsProcess:
        resolved = _resolve_windows_command(command, environment)
        job = _create_job(memory_limit)
        try:
            process = subprocess.Popen(
                resolved,
                cwd=cwd,
                shell=isinstance(resolved, str),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                env=environment,
            )
        except OSError:
            _kernel32.CloseHandle(job)
            raise
        assigned = bool(_kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle))))
        return cls(process, job, assigned)

    @property
    def args(self):
        return self.process.args

    @property
    def stdout(self):
        return self.process.stdout

    def has_exited(self) -> bool:
        return self.process.poll() is not None

    def stop_soft(self) -> bool:
        return self.stop_hard()

    def stop_hard(self) -> bool:
        if self._job and self._assigned:
            return bool(_kernel32.TerminateJobObject(self._job, 1))
        if self.process.poll() is None:
            return bool(_kernel32.TerminateProcess(wintypes.HANDLE(int(self.process._handle)), 1))
        return False

    def reap(self, patience: float = 5.0) -> int:
        try:
            self.stop_hard()
            code = self.process.wait()
            if self._job and self._assigned:
                _wait_job_empty(self._job, patience)
            return code
        finally:
            if self._job:
                _kernel32.CloseHandle(self._job)
                self._job = None


def spawn_process(
    command: Command,
    cwd: Path,
    environment: dict[str, str],
    memory_limit: int = 0,
) -> ManagedProcess:
    """Start `command` in an isolated process tree with an optional MiB memory limit."""

    if os.name == "nt":
        return _WindowsProcess.spawn(command, cwd, environment, memory_limit)
    return _PosixProcess.spawn(command, cwd, environment, memory_limit)
