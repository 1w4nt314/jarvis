"""What differs between macOS and Windows, decided in one place.

JARVIS was written on a Mac, and most of it never cared: FastAPI, SQLite,
stream-json and Fish Audio behave the same everywhere. What does differ is
small but sharp, and every piece of it lives here (pure Python) or in
`windows_native.py` (ctypes, Windows only) so the rest of the code asks a
question instead of testing `sys.platform` itself:

* **Starting `claude`.** On Windows an npm install puts `claude.cmd` on PATH,
  a batch-file shim. Spawning a shim runs `cmd.exe`, which re-parses every
  argument: the brain's multi-line `--append-system-prompt` is cut at the
  first newline and its `--settings` JSON loses its quotes. So a shim is
  never spawned — `split_command` reads it and runs what it points at
  (`node cli.js`, or a native `claude.exe`) directly. A native install's
  `claude.exe` needs nothing.
* **Splitting a configured command.** `shlex.split` in POSIX mode eats the
  backslashes out of `C:\\Users\\...`, so on Windows a path is never split.
* **Is a pid alive.** `os.kill(pid, 0)` is the POSIX probe. On Windows the
  same call is `TerminateProcess(pid, 0)`: it KILLS the process. That is the
  one bug in this list that is dangerous rather than merely broken — the
  session watcher polls every Claude Code process on the machine once a
  second — so `pid_alive` never calls `os.kill` on Windows.

No new dependencies: `ctypes` is the standard library.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from pathlib import Path, PureWindowsPath

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def platform_name() -> str:
    """"macOS", "Windows" or "Linux" — for messages a person reads."""
    if IS_MAC:
        return "macOS"
    if IS_WINDOWS:
        return "Windows"
    return "Linux"


# ── processes ─────────────────────────────────────────────────────────────

def pid_alive(pid) -> bool:
    """True if a process with this pid exists. Never signals it.

    `pid` must be a positive integer: 0 means "this process's group" and a
    negative pid means "that group" to `os.kill`, neither of which is a real
    process, so both are rejected before the syscall.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        # NEVER os.kill here: on Windows it terminates the process.
        import windows_native
        return windows_native.pid_alive(pid)
    try:
        os.kill(pid, 0)    # signal 0: checks, touches nothing (POSIX only)
    except OSError:
        return False
    return True


def kill_tree(pid: int, timeout: float = 10.0) -> bool:
    """Windows: end `pid` AND everything it started. True if taskkill agreed.

    On POSIX a run is stopped with SIGTERM and Claude Code takes its own
    children (a dev server it started, its MCP servers) down with it. Windows
    has no SIGTERM for a console-less child — `terminate()` is
    TerminateProcess, which gives the process no chance to clean up — so
    without `/T` every cancelled run would leave its children running.
    """
    import subprocess
    try:
        out = subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                             capture_output=True, timeout=timeout,
                             **no_window_flags())
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return out.returncode == 0


# ── starting a command ────────────────────────────────────────────────────

_SHIM_SUFFIXES = (".cmd", ".bat")
_SCRIPT_SUFFIXES = (".js", ".mjs", ".cjs")

# The targets an npm `cmd-shim` runs, each written as "%dp0%\<relative path>"
# (or %~dp0). The node.exe probe in the same file is matched too and skipped.
_SHIM_TARGET = re.compile(r'"%~?dp0%?\\([^"%]+)"', re.IGNORECASE)


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def resolve_shim(shim: str) -> list[str] | None:
    """The argv a Windows npm `.cmd` shim would run, without running cmd.exe.

    None if the file cannot be read or does not look like a shim we
    understand; the caller then falls back to the shim itself, which still
    works for simple arguments.
    """
    try:
        text = Path(shim).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    base = Path(shim).parent
    target = None
    for rel in _SHIM_TARGET.findall(text):
        parts = PureWindowsPath(rel).parts
        if not parts or parts[-1].lower() == "node.exe":
            continue
        target = base.joinpath(*parts)
    if target is None or not target.exists():
        return None
    if target.suffix.lower() in _SCRIPT_SUFFIXES:
        local_node = base / "node.exe"
        node = str(local_node) if local_node.exists() else shutil.which("node")
        if not node:
            return None
        return [node, str(target)]
    return [str(target)]


def split_command(spec: str) -> list[str]:
    """A configured command (`JARVIS_CLAUDE_PATH`, or what `which` found) as argv.

    POSIX: `shlex.split`, exactly as before. Windows: a path that exists is
    one argument even with spaces in it, anything else is split without
    eating backslashes, and a `.cmd`/`.bat` shim is replaced by what it runs
    (see the module docstring for why spawning a shim is not an option).
    """
    if not IS_WINDOWS:
        return shlex.split(spec)
    spec = spec.strip()
    if os.path.exists(_unquote(spec)):
        argv = [_unquote(spec)]
    else:
        argv = [_unquote(t) for t in shlex.split(spec, posix=False)]
    if not argv:
        return argv
    head = argv[0]
    if not os.path.splitext(head)[1]:
        found = shutil.which(head)
        if found:
            head = found
    if head.lower().endswith(_SHIM_SUFFIXES):
        unwrapped = resolve_shim(head)
        if unwrapped:
            return unwrapped + argv[1:]
    return [head] + argv[1:]


def find_claude() -> str | None:
    """Where `claude` is, or None. On Windows the native installer's
    `claude.exe` lives in ~/.local/bin, which a fresh shell may not have on
    PATH yet, so that is looked at too."""
    found = shutil.which("claude")
    if found:
        return found
    if IS_WINDOWS:
        native = Path.home() / ".local" / "bin" / "claude.exe"
        if native.exists():
            return str(native)
    return None


def claude_command(configured: str | None = None) -> list[str]:
    """argv[0:n] that starts Claude Code: the configured command if any,
    else whatever `find_claude` finds, else plain "claude" (which then fails
    to spawn with an error that names it)."""
    return split_command(configured or find_claude() or "claude")


# ── a session's inbox ─────────────────────────────────────────────────────

_PIPE_PREFIXES = ("\\\\.\\pipe\\", "\\\\?\\pipe\\")


def is_named_pipe(path: str | None) -> bool:
    r"""A Windows named-pipe path (`\\.\pipe\name`).

    Node — and so Claude Code — cannot listen on a Unix socket file on
    Windows; a session's inbox there is a named pipe.
    """
    return bool(path) and path.lower().startswith(_PIPE_PREFIXES)


def ipc_endpoint_exists(path: str | None) -> bool:
    r"""Whether a session's inbox socket (or named pipe) is there to talk to.

    A pipe is looked up by LISTING `\\.\pipe\`, never by stat or open:
    opening a named pipe connects to it, and a liveness poll that runs every
    second must not keep connecting to other sessions' inboxes.
    """
    if not path:
        return False
    if is_named_pipe(path):
        if not IS_WINDOWS:
            return False
        name = path[len(_PIPE_PREFIXES[0]):]
        try:
            return name.lower() in {n.lower() for n in os.listdir(_PIPE_PREFIXES[0])}
        except OSError:
            return False
    return Path(path).exists()


# ── opening a new console window ──────────────────────────────────────────

def new_console_flags() -> dict:
    """Popen kwargs that give a child its own visible console on Windows.

    On Windows 11 with Windows Terminal set as the default terminal this
    opens a Windows Terminal tab; otherwise a classic console window.
    Empty off Windows.
    """
    if not IS_WINDOWS:
        return {}
    return {"creationflags": 0x00000010}          # CREATE_NEW_CONSOLE


def no_window_flags() -> dict:
    """Popen kwargs that keep a helper process from flashing a console."""
    if not IS_WINDOWS:
        return {}
    return {"creationflags": 0x08000000}          # CREATE_NO_WINDOW
