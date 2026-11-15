#!/usr/bin/env python3
"""Platform-dependent primitives of the development-control system (ADR-000199).

Everything that differs between operating systems is isolated here so the
rest of the tooling stays portable:

* ``lock(fd)`` / ``unlock(fd)``      exclusive inter-process file lock
                                     (POSIX ``fcntl.flock``; Windows ``msvcrt.locking``)
* ``link_no_clobber(src, dst)``      create ``dst`` from ``src`` without ever
                                     overwriting (hard link; rename fallback where
                                     hard links are unsupported, callers hold a lock)
* ``boot_id()``                      identity of the current boot (Linux), or None
* ``process_start(pid)``             kernel start time of a process (Linux), or None
* ``pid_alive(pid)``                 True / False / None (unknown on this platform)
* ``session_identity(pid)``          what is recorded to verify a process later
* ``verify_session(identity)``       ALIVE / DEAD / UNKNOWN with the reason
* ``group_popen_kwargs()``           start a child in its own process group
* ``signal_group(pid, hard)``        terminate (or kill) that whole group
* ``proc_environ(pid)``              a process's initial environment (Linux), or None
* ``find_process(markers)``          the live process carrying these environment markers
* ``kill_tree(pid, grace)``          terminate a process, its group and descendants

The POSIX implementation is the supported and tested one. The Windows
branches (process existence and creation time through the Win32 API, process
trees through ``taskkill``) are written from the API documentation and are
NOT yet verified on a Windows host (docs/process/portability.md, PORT-001..006).
"""
from __future__ import annotations

import errno
import os
import socket
import sys
from pathlib import Path

WINDOWS = os.name == "nt"

if WINDOWS:  # pragma: no cover - exercised only on Windows
    import msvcrt
else:
    import fcntl

ALIVE, DEAD, UNKNOWN = "ALIVE", "DEAD", "UNKNOWN"


def utf8_stdio() -> None:
    """On Windows the console and pipes default to a legacy code page: the
    tools print and read UTF-8 (records, git output), so their own streams
    are switched to UTF-8 and unencodable characters are replaced instead of
    raising. Decoding other programs' output and files opened without an
    encoding still follow the locale: run with PYTHONUTF8=1 (the doctor warns)."""
    if not WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):  # pragma: no cover
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


utf8_stdio()


# ------------------------------------------------------------------ Win32
def _win_process(pid: int):  # pragma: no cover - exercised only on Windows
    """(exists, creation time as a string | None) from the Win32 API, or
    (None, None) when the process cannot be queried (not: does not exist)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
    ERROR_INVALID_PARAMETER, ERROR_ACCESS_DENIED = 87, 5
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        err = ctypes.get_last_error()
        if err == ERROR_INVALID_PARAMETER:
            return False, None                   # no such process
        if err == ERROR_ACCESS_DENIED:
            return True, None                    # exists, owned by someone else
        return None, None
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None, None
        if code.value != STILL_ACTIVE:
            return False, None                   # exited; the handle kept the pid readable
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in (created, exited, kernel, user))):
            return True, None
        return True, str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        k32.CloseHandle(handle)


# ------------------------------------------------------------------ locking
class WouldBlock(Exception):
    """The lock is held by another process."""


def lock(fd: int) -> None:
    """Take an exclusive lock without blocking; raise WouldBlock if held."""
    if WINDOWS:  # pragma: no cover
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            raise WouldBlock()
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise WouldBlock()


def unlock(fd: int) -> None:
    if WINDOWS:  # pragma: no cover
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


# ------------------------------------------------------------------ files
_LINK_UNSUPPORTED = {errno.EPERM, errno.ENOTSUP, errno.EXDEV, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}


def link_no_clobber(src: Path, dst: Path) -> None:
    """Make ``dst`` refer to ``src``'s content; FileExistsError if ``dst`` exists.

    Hard links are atomic and never overwrite. On filesystems without them
    the fallback checks and renames; it is race-free only among cooperating
    processes, which is why every caller holds the ``claims`` lock."""
    try:
        os.link(src, dst)
        return
    except FileExistsError:
        raise
    except OSError as e:
        if e.errno not in _LINK_UNSUPPORTED and not WINDOWS:
            raise
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "exists", str(dst))
    os.replace(src, dst)           # src is gone afterwards; the caller's unlink tolerates that


# ------------------------------------------------------------------ processes
def boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def process_start(pid: int) -> str | None:
    """Kernel start time of ``pid`` (clock ticks since boot on Linux, the
    creation FILETIME on Windows), or None."""
    if WINDOWS:  # pragma: no cover
        try:
            return _win_process(pid)[1]
        except Exception:
            return None
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError):
        return None
    # comm may contain spaces; fields after the closing parenthesis are fixed
    rest = stat[stat.rfind(")") + 2:].split()
    return rest[19] if len(rest) > 19 else None


def pid_alive(pid: int) -> bool | None:
    if pid is None or int(pid) <= 0:
        return False
    if WINDOWS:  # pragma: no cover
        try:
            return _win_process(pid)[0]
        except Exception:
            return None
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True      # exists, owned by someone else
    except OSError:
        return None


def hostname() -> str:
    return socket.gethostname()


# ------------------------------------------------------------------ process groups
def group_popen_kwargs() -> dict:
    """subprocess.Popen arguments that give the child its own process group,
    so a test runner's children can be stopped with it."""
    if WINDOWS:  # pragma: no cover
        import subprocess
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def signal_group(pid: int, hard: bool = False) -> bool:
    """Terminate (``hard``: kill) the process group led by ``pid``; False
    when it no longer exists."""
    if WINDOWS:  # pragma: no cover
        import subprocess
        # Without /F taskkill only asks windowed programs to close; a console
        # program (every agent CLI) ignores it, so soft is tried, hard works.
        r = subprocess.run(["taskkill", "/T", *(["/F"] if hard else []), "/PID", str(pid)], capture_output=True)
        return r.returncode == 0 or pid_alive(pid) is True
    import signal
    try:
        os.killpg(int(pid), signal.SIGKILL if hard else signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def session_identity(pid: int) -> dict:
    """What is recorded about a process so it can be verified later."""
    return {"host": hostname(), "boot_id": boot_id(), "pid": int(pid), "start": process_start(pid)}


def verify_session(identity: dict | None, markers: dict | None = None) -> tuple[str, str]:
    """(ALIVE|DEAD|UNKNOWN, reason) for a recorded session identity.

    A pid alone is never enough: within one boot a (pid, start time) pair
    names one process, a changed boot id means the machine restarted, a
    zombie has exited, and ``markers`` (environment variables the launched
    process carries, e.g. a worker session id) tell a reused pid from the
    process that was launched. Read-only: nothing is signalled."""
    markers = markers or {}
    if not identity or not identity.get("pid"):
        if markers:
            found = find_process(markers)
            if found:
                return ALIVE, f"no pid recorded; process {found} carries the session marker"
            if proc_environ(os.getpid()) is not None:
                return DEAD, "no pid recorded and no running process carries the session marker"
        return UNKNOWN, "no owner process recorded"
    if identity.get("host") and identity["host"] != hostname():
        return UNKNOWN, f"recorded on host {identity['host']} (this is {hostname()})"
    current_boot = boot_id()
    if identity.get("boot_id") and current_boot and identity["boot_id"] != current_boot:
        return DEAD, "recorded in an earlier boot (the machine restarted)"
    alive = pid_alive(identity["pid"])
    if alive is None:
        return UNKNOWN, "process liveness cannot be checked on this platform"
    if not alive or _zombie(identity["pid"]):
        return DEAD, f"process {identity['pid']} no longer exists"
    start = process_start(identity["pid"])
    if identity.get("start") and start and start != identity["start"]:
        return DEAD, f"pid {identity['pid']} now belongs to another process (start time changed)"
    if markers:
        env = proc_environ(identity["pid"])
        if env is not None and not all(env.get(k) == v for k, v in markers.items()):
            if identity.get("start") and start:
                return UNKNOWN, f"pid {identity['pid']} matches its start time but lacks the session marker"
            return DEAD, f"pid {identity['pid']} is another program (it lacks the session marker)"
    if identity.get("start") is None or start is None:
        return ALIVE, f"process {identity['pid']} exists (start time not verifiable)"
    return ALIVE, f"process {identity['pid']} is running"


def _zombie(pid: int) -> bool:
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[-1].split()[0] == "Z"
    except (OSError, IndexError, ValueError):
        return False


def proc_environ(pid: int) -> dict | None:
    """A process's initial environment, or None when it cannot be read."""
    try:
        raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
    except (OSError, ValueError):
        return None
    env = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep:
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


def find_process(markers: dict) -> int | None:
    """The live (non-zombie) process whose environment carries every marker."""
    proc = Path("/proc")
    if not markers or not proc.is_dir():
        return None
    for d in proc.iterdir():
        if d.name.isdigit():
            env = proc_environ(int(d.name))
            if env and all(env.get(k) == v for k, v in markers.items()) and not _zombie(int(d.name)):
                return int(d.name)
    return None


def descendants(pid: int) -> list[int]:
    children: dict[int, list[int]] = {}
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            rest = (d / "stat").read_text().rsplit(")", 1)[-1].split()
            children.setdefault(int(rest[1]), []).append(int(d.name))
        except (OSError, IndexError, ValueError):
            continue
    out, stack = [], [int(pid)]
    while stack:
        for c in children.get(stack.pop(), []):
            out.append(c)
            stack.append(c)
    return out


def kill_tree(pid: int, grace_seconds: float = 30) -> None:
    """Terminate a process, its process group and every descendant; kill what
    is still alive after the grace period. Only for processes the caller
    started (a worker's own agent session)."""
    import signal
    import time
    if WINDOWS:  # pragma: no cover
        # No process groups to signal and no SIGKILL: taskkill walks the tree.
        signal_group(pid, hard=False)
        deadline = time.time() + grace_seconds
        while time.time() < deadline and pid_alive(pid):
            time.sleep(0.2)
        signal_group(pid, hard=True)
        return
    targets = [int(pid)] + descendants(pid)
    for hard in (False, True):
        signal_group(pid, hard=hard)
        for p in targets:
            try:
                os.kill(p, signal.SIGKILL if hard else signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if hard:
            return
        deadline = time.time() + grace_seconds
        while time.time() < deadline and any(pid_alive(p) and not _zombie(p) for p in targets):
            time.sleep(0.1)
        targets = [p for p in targets if pid_alive(p) and not _zombie(p)]
        if not targets:
            return


def describe() -> dict:
    return {"platform": sys.platform, "posix": not WINDOWS, "boot_id_available": boot_id() is not None,
            "process_start_available": process_start(os.getpid()) is not None}
