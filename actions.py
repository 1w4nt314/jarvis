"""
JARVIS Action Executor — system actions on macOS and Windows.

macOS drives Terminal.app and the browsers over AppleScript. Windows has no
AppleScript and needs none: a console window is a `cmd.exe` started with
its own console, and a browser or editor is its executable started with the
target as one argv entry — never through a shell, never through
`os.startfile` for anything that could be a program.

Execute actions IMMEDIATELY, before generating any LLM response.
Each function returns {"success": bool, "confirmation": str}.
"""

import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import platform_support

log = logging.getLogger("jarvis.actions")


def _is_windows() -> bool:
    # Read at call time, not import time, so a test can stand in for either.
    return sys.platform == "win32"


async def _spawn_detached(argv, *, cwd: str | None = None, **kwargs) -> bool:
    """Start a GUI program (or a console window) and do not wait for it.

    A browser that was not already running stays alive as long as the user
    keeps it open, so waiting for it would hang the caller for hours.
    """
    def _go() -> bool:
        subprocess.Popen(argv, cwd=cwd or None, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **kwargs)
        return True
    try:
        return await asyncio.to_thread(_go)
    except OSError as e:
        log.error(f"could not start {argv!r}: {e}")
        return False

async def _mark_terminal_as_jarvis(revert_after: float = 5.0):
    """Temporarily set the front Terminal window to Ocean theme, then revert.

    Shows the user JARVIS is active in that terminal. Reverts after revert_after seconds.
    """
    # Save the current profile, switch to Ocean, then revert
    script_save = (
        'tell application "Terminal"\n'
        '    return name of current settings of front window\n'
        'end tell'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script_save,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        original_profile = stdout.decode().strip()

        # Switch to Ocean
        script_set = (
            'tell application "Terminal"\n'
            '    set current settings of front window to settings set "Ocean"\n'
            'end tell'
        )
        proc2 = await asyncio.create_subprocess_exec(
            "osascript", "-e", script_set,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc2.communicate()

        # Schedule revert
        if original_profile and original_profile != "Ocean":
            asyncio.get_event_loop().call_later(
                revert_after,
                lambda: asyncio.ensure_future(_revert_terminal_theme(original_profile))
            )
    except Exception:
        pass


async def _revert_terminal_theme(profile_name: str):
    """Revert a Terminal window back to its original profile."""
    escaped = applescript_escape(profile_name)
    script = (
        'tell application "Terminal"\n'
        f'    set current settings of front window to settings set "{escaped}"\n'
        'end tell'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    except Exception:
        pass


def applescript_escape(s: str) -> str:
    """Escape a string for safe embedding in an AppleScript double-quoted string."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", " ")


def _cd_then(cwd: str | None, command: str) -> str:
    """The POSIX shell text for "in `cwd`, run `command`" — what Terminal.app
    is asked to `do script`."""
    if not cwd:
        return command
    cd = f"cd {shlex.quote(cwd)}"
    return f"{cd} && {command}" if command else cd


async def _open_console_windows(command: str, cwd: str | None) -> dict:
    """A new `cmd.exe` console in `cwd`, optionally running `command`.

    The directory is the child's working directory, not text in a command
    line, so no path ever needs quoting for cmd.exe. `command` has already
    been through `builds.command_problem` (no shell metacharacters) by the
    only caller that passes one. `title JARVIS` is this platform's version of
    the blue Terminal theme: it shows the user which window JARVIS opened.
    """
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    line = f'"{comspec}" /k title JARVIS'
    if command:
        # A project-local launcher (`.venv/Scripts/python.exe`) must be
        # spelled with backslashes, or cmd.exe reads its `/` as a switch.
        head, sep, rest = command.partition(" ")
        line += f" & {head.replace('/', chr(92))}{sep}{rest}"
    ok = await _spawn_detached(line, cwd=cwd, **platform_support.new_console_flags())
    return {
        "success": ok,
        "confirmation": "A console window is open, sir." if ok
        else "I had trouble opening a console window, sir.",
    }


async def open_terminal(command: str = "", cwd: str | None = None) -> dict:
    """Open a terminal window, optionally in `cwd` and running `command`.

    macOS: Terminal.app, marked blue for JARVIS. Windows: a new console
    window (a Windows Terminal tab where that is the default terminal).
    """
    if _is_windows():
        return await _open_console_windows(command, cwd)
    command = _cd_then(cwd, command)
    if command:
        escaped = applescript_escape(command)
        script = (
            'tell application "Terminal"\n'
            "    activate\n"
            f'    do script "{escaped}"\n'
            "end tell"
        )
    else:
        script = (
            'tell application "Terminal"\n'
            "    activate\n"
            "end tell"
        )
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    success = proc.returncode == 0
    if not success:
        log.error(f"open_terminal failed: {stderr.decode()}")
    else:
        await _mark_terminal_as_jarvis()
    return {
        "success": success,
        "confirmation": "Terminal is open, sir." if success else "I had trouble opening Terminal, sir.",
    }


async def open_browser(url: str, browser: str = "chrome") -> dict:
    """Open URL in user's browser (Chrome or Firefox).

    The URL goes through `applescript_escape` and nothing else. A hand-rolled
    `.replace('"', ...)` lived here and escaped the quote but not the
    BACKSLASH, which is the half that matters: AppleScript reads `\\\\` as one
    literal backslash, so a URL ending `x\\"` closes the string literal and
    everything after it is code — and `do shell script` is in that language.
    The URL arrives from a model, out of speech, possibly echoing a page or a
    README, so this is a straight line from attacker text to a shell.
    `tests/test_applescript_url_injection.py` runs the payload.

    On Windows there is no script to escape out of, but there is an argv to
    smuggle a flag into: a "URL" of `--renderer-cmd-prefix=...` handed to
    chrome.exe is a command. So only http(s) and file URLs are passed, and
    they cannot begin with a dash.
    """
    if _is_windows():
        return await _open_browser_windows(url, browser)
    escaped_url = applescript_escape(url)

    if browser.lower() == "firefox":
        app_name = "Firefox"
        script = (
            'tell application "Firefox"\n'
            "    activate\n"
            f'    open location "{escaped_url}"\n'
            "end tell"
        )
    else:
        app_name = "Chrome"
        script = (
            'tell application "Google Chrome"\n'
            "    activate\n"
            f'    open location "{escaped_url}"\n'
            "end tell"
        )

    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    success = proc.returncode == 0
    if not success:
        log.error(f"open_browser ({app_name}) failed: {stderr.decode()}")
    return {
        "success": success,
        "confirmation": f"Pulled that up in {app_name}, sir." if success else f"{app_name} ran into a problem, sir.",
    }


_WINDOWS_BROWSERS = {
    "chrome": ("Chrome", "chrome", (r"Google\Chrome\Application\chrome.exe",)),
    "firefox": ("Firefox", "firefox", (r"Mozilla Firefox\firefox.exe",)),
}


def _windows_browser(which: str) -> str | None:
    """Where the named browser's executable is, or None."""
    _label, exe, rel_paths = _WINDOWS_BROWSERS[which]
    found = shutil.which(exe)
    if found:
        return found
    roots = [os.environ.get(v) for v in
             ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    for root in filter(None, roots):
        for rel in rel_paths:
            candidate = Path(root) / rel
            if candidate.is_file():
                return str(candidate)
    return None


_SAFE_URL = re.compile(r"^(https?://|file:///)", re.IGNORECASE)


async def _open_browser_windows(url: str, browser: str) -> dict:
    which = "firefox" if browser.lower() == "firefox" else "chrome"
    app_name = _WINDOWS_BROWSERS[which][0]
    if not _SAFE_URL.match(url or ""):
        log.error(f"open_browser refused a non-web URL on Windows: {url[:80]!r}")
        return {"success": False,
                "confirmation": f"I won't hand that to {app_name}, sir."}
    exe = _windows_browser(which)
    if exe is None:
        log.error(f"open_browser: {app_name} is not installed")
        return {"success": False,
                "confirmation": f"I can't find {app_name} on this machine, sir."}
    success = await _spawn_detached([exe, url])
    return {
        "success": success,
        "confirmation": f"Pulled that up in {app_name}, sir." if success
        else f"{app_name} ran into a problem, sir.",
    }


# Keep backward compat
async def open_chrome(url: str) -> dict:
    return await open_browser(url, "chrome")


async def get_chrome_tab_info() -> dict:
    """Read the current Chrome tab's title and URL via AppleScript.

    macOS only: Windows exposes no tab URL without a browser extension, so
    there this is always {} — the same answer as "could not tell".
    """
    if _is_windows():
        return {}
    script = (
        'tell application "Google Chrome"\n'
        "    set tabTitle to title of active tab of front window\n"
        "    set tabURL to URL of active tab of front window\n"
        '    return tabTitle & "|" & tabURL\n'
        "end tell"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            result = stdout.decode().strip()
            parts = result.split("|", 1)
            if len(parts) == 2:
                return {"title": parts[0], "url": parts[1]}
        return {}
    except Exception as e:
        log.warning(f"get_chrome_tab_info failed: {e}")
        return {}


# --- Opening code where the user actually reads it -----------------------
#
# The user's words: "maybe he should be able to open code files in VS Code or
# text editor." VS Code first when it is installed, the system default
# otherwise, so this still works on a Mac that has never had it.
#
# No AppleScript and no shell: `open` is exec'd with the path as its own argv
# entry, so a filename cannot be quoted out of a script the way it can out of
# an AppleScript string literal. Containment and the sensitive-file wall are
# the CALLER's job and have already run by the time this is reached — see
# server.tool_open_in_editor.

VSCODE_APP = "/Applications/Visual Studio Code.app"


def _vscode_exe_windows() -> str | None:
    """VS Code's own Code.exe on Windows, or None.

    Never `code.cmd`: a batch file re-parses its arguments in cmd.exe, and a
    file name may legally contain `&`. The shim sits in `<install>\bin`, so
    Code.exe is one directory up from it; the two default install
    locations are tried after that.
    """
    shim = shutil.which("code")
    candidates = []
    if shim:
        candidates.append(Path(shim).resolve().parent.parent / "Code.exe")
    for var, rel in (("LOCALAPPDATA", r"Programs\Microsoft VS Code\Code.exe"),
                     ("PROGRAMFILES", r"Microsoft VS Code\Code.exe")):
        root = os.environ.get(var)
        if root:
            candidates.append(Path(root) / rel)
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _vscode_command(path: str) -> list[str] | None:
    """The argv that opens `path` in VS Code, or None if it is not installed."""
    if _is_windows():
        exe = _vscode_exe_windows()
        return [exe, str(path)] if exe else None
    binary = shutil.which("code")
    if binary:
        return [binary, str(path)]
    if os.path.isdir(VSCODE_APP):
        return ["open", "-a", VSCODE_APP, str(path)]
    return None


def _system_editor_command(path: str) -> list[str]:
    """The no-VS-Code fallback.

    macOS `open` hands a file to its default app. Windows' equivalent
    (`os.startfile`) RUNS a .bat, .ps1 or .exe, so it is never used: a
    directory goes to Explorer and a file to Notepad, which can open
    anything and execute nothing.
    """
    if not _is_windows():
        return ["open", str(path)]
    windir = os.environ.get("WINDIR") or r"C:\Windows"
    if os.path.isdir(path):
        return [str(Path(windir) / "explorer.exe"), str(path)]
    return [str(Path(windir) / "System32" / "notepad.exe"), str(path)]


async def open_in_editor(path: str) -> dict:
    """Open a file or directory in VS Code, else in the system default."""
    argv = _vscode_command(path)
    if _is_windows():
        editor = "VS Code" if argv else "your editor"
        ok = await _spawn_detached(argv or _system_editor_command(path))
        return {
            "success": ok,
            "editor": editor,
            "confirmation": f"Opened that in {editor}, sir." if ok
            else f"{editor} wouldn't open that, sir.",
        }
    editor = "VS Code"
    if argv is None:
        argv = _system_editor_command(path)
        editor = "your editor"

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        success = proc.returncode == 0
    except OSError as e:
        log.error(f"open_in_editor could not launch: {e}")
        return {"success": False, "editor": editor,
                "confirmation": "I couldn't open an editor, sir."}

    if not success:
        log.error(f"open_in_editor failed: {stderr.decode(errors='replace')}")
    return {
        "success": success,
        "editor": editor,
        "confirmation": f"Opened that in {editor}, sir." if success
        else f"{editor} wouldn't open that, sir.",
    }


def _generate_project_name(prompt: str) -> str:
    """Generate a kebab-case project folder name from the prompt."""
    # First: check for a quoted name like "tiktok-analytics-dashboard"
    quoted = re.search(r'"([^"]+)"', prompt)
    if quoted:
        name = quoted.group(1).strip()
        # Already kebab-case or close to it
        name = re.sub(r"[^a-zA-Z0-9\s-]", "", name).strip()
        if name:
            return re.sub(r"[\s]+", "-", name.lower())

    # Second: check for "called X" or "named X" pattern
    called = re.search(r'(?:called|named)\s+(\S+(?:[-_]\S+)*)', prompt, re.IGNORECASE)
    if called:
        name = re.sub(r"[^a-zA-Z0-9-]", "", called.group(1))
        if len(name) > 3:
            return name.lower()

    # Fallback: extract meaningful words
    words = re.sub(r"[^a-zA-Z0-9\s]", "", prompt.lower()).split()
    skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and",
            "to", "of", "i", "want", "need", "new", "project", "directory", "called",
            "on", "desktop", "that", "application", "app", "full", "stack", "simple",
            "web", "page", "site", "named"}
    meaningful = [w for w in words if w not in skip and len(w) > 2][:4]
    return "-".join(meaningful) if meaningful else "jarvis-project"
