# -*- coding: utf-8 -*-
"""验证工作日志贴片窗口的物理位置是否在屏幕内。"""
import ctypes
import ctypes.wintypes as wt

u = ctypes.windll.user32
u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # 拿物理坐标
res = []


@ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
def cb(h, _l):
    n = ctypes.create_unicode_buffer(64)
    u.GetWindowTextW(h, n, 64)
    if n.value == "工作日志":
        r = wt.RECT()
        u.GetWindowRect(h, ctypes.byref(r))
        mon = u.MonitorFromWindow(h, 0)  # 0 = 完全在屏幕外
        vis = u.IsWindowVisible(h)
        pref = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(h, 33, ctypes.byref(pref), 4)
        corner = {0: "默认(直角)", 1: "不要圆角", 2: "圆角", 3: "小圆角"}.get(pref.value, pref.value)
        res.append((r.left, r.top, r.right, r.bottom,
                    "on_screen" if mon else "OFF-SCREEN",
                    "visible" if vis else "hidden", "corner=" + str(corner)))
    return True


u.EnumWindows(cb, 0)
print("窗口状态:", res if res else "未找到窗口")
