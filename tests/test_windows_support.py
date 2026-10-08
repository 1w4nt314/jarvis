"""The Windows paths, exercised on any platform.

Nothing here touches Win32: `sys.platform` (or `platform_support.IS_WINDOWS`)
is stood in for, and every process, DLL and helper boundary is faked. What
these pin is the part that can be wrong without a Windows machine to notice:
which argv is built, what never reaches a shell, and what is never called.
"""

import asyncio
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import actions
import dialog
import platform_support
import preflight
import screen
import session_steer
import windows_native


@pytest.fixture
def on_windows(monkeypatch):
    """Stand in for Windows: both the import-time flag and the runtime one."""
    monkeypatch.setattr(platform_support, "IS_WINDOWS", True)
    monkeypatch.setattr(sys, "platform", "win32")


# ── starting claude ───────────────────────────────────────────────────────

_NPM_SHIM = r'''@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@anthropic-ai\claude-code\cli.js" %*
'''


def _npm_prefix(tmp_path, target_rel, shim_text):
    shim = tmp_path / "claude.cmd"
    shim.write_text(shim_text)
    target = tmp_path.joinpath(*target_rel.split("\\"))
    target.parent.mkdir(parents=True)
    target.write_text("")
    return shim, target


def test_an_npm_shim_is_unwrapped_to_node_and_cli_js(tmp_path, monkeypatch):
    """cmd.exe would cut the brain's multi-line system prompt at its first
    newline; the shim must never be what is spawned."""
    shim, cli = _npm_prefix(
        tmp_path, r"node_modules\@anthropic-ai\claude-code\cli.js", _NPM_SHIM)
    monkeypatch.setattr(platform_support.shutil, "which",
                        lambda name: "/usr/bin/node" if name == "node" else None)
    assert platform_support.resolve_shim(str(shim)) == ["/usr/bin/node", str(cli)]


def test_a_node_exe_beside_the_shim_is_preferred(tmp_path, monkeypatch):
    shim, cli = _npm_prefix(
        tmp_path, r"node_modules\@anthropic-ai\claude-code\cli.js", _NPM_SHIM)
    (tmp_path / "node.exe").write_text("")
    monkeypatch.setattr(platform_support.shutil, "which", lambda name: None)
    assert platform_support.resolve_shim(str(shim)) == [
        str(tmp_path / "node.exe"), str(cli)]


def test_a_shim_for_a_native_binary_runs_the_binary(tmp_path):
    text = '@ECHO off\r\n"%~dp0\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\r\n'
    shim, exe = _npm_prefix(
        tmp_path, r"node_modules\@anthropic-ai\claude-code\bin\claude.exe", text)
    assert platform_support.resolve_shim(str(shim)) == [str(exe)]


def test_an_unrecognised_shim_is_none(tmp_path):
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off\r\necho hello\r\n")
    assert platform_support.resolve_shim(str(shim)) is None


def test_split_command_unwraps_a_shim_and_keeps_extra_args(tmp_path, monkeypatch,
                                                           on_windows):
    shim, cli = _npm_prefix(
        tmp_path, r"node_modules\@anthropic-ai\claude-code\cli.js", _NPM_SHIM)
    monkeypatch.setattr(platform_support.shutil, "which",
                        lambda name: "/usr/bin/node" if name == "node" else None)
    assert platform_support.split_command(str(shim)) == ["/usr/bin/node", str(cli)]


def test_split_command_keeps_backslashes_on_windows(on_windows):
    """shlex.split in POSIX mode turns C:\\Users\\x into C:Usersx."""
    argv = platform_support.split_command(r"C:\Tools\claude.exe --debug")
    assert argv == [r"C:\Tools\claude.exe", "--debug"]


def test_split_command_is_shlex_off_windows():
    assert platform_support.split_command("claude --x 'a b'") == ["claude", "--x", "a b"]


# ── pid_alive never kills ─────────────────────────────────────────────────

def test_pid_alive_on_windows_never_calls_os_kill(monkeypatch, on_windows):
    """On Windows os.kill(pid, 0) is TerminateProcess. The session watcher
    polls every Claude Code process once a second."""
    def _kill(*a):
        raise AssertionError("os.kill must never be called on Windows")
    monkeypatch.setattr(platform_support.os, "kill", _kill)
    monkeypatch.setattr(windows_native, "pid_alive", lambda pid: pid == 42)
    assert platform_support.pid_alive(42) is True
    assert platform_support.pid_alive(43) is False
    assert platform_support.pid_alive(0) is False
    assert platform_support.pid_alive(-1) is False
    assert platform_support.pid_alive("x") is False


def test_session_watch_goes_through_platform_support(monkeypatch):
    import session_watch
    seen = []
    monkeypatch.setattr(platform_support, "pid_alive",
                        lambda pid: seen.append(pid) or True)
    assert session_watch.pid_alive(7) is True
    assert seen == [7]


# ── PNG and pixels, the pure half of the Windows capture ─────────────────

def test_encode_png_round_trips():
    w, h = 3, 2
    rgb = bytes(range(w * h * 3))
    png = windows_native.encode_png(w, h, rgb)
    assert screen._png_size(png) == (w, h)
    # IDAT decompresses to filter byte 0 + each row.
    i = png.index(b"IDAT")
    (length,) = struct.unpack(">I", png[i - 4:i])
    raw = zlib.decompress(png[i + 4:i + 4 + length])
    assert raw == b"\x00" + rgb[:9] + b"\x00" + rgb[9:]


def test_bgra_becomes_rgb():
    assert windows_native.bgra_to_rgb(bytes([1, 2, 3, 255, 4, 5, 6, 255])) == bytes(
        [3, 2, 1, 6, 5, 4])


def test_a_flat_frame_is_blank_and_a_busy_one_is_not():
    flat = bytes([10, 20, 30]) * (64 * 64)
    assert screen._is_blank(windows_native.sample_pixels(64, 64, flat))
    busy = bytes((i * 37) % 256 for i in range(64 * 64 * 3))
    assert not screen._is_blank(windows_native.sample_pixels(64, 64, busy))


@pytest.mark.asyncio
async def test_windows_capture_refuses_a_blank_screen(monkeypatch, on_windows):
    monkeypatch.setattr(windows_native, "capture_display",
                        lambda display, edge: (8, 8, bytes(8 * 8 * 3)))
    with pytest.raises(screen.ScreenError, match="blank"):
        await screen.capture_screen()


@pytest.mark.asyncio
async def test_windows_capture_returns_a_png(monkeypatch, on_windows):
    busy = bytes((i * 37) % 256 for i in range(40 * 30 * 3))
    monkeypatch.setattr(windows_native, "capture_display",
                        lambda display, edge: (40, 30, busy))
    shot = await screen.capture_screen()
    assert (shot.width, shot.height) == (40, 30)
    assert screen._png_size(shot.png) == (40, 30)


@pytest.mark.asyncio
async def test_windows_capture_names_a_missing_display(monkeypatch, on_windows):
    def _missing(display, edge):
        raise LookupError(display)
    monkeypatch.setattr(windows_native, "capture_display", _missing)
    with pytest.raises(screen.ScreenError, match="display number 3"):
        await screen.capture_screen(display=3)


@pytest.mark.asyncio
async def test_windows_window_list(monkeypatch, on_windows):
    monkeypatch.setattr(windows_native, "list_windows", lambda: [
        ("Code", "server.py - jarvis", True), ("chrome", "JARVIS", False)])
    wins = await screen.list_windows()
    assert [(w.app, w.title, w.frontmost) for w in wins] == [
        ("Code", "server.py - jarvis", True), ("chrome", "JARVIS", False)]


# ── actions ───────────────────────────────────────────────────────────────

class _Popen:
    calls: list = []

    def __init__(self, argv, **kwargs):
        _Popen.calls.append((argv, kwargs))


@pytest.fixture
def popen(monkeypatch):
    _Popen.calls = []
    monkeypatch.setattr(actions.subprocess, "Popen", _Popen)
    return _Popen.calls


@pytest.mark.asyncio
async def test_windows_terminal_runs_in_cwd_and_never_quotes_the_path(
        on_windows, popen):
    """The directory is the child's cwd, not text cmd.exe could misread."""
    weird = r"C:\Users\me\100% & co"
    result = await actions.open_terminal("npm run dev", cwd=weird)
    assert result["success"] is True
    (line, kwargs), = popen
    assert kwargs["cwd"] == weird
    assert weird not in line
    assert line.endswith("/k title JARVIS & npm run dev")


@pytest.mark.asyncio
async def test_windows_terminal_spells_a_local_launcher_with_backslashes(
        on_windows, popen):
    await actions.open_terminal(".venv/Scripts/python.exe app.py --port=8/0",
                                cwd=r"C:\p")
    (line, _kwargs), = popen
    assert line.endswith(r"& .venv\Scripts\python.exe app.py --port=8/0")


@pytest.mark.asyncio
async def test_windows_terminal_without_a_command(on_windows, popen):
    await actions.open_terminal(cwd=r"C:\p")
    (line, _kwargs), = popen
    assert line.endswith("/k title JARVIS")


def test_the_mac_terminal_text_is_unchanged():
    assert actions._cd_then("/p/my app", "npm run dev") == "cd '/p/my app' && npm run dev"
    assert actions._cd_then("/p/x", "") == "cd /p/x"


@pytest.mark.asyncio
async def test_windows_browser_refuses_anything_that_could_be_a_flag(
        on_windows, popen, monkeypatch):
    """chrome.exe --renderer-cmd-prefix=... is a command, not a page."""
    monkeypatch.setattr(actions, "_windows_browser", lambda which: r"C:\chrome.exe")
    for url in ("--renderer-cmd-prefix=calc", "javascript:alert(1)",
                "ms-settings:", "\\\\host\\share"):
        result = await actions.open_browser(url)
        assert result["success"] is False, url
    assert popen == []


@pytest.mark.asyncio
async def test_windows_browser_opens_a_web_page_by_argv(on_windows, popen,
                                                        monkeypatch):
    monkeypatch.setattr(actions, "_windows_browser", lambda which: r"C:\chrome.exe")
    result = await actions.open_browser("https://example.com/?a=1&b=2")
    assert result["success"] is True
    assert popen[0][0] == [r"C:\chrome.exe", "https://example.com/?a=1&b=2"]


@pytest.mark.asyncio
async def test_windows_browser_missing_says_so(on_windows, popen, monkeypatch):
    monkeypatch.setattr(actions, "_windows_browser", lambda which: None)
    result = await actions.open_browser("https://example.com", "firefox")
    assert result["success"] is False
    assert "Firefox" in result["confirmation"]
    assert popen == []


@pytest.mark.asyncio
async def test_windows_editor_fallback_never_runs_the_file(on_windows, popen,
                                                           monkeypatch, tmp_path):
    """os.startfile on a .bat runs it. The fallback is Notepad / Explorer."""
    monkeypatch.setattr(actions, "_vscode_exe_windows", lambda: None)
    monkeypatch.setenv("WINDIR", r"C:\Windows")
    script = tmp_path / "evil.bat"
    script.write_text("calc")
    await actions.open_in_editor(str(script))
    await actions.open_in_editor(str(tmp_path))
    assert Path(popen[0][0][0]).name == "notepad.exe"
    assert popen[0][0][1] == str(script)
    assert Path(popen[1][0][0]).name == "explorer.exe"


@pytest.mark.asyncio
async def test_windows_editor_prefers_code_exe(on_windows, popen, monkeypatch):
    monkeypatch.setattr(actions, "_vscode_exe_windows", lambda: r"C:\VSCode\Code.exe")
    result = await actions.open_in_editor(r"C:\p\a & b.py")
    assert result["editor"] == "VS Code"
    assert popen[0][0] == [r"C:\VSCode\Code.exe", r"C:\p\a & b.py"]


# ── answer_dialog ─────────────────────────────────────────────────────────

def test_a_console_id_is_a_tty_and_nothing_else_is():
    assert dialog.normalize_tty("console:132456") == "console:132456"
    assert dialog.normalize_tty("console:12; rm -rf") is None
    assert dialog.normalize_tty("console:") is None


@pytest.mark.asyncio
async def test_windows_answer_presses_through_the_helper(monkeypatch, on_windows):
    calls = []

    def _helper(*args):
        calls.append(args)
        if args[0] == "console":
            return 0, "777"
        return 0, "ok"

    monkeypatch.setattr(dialog, "_win_helper", _helper)
    assert await dialog.answer(4242, "yes") == dialog.SENT
    assert calls == [("console", "4242"), ("press", "4242", "return", "777")]


@pytest.mark.asyncio
@pytest.mark.parametrize("said, outcome", [
    ("moved", dialog.NOT_FOUND), ("no_console", dialog.NOT_FOUND),
    ("failed", dialog.FAILED), ("bad_key", dialog.BAD_KEY)])
async def test_windows_answer_outcomes(monkeypatch, on_windows, said, outcome):
    monkeypatch.setattr(dialog, "_win_helper", lambda *a: (
        (0, "9") if a[0] == "console" else (0, said)))
    assert await dialog.answer(1, "escape") == outcome


@pytest.mark.asyncio
async def test_windows_answer_without_a_console_presses_nothing(monkeypatch,
                                                                on_windows):
    calls = []
    monkeypatch.setattr(dialog, "_win_helper",
                        lambda *a: calls.append(a) or (0, ""))
    assert await dialog.answer(1, "1") == dialog.NO_TTY
    assert [c[0] for c in calls] == ["console"]


@pytest.mark.asyncio
async def test_windows_answer_refuses_free_text_before_any_helper(monkeypatch,
                                                                  on_windows):
    monkeypatch.setattr(dialog, "_win_helper", lambda *a: pytest.fail("ran"))
    assert await dialog.answer(1, "rm -rf") == dialog.BAD_KEY


def test_the_helper_refuses_keys_outside_the_vocabulary():
    assert windows_native.press_key(1, "a", 0) == "bad_key"
    assert windows_native.press_key(1, "return\r", 0) == "bad_key"


# ── steering over a named pipe ────────────────────────────────────────────

def test_named_pipe_paths_are_recognised():
    assert platform_support.is_named_pipe(r"\\.\pipe\claude-abc")
    assert platform_support.is_named_pipe(r"\\?\PIPE\claude-abc")
    assert not platform_support.is_named_pipe("/tmp/claude.sock")
    assert not platform_support.is_named_pipe(None)


def test_a_pipe_is_never_live_off_windows():
    assert platform_support.ipc_endpoint_exists(r"\\.\pipe\x") is False


def test_a_pipe_is_found_by_listing_never_by_opening(monkeypatch, on_windows):
    """Opening a named pipe connects to it; a liveness poll must not."""
    monkeypatch.setattr(platform_support.os, "listdir",
                        lambda p: ["other", "Claude-ABC"])
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("opened"))
    assert platform_support.ipc_endpoint_exists(r"\\.\pipe\claude-abc") is True
    assert platform_support.ipc_endpoint_exists(r"\\.\pipe\gone") is False


def test_steering_writes_one_payload_to_the_pipe(monkeypatch, tmp_path):
    sink = tmp_path / "pipe"
    monkeypatch.setattr(platform_support, "ipc_endpoint_exists", lambda p: True)
    monkeypatch.setattr(platform_support, "is_named_pipe", lambda p: True)
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_TOKEN", raising=False)
    assert session_steer.post_to_session(str(sink), "carry on") == session_steer.SENT
    assert b'"content": "carry on"' in sink.read_bytes()


# ── preflight ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_preflight_needs_no_mac_permissions_on_windows(on_windows):
    acc = await preflight._check_accessibility()
    rec = preflight._check_screen_recording_sync()
    assert (acc.status, rec.status) == (preflight.STATUS_OK, preflight.STATUS_OK)


@pytest.mark.asyncio
async def test_preflight_reads_the_refresh_expiry_from_the_credentials_file(
        tmp_path, on_windows):
    (tmp_path / ".credentials.json").write_text(
        '{"claudeAiOauth": {"refreshTokenExpiresAt": 1700000000000}}')
    assert await preflight._read_oauth_refresh_expiry(tmp_path, 1.0) == 1700000000.0
    assert await preflight._read_oauth_refresh_expiry(tmp_path / "none", 1.0) is None


# ── cancelling a run ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancelling_a_run_on_windows_ends_the_whole_tree(monkeypatch):
    """TerminateProcess gives Claude Code no chance to stop what it started;
    taskkill /T does it for it."""
    import run_executor
    monkeypatch.setattr(platform_support, "IS_WINDOWS", True)
    killed = []
    monkeypatch.setattr(platform_support, "kill_tree",
                        lambda pid: killed.append(pid) or True)

    class _Proc:
        pid, returncode = 99, None

        def terminate(self):
            pytest.fail("terminate() would orphan the run's children")

        def kill(self):
            self.returncode = -9

        async def wait(self):
            self.returncode = 1
            return 1

    ex = run_executor.RunExecutor.__new__(run_executor.RunExecutor)
    ex._grace_sec = 0.5
    await ex._terminate(_Proc())
    assert killed == [99]
