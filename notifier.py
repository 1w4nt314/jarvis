"""
JARVIS Notifier -- desktop notification fallback (macOS and Windows).

When JARVIS needs the user's attention but no browser tab is connected to
speak through, this posts a native notification instead: Notification
Center through `osascript` on macOS, a toast through Windows PowerShell on
Windows. See
actions.py for the project's usual pattern of driving `osascript`; this
module differs from it deliberately (see below) because the text here is
attacker-influenced.

Notification titles/messages surface a Claude Code session's title or its
last message -- text that originates in someone else's transcript, not
text JARVIS wrote itself. actions.py's `applescript_escape()` handles this
by escaping quotes/backslashes before interpolating into the script
*source*. That is fine for its callers, but here we take a stronger
approach: the script never contains the untrusted text at all. It is
written once as a fixed `on run argv` handler and read from stdin, and the
title/message/subtitle are passed as trailing argv strings, which
osascript hands to the script as plain data -- never parsed as AppleScript
source. There is no escaping step to get wrong because there is nothing to
escape: a value containing `" & do shell script "..." & "` is just a
string with those characters in it.

Windows follows the same rule by a different road. PowerShell has no
`on run argv`, so its script is fixed too and the three strings travel as
ENVIRONMENT VARIABLES, which the script reads as values and puts into the
toast with `CreateTextNode` -- never into script source, never into XML
markup. The script itself goes over `-EncodedCommand`, so not even the
fixed part is re-parsed by a shell.
"""

import asyncio
import base64
import logging
import os
import shutil
import sys

log = logging.getLogger("jarvis.notifier")

# A notification is a glance, not an essay -- keep it short. These bound
# what we pass to Notification Center regardless of how long the source
# text (a session title, a transcript line) actually is.
_TITLE_MAX = 120
_SUBTITLE_MAX = 120
_MESSAGE_MAX = 300

# osascript is usually near-instant; a wedged one must not hang the caller.
_TIMEOUT_SECONDS = 5.0
# Windows PowerShell is not near-instant: a cold start plus loading the
# WinRT toast types is measured in seconds on an ordinary laptop.
_WINDOWS_TIMEOUT_SECONDS = 15.0

# Fixed script, no untrusted text ever enters this string. Title, message,
# and subtitle arrive purely via argv (see module docstring).
_NOTIFY_SCRIPT = """\
on run argv
    set theTitle to item 1 of argv
    set theMessage to item 2 of argv
    set theSubtitle to item 3 of argv
    if theSubtitle is "" then
        display notification theMessage with title theTitle
    else
        display notification theMessage with title theTitle subtitle theSubtitle
    end if
end run
"""


# Windows: fixed, like the AppleScript above. The values arrive only as
# $env:JARVIS_NOTIFY_* (see module docstring). The AUMID is Windows
# PowerShell's own, which every Windows 10/11 install has registered, so the
# toast needs no app registration of ours.
_WINDOWS_NOTIFY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$values = @([string]$env:JARVIS_NOTIFY_TITLE)
if ([string]$env:JARVIS_NOTIFY_SUBTITLE -ne '') { $values += [string]$env:JARVIS_NOTIFY_SUBTITLE }
$values += [string]$env:JARVIS_NOTIFY_MESSAGE
if ($values.Count -eq 3) { $kind = [Windows.UI.Notifications.ToastTemplateType]::ToastText04 }
else { $kind = [Windows.UI.Notifications.ToastTemplateType]::ToastText02 }
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($kind)
$texts = $xml.GetElementsByTagName('text')
for ($i = 0; $i -lt $values.Count; $i++) {
    [void]$texts.Item($i).AppendChild($xml.CreateTextNode($values[$i]))
}
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
"""


def _powershell() -> str | None:
    """Windows PowerShell 5.1 (the one with WinRT type loading), or None."""
    return shutil.which("powershell") or shutil.which("powershell.exe")


def _windows_command(title: str, message: str, subtitle: str
                     ) -> tuple[list[str], bytes | None, dict]:
    encoded = base64.b64encode(_WINDOWS_NOTIFY_SCRIPT.encode("utf-16-le")).decode()
    env = dict(os.environ)
    env.update(JARVIS_NOTIFY_TITLE=title, JARVIS_NOTIFY_MESSAGE=message,
               JARVIS_NOTIFY_SUBTITLE=subtitle)
    argv = [_powershell() or "powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-EncodedCommand", encoded]
    return argv, None, env


def _truncate(text: str, limit: int) -> str:
    """Bound text length for a glanceable notification, marking any cut."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"  # ellipsis


def available() -> bool:
    """Whether posting a notification is plausible right now.

    Cheap and side-effect free: macOS + `osascript` on PATH, or Windows +
    Windows PowerShell on PATH. This is a precondition check, not a delivery
    guarantee -- Notification Center settings, Focus / Do Not Disturb, or
    per-app permissions can still silently drop the notification even when
    this returns True.
    """
    if sys.platform == "win32":
        return _powershell() is not None
    return sys.platform == "darwin" and shutil.which("osascript") is not None


async def notify(title: str, message: str, *, subtitle: str = "") -> bool:
    """Post a macOS notification. Returns whether it was handed off successfully.

    This is a fallback path for when nobody is listening on the voice
    channel, so it must never raise: any failure (no macOS, no osascript,
    non-zero exit, timeout, spawn error) is logged as a warning and
    reported back as False. The `osascript` call runs as a subprocess so it
    never blocks the event loop, and is bounded by _TIMEOUT_SECONDS so a
    wedged process cannot hang the caller.
    """
    try:
        if not available():
            log.warning("notifier: notifications unavailable on this platform")
            return False

        safe_title = _truncate(str(title or ""), _TITLE_MAX)
        safe_message = _truncate(str(message or ""), _MESSAGE_MAX)
        safe_subtitle = _truncate(str(subtitle or ""), _SUBTITLE_MAX)

        if sys.platform == "win32":
            argv, stdin, env = _windows_command(safe_title, safe_message,
                                                safe_subtitle)
            extra = {"env": env, "creationflags": 0x08000000}  # CREATE_NO_WINDOW
        else:
            argv = ["osascript", "-", safe_title, safe_message, safe_subtitle]
            stdin, extra = _NOTIFY_SCRIPT.encode("utf-8"), {}
        tool = "powershell" if sys.platform == "win32" else "osascript"

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **extra,
            )
        except OSError as e:
            log.warning(f"notifier: failed to spawn {tool}: {e}")
            return False

        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(stdin),
                timeout=(_WINDOWS_TIMEOUT_SECONDS if sys.platform == "win32"
                         else _TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            log.warning(f"notifier: {tool} timed out, killing it")
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            return False

        if proc.returncode != 0:
            log.warning(
                f"notifier: {tool} exited {proc.returncode}: "
                f"{stderr.decode(errors='replace').strip()}"
            )
            return False

        return True
    except Exception as e:
        # Belt and suspenders: this path must never raise into the caller.
        log.warning(f"notifier: unexpected error posting notification: {e}")
        return False
