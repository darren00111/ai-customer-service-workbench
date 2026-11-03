#!/usr/bin/env python3
"""Feige handoff alert: redact, then deliver to personal WeChat by default.

Delivery channels
-----------------
desktop (default)  Paste the redacted message into an already logged-in WeChat
                   desktop app. Default target chat is "文件传输助手".
webhook            POST to a WeCom group bot via WECOM_WEBHOOK_URL.

Safety model
------------
* Redaction always runs before any delivery attempt.
* A delivery gate applies: a real desktop send requires --confirm-send in
  addition to --send. Without that pair, the message is only prepared.
* --dry-run and --plain never touch the desktop app.
* The webhook secret is read from the environment only, never from a file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request


PHONE_RE = re.compile(r"(?<!\d)1\d{10}(?!\d)")
ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
LONG_NUMBER_RE = re.compile(r"(?<!\d)(\d{8,})(?!\d)")

DEFAULT_CHAT = "文件传输助手"
DEFAULT_PROVIDER = "personal_wechat"


def redact(value: str) -> str:
    value = PHONE_RE.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:], value)
    value = ID_RE.sub("[身份证号已隐藏]", value)
    value = LONG_NUMBER_RE.sub(lambda m: "***" + m.group(1)[-4:], value)
    return value.strip()


def redact_title(value: str) -> str:
    """Redact a chat title without mangling ordinary store names.

    Store names legitimately contain long digits (for example "旗舰店12号"),
    so only identity-grade patterns are masked here.
    """
    value = PHONE_RE.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:], value)
    value = ID_RE.sub("[身份证号已隐藏]", value)
    return value.strip()


def build_content(args: argparse.Namespace) -> str:
    lines = [
        "【飞鸽客服需要人工处理】",
        f"店铺：{redact(args.store)}",
        f"会话：{redact(args.customer)}",
        f"类型：{redact(args.category)}",
        f"摘要：{redact(args.summary)}",
    ]
    if args.product:
        lines.append(f"商品/SKU：{redact(args.product)}")
    if args.waiting:
        lines.append(f"已等待：{redact(args.waiting)}")
    if args.conversation_url:
        lines.append(f"会话入口：{args.conversation_url.strip()}")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--customer", required=True, help="Use a masked conversation identifier")
    parser.add_argument("--category", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--product", default="")
    parser.add_argument("--waiting", default="")
    parser.add_argument("--conversation-url", default="")

    parser.add_argument(
        "--channel",
        choices=("desktop", "webhook"),
        default="desktop",
        help="Delivery channel. Personal WeChat desktop is the default.",
    )
    parser.add_argument(
        "--chat",
        default=os.environ.get("WECHAT_CHAT_TITLE", DEFAULT_CHAT),
        help=f'Exact WeChat chat title. Defaults to "{DEFAULT_CHAT}".',
    )
    parser.add_argument(
        "--current-chat",
        action="store_true",
        help="Windows only: send to the chat already open in the main window; skip search.",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually deliver. Without it the message is only prepared.",
    )
    parser.add_argument(
        "--confirm-send",
        action="store_true",
        help="Second gate for desktop delivery; required together with --send.",
    )

    parser.add_argument("--webhook-env", default="WECOM_WEBHOOK_URL")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Print the redacted message for supervised personal-WeChat delivery",
    )
    return parser.parse_args()


def emit_osascript(script: str) -> bool:
    """Run an AppleScript snippet on macOS."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Desktop delivery failed: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(
            f"Desktop delivery failed: {result.stderr.strip() or 'osascript error'}",
            file=sys.stderr,
        )
        return False
    return True


def desktop_macos(title: str, content: str) -> bool:
    # Bring WeChat forward so the keystrokes land in the right window, then use
    # the search box to open the exact chat before pasting.
    escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
    script = textwrap.dedent(
        f'''
        tell application "WeChat" to activate
        delay 1
        tell application "System Events"
            keystroke "f" using command down
            delay 0.5
            keystroke "{escaped_title}"
            delay 0.8
            key code 36
            delay 0.8
            keystroke "v" using command down
            delay 0.4
            key code 36
        end tell
        '''
    )
    try:
        subprocess.run(
            ["pbcopy"],
            input=content,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Clipboard write failed: {exc}", file=sys.stderr)
        return False
    return emit_osascript(script)


WECHAT_WINDOW_CLASSES = (
    "Qt51514QWindowIcon",  # Weixin.exe 4.x
    "WeChatMainWndForPC",  # WeChat.exe 3.x
    "Qt51512QWindowIcon",
    "Qt5152QWindowIcon",
)

WECHAT_PROCESS_NAMES = ("Weixin.exe", "WeChat.exe")


def find_wechat_hwnd() -> int | None:
    """Return the HWND of the visible WeChat main window, or None.

    Matching by window class is deliberate: the visible Weixin 4.x window
    carries a non-descriptive title (for example "Rl") while its hidden
    siblings are titled "Weixin", so title-based activation is unreliable.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    pid_output = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout
    target_pids = set()
    for line in pid_output.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0] in WECHAT_PROCESS_NAMES:
            try:
                target_pids.add(int(parts[1]))
            except ValueError:
                continue
    if not target_pids:
        return None

    found: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in target_pids or not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value in WECHAT_WINDOW_CLASSES:
            found.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    return found[0] if found else None


def set_clipboard_unicode(text: str) -> None:
    """Place text on the clipboard as CF_UNICODETEXT via the Win32 API.

    Piping through clip.exe is encoding-ambiguous: with PYTHONUTF8=1 the
    Python side emits UTF-8 bytes while the system ANSI codepage stays GBK
    (936), so WeChat rendered the chat name as mojibake in its search box
    and no conversation ever matched. Writing CF_UNICODETEXT directly
    carries real Unicode and bypasses every codepage conversion.

    Callers must hold no clipboard open; this call replaces the whole
    clipboard content.
    """
    import ctypes
    import time as _time

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # 64-bit handles need explicit restype/argtypes or ctypes truncates them.
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    payload = text.encode("utf-16-le") + b"\x00\x00"

    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        _time.sleep(0.1)
    else:
        raise OSError("OpenClipboard failed: clipboard busy or denied")
    try:
        if not user32.EmptyClipboard():
            raise OSError("EmptyClipboard failed")
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not handle:
            raise OSError("GlobalAlloc failed")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            raise OSError("GlobalLock failed")
        ctypes.memmove(ptr, payload, len(payload))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise OSError("SetClipboardData failed")
    finally:
        user32.CloseClipboard()


def desktop_windows(title: str, content: str, current_chat: bool = False) -> bool:
    """Deliver through the Weixin.exe 4.x client on Windows.

    Weixin 4.x draws its entire UI itself and exposes no UI Automation
    controls, so the flow is driven with low-level keybd_event calls. The
    critical step is pressing the Down arrow after pasting the chat name:
    without it the search box keeps the text and Enter never opens the chat.
    Clipboard text goes through set_clipboard_unicode(), never through the
    clip.exe pipe (see that function's docstring for the mojibake story).
    """
    import ctypes
    import time

    user32 = ctypes.windll.user32

    VK_CONTROL = 0x11
    VK_A = 0x41
    VK_DELETE = 0x2E
    VK_F = 0x46
    VK_V = 0x56
    VK_DOWN = 0x28
    VK_RETURN = 0x0D
    KEYEVENTF_KEYUP = 0x0002

    def tap(vk, delay=0.6):
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.08)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(delay)

    def combo(modifier, vk, delay=0.8):
        user32.keybd_event(modifier, 0, 0, 0)
        time.sleep(0.08)
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.08)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.08)
        user32.keybd_event(modifier, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(delay)

    def set_clipboard(text: str) -> None:
        set_clipboard_unicode(text)

    hwnd = find_wechat_hwnd()
    if hwnd is None:
        print(
            "WeChat main window not found. Confirm the Windows WeChat client is "
            "running and showing a window, not only a tray icon.",
            file=sys.stderr,
        )
        return False

    try:
        # Focus the exact window, then locate the chat.
        user32.ShowWindow(hwnd, 9)
        time.sleep(0.5)
        user32.SetForegroundWindow(hwnd)
        time.sleep(1.2)
        if user32.GetForegroundWindow() != hwnd:
            print("Could not bring WeChat to the foreground.", file=sys.stderr)
            return False

        if not current_chat:
            # Open search, clear leftovers, paste the chat name.
            combo(VK_CONTROL, VK_F)
            time.sleep(1.2)
            combo(VK_CONTROL, VK_A)
            tap(VK_DELETE)
            set_clipboard(title)
            combo(VK_CONTROL, VK_V)
            time.sleep(2.0)

            # Select the first result, then open it. Skipping Down leaves the
            # text stranded in the search box.
            tap(VK_DOWN)
            time.sleep(1.0)
            tap(VK_RETURN, delay=1.8)

        # Paste the message and send.
        set_clipboard(content)
        combo(VK_CONTROL, VK_V)
        time.sleep(1.0)
        tap(VK_RETURN, delay=0.6)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Desktop delivery failed: {exc}", file=sys.stderr)
        return False

    return True


def deliver_desktop(args: argparse.Namespace, content: str) -> int:
    title = redact_title(args.chat)

    if not args.send:
        print("Delivery not sent. Re-run with --send --confirm-send to deliver.")
        print(f"Target chat: {title}")
        print("---- message ----")
        print(content)
        return 0

    if not args.confirm_send:
        print(
            "Refusing a real send: --send requires --confirm-send after the user "
            "has approved this exact message and target.",
            file=sys.stderr,
        )
        return 5

    if sys.platform == "darwin":
        ok = desktop_macos(title, content)
    elif sys.platform.startswith("win"):
        ok = desktop_windows(title, content, current_chat=args.current_chat)
    else:
        print(
            f"Desktop delivery is not implemented for platform {sys.platform!r}. "
            "Use --channel webhook or deliver manually.",
            file=sys.stderr,
        )
        return 6

    if ok:
        print(f"Notification sent to WeChat chat: {title}")
        return 0
    return 3


def deliver_webhook(args: argparse.Namespace, content: str) -> int:
    payload = {"msgtype": "text", "text": {"content": content}}

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    webhook = os.environ.get(args.webhook_env, "").strip()
    if not webhook:
        print(f"Missing environment variable: {args.webhook_env}", file=sys.stderr)
        return 2

    request = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Notification failed: {exc}", file=sys.stderr)
        return 3

    if result.get("errcode") != 0:
        print(f"WeCom rejected notification: {result.get('errmsg', 'unknown error')}", file=sys.stderr)
        return 4
    print("Notification sent")
    return 0


def main() -> int:
    args = parse_args()
    content = build_content(args)

    if args.plain:
        print(content)
        return 0

    if args.channel == "webhook":
        return deliver_webhook(args, content)
    return deliver_desktop(args, content)


if __name__ == "__main__":
    raise SystemExit(main())
