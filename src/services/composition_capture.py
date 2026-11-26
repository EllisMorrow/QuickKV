
"""User-initiated Windows selection capture with guarded clipboard fallback."""

import base64
import ctypes
from ctypes import wintypes
import subprocess
import time

from PySide6.QtCore import QObject, QTimer, Signal


class CompositionCapture(QObject):
    received = Signal(str)
    failed = Signal(str)

    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.request = self.process = None
        self.success_deadline = 0
        self.timer = QTimer(self, interval=25)
        self.timer.timeout.connect(self.poll)

    def begin(self, hwnd):
        ctl = self.controller
        pid = ctl._get_window_process_id(hwnd)
        if self.request:
            return
        if not hwnd or not pid or pid == ctl.quickkv_pid:
            self.failed.emit("请先在目标窗口选中文字，再使用回载快捷键或剪贴板入口。")
            return
        sequence = ctl.user32.GetClipboardSequenceNumber()
        if not sequence:
            self.failed.emit("无法安全读取剪贴板状态。")
            return
        self.request = (hwnd, pid, sequence, time.monotonic())
        foreground = ctl._get_foreground_hwnd()
        if (foreground != hwnd and ctl._get_window_process_id(foreground) == ctl.quickkv_pid
                and ctl._get_window_process_id(hwnd) == pid and ctl._get_foreground_hwnd() == foreground):
            # Restore while QuickKV still owns foreground permission.
            ctl.user32.SetForegroundWindow(wintypes.HWND(hwnd))
        ctl.popup.hide()
        self.timer.start()

    def stop(self):
        self.request = None
        self.timer.stop()
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process = None

    def poll(self):
        if self.request is None:
            return
        ctl = self.controller
        hwnd, pid, sequence, started = self.request
        foreground = ctl._get_foreground_hwnd()
        clipboard_sequence = ctl.user32.GetClipboardSequenceNumber()
        now = time.monotonic()
        if self.process:
            exit_code = self.process.poll()
            valid = foreground == hwnd and ctl._get_window_process_id(hwnd) == pid
            if exit_code == 0 and valid and clipboard_sequence and clipboard_sequence != sequence:
                clipboard_owner = ctl.user32.GetClipboardOwner()
                if clipboard_owner and ctl._get_window_process_id(clipboard_owner) == pid:
                    text = ctl.app.clipboard().text()
                    if ctl.user32.GetClipboardSequenceNumber() == clipboard_sequence and text:
                        self.stop()
                        self.received.emit(text)
                        return
            if valid and exit_code in (None, 0) and now < self.success_deadline:
                return
        elif now - started < 0.9 and ctl._get_window_process_id(hwnd) == pid and clipboard_sequence == sequence:
            if foreground != hwnd or ctl._get_pressed_modifiers():
                return
            selected = native_edit_selection(ctl, hwnd, pid)
            if selected and ctl._get_foreground_hwnd() == hwnd:
                self.stop()
                self.received.emit(selected)
                return
            if self.start_process(hwnd, pid, sequence):
                self.success_deadline = now + 2
                return
        self.stop()
        self.failed.emit("未能确认目标选区。可以在原应用手动复制，再打开剪贴板回载。")

    def start_process(self, hwnd, pid, sequence):
        script = f"""$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Name Native -Namespace QuickKV -MemberDefinition @'
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
[DllImport("user32.dll")] public static extern uint GetClipboardSequenceNumber();
[DllImport("user32.dll")] public static extern short GetAsyncKeyState(int key);
{{close}}
foreach ($key in 0x11, 0x10, 0x12, 0x5B, 0x5C) {{
    if ([QuickKV.Native]::GetAsyncKeyState($key) -band 0x8000) {{ exit 2 }}
}}
$target = [QuickKV.Native]::GetForegroundWindow()
[uint32]$process = 0
[void][QuickKV.Native]::GetWindowThreadProcessId($target, [ref]$process)
if ($target.ToInt64() -ne {hwnd} -or $process -ne {pid} -or [QuickKV.Native]::GetClipboardSequenceNumber() -ne {sequence}) {{ exit 2 }}
[System.Windows.Forms.SendKeys]::SendWait('^c')
""".replace("{close}", "'@")
        command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            self.process = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            return False

class GuiThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("active", wintypes.HWND), ("focus", wintypes.HWND), ("capture", wintypes.HWND),
        ("menu_owner", wintypes.HWND), ("move_size", wintypes.HWND), ("caret", wintypes.HWND),
        ("caret_rect", wintypes.RECT),
    ]


def native_edit_selection(controller, hwnd, pid):
    """Use system-marshalled edit messages only for a verified, non-password Edit control."""
    user = controller.user32
    if not isinstance(user, ctypes.WinDLL):
        return None
    parent = wintypes.HWND(hwnd)
    info = GuiThreadInfo()
    info.cbSize = ctypes.sizeof(info)
    thread = user.GetWindowThreadProcessId(parent, None)
    if not thread or not user.GetGUIThreadInfo(thread, ctypes.byref(info)) or info.active != hwnd:
        return None
    if not info.focus or controller._get_window_process_id(info.focus) != pid:
        return None
    editor = wintypes.HWND(info.focus)
    name = ctypes.create_unicode_buffer(32)
    if not user.GetClassNameW(editor, name, len(name)) or name.value.lower() != "edit":
        return None
    if user.GetWindowLongW(editor, -16) & 0x20:  # ES_PASSWORD
        return None

    def send(message, first=0, second=0):
        result = ctypes.c_size_t()
        if user.SendMessageTimeoutW(editor, message, first, second, 0x0002, 100, ctypes.byref(result)):
            return result.value
        return None

    start, end = wintypes.DWORD(), wintypes.DWORD()
    if send(0x00B0, ctypes.byref(start), ctypes.byref(end)) is None or start.value >= end.value:
        return None
    length = send(0x000E)
    if length is None or not end.value <= length <= 1_000_000:
        return None
    text = ctypes.create_unicode_buffer(length + 1)
    if send(0x000D, length + 1, text) != length:
        return None
    final_start, final_end = wintypes.DWORD(), wintypes.DWORD()
    if (send(0x00B0, ctypes.byref(final_start), ctypes.byref(final_end)) is None
            or (start.value, end.value) != (final_start.value, final_end.value)):
        return None
    data = ctypes.string_at(ctypes.addressof(text) + start.value * 2, (end.value - start.value) * 2)
    return data.decode("utf-16-le", errors="replace")
