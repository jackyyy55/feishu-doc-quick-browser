# -*- coding: utf-8 -*-
"""飞书工作日志桌面贴片。

无边框 / 置顶 / 可拖动 / 半透明圆角卡片。内容来自飞书云文档，
页面用本地 HTTP 服务承载（比 JS 注入稳定），改动通过 lark-cli 写回文档。
"""
import ctypes
import json
import os
import sys
import threading
import time
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


def _enable_dpi_awareness():
    """默认走 PMv2（逐显示器感知），这是 WebView2 内容能正确缩放的前提。"""
    if os.environ.get("WORKLOG_SYSTEM_DPI") == "1":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


_enable_dpi_awareness()

import webview  # noqa: E402

import bridge  # noqa: E402

# 用于校准 WebView2 的 DIP 尺寸（反射拿控制器需要 System 类型）
try:
    import clr  # noqa: F401  pythonnet 要先加载 clr 才能 import System*
    import System.Reflection as Reflection  # noqa: E402
    from System import Func  # noqa: E402

    clr.AddReference("System.Drawing")
    from System.Drawing import Rectangle  # noqa: E402
except Exception:  # pragma: no cover
    Reflection = None
    Func = None
    Rectangle = None

# PyInstaller 打包后：资源（web/）在 _MEIPASS 临时解包目录；
# 可写文件（config/data）统一放 %APPDATA%\FeishuDocQuickBrowser，exe 目录保持干净，
# exe 挪到任何位置配置都跟着走。开发模式（python app.py）仍用脚本所在目录。
FROZEN = getattr(sys, "frozen", False)
RES_DIR = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
if FROZEN:
    APP_DIR = os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"), "FeishuDocQuickBrowser"
    )
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(RES_DIR, "web")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
DATA_DIR = os.path.join(APP_DIR, "data")


def _migrate_legacy_writable():
    """旧版本把 config.json / data 放在 exe 旁边：首次运行时搬到 APPDATA（只搬不删）。"""
    if not FROZEN:
        return
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if os.path.normcase(exe_dir) == os.path.normcase(APP_DIR):
        return
    import shutil

    legacy_cfg = os.path.join(exe_dir, "config.json")
    if os.path.isfile(legacy_cfg) and not os.path.exists(CONFIG_PATH):
        try:
            shutil.copy2(legacy_cfg, CONFIG_PATH)
            _life_safe(f"已迁移配置 {legacy_cfg} -> {CONFIG_PATH}")
        except OSError as e:
            _life_safe(f"迁移配置失败（忽略，用默认配置）: {e}")
    legacy_data = os.path.join(exe_dir, "data")
    if os.path.isdir(legacy_data) and not os.path.exists(DATA_DIR):
        try:
            shutil.copytree(legacy_data, DATA_DIR)
            _life_safe(f"已迁移数据 {legacy_data} -> {DATA_DIR}")
        except OSError as e:
            _life_safe(f"迁移数据失败（忽略，新建空数据目录）: {e}")


def _life_safe(msg):
    """迁移阶段 DATA_DIR 可能还没建，先保证目录再写日志。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "life.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


if FROZEN:
    _migrate_legacy_writable()

# 窗口标题（顶栏 / 任务栏 / 找窗口都认它；旧标题留作兼容，改名后旧进程也能被找到）
WINDOW_TITLE = "飞书文档快速浏览"
WINDOW_TITLES = (WINDOW_TITLE, "工作日志")

DEFAULT_CONFIG = {
    "doc_url": "",
    "on_top": True,
    # 几何全部按物理像素存（换屏/换 DPI 都不会漂）
    "width": 700,
    "height": 1080,
    "x": None,
    "y": None,
    "collapsed": False,
    "geom_unit": "px",
    # 半透明：鼠标移入/移出时的不透明度（%），通过页面 CSS 变量生效，
    # 这样贴边缩进后露出的拉手 #dockbar 不受透明度影响
    "alpha_in": 100,
    "alpha_out": 70,
    # 贴边隐藏："" = 不贴边；left / right / top
    "dock_side": "",
    "dock_px": 20,  # 贴边隐藏后留出的可见条宽度（物理像素，要能装下拉手图标）
    # 动画时长（毫秒）：两段动画各自独立 —— 位移（收起/展开）与透明度（明暗渐变）
    "anim_dock_ms": 160,   # 收起/展开的滑入滑出时长，0 = 直接到位
    "anim_alpha_ms": 300,  # 透明度渐变时长，0 = 立刻切换
}


# ---------------------------------------------------------------- DPI
def dpi_scale():
    try:
        return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except Exception:
        return 1.0


def window_scale():
    """窗口所在显示器的缩放比（按窗口 DPI，而不是系统 DPI）。

    多屏混合 DPI 时两者常常不同：用系统 DPI 换算出"露出的 CSS 像素宽"
    会偏小，导致贴边拉手盖不满那条缝、文档内容从边上漏出来。
    """
    try:
        hwnd = _find_hwnd()
        if hwnd:
            return max(1.0, ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0)
    except Exception:
        pass
    return SCALE or 1.0


SCALE = dpi_scale()

# 收起时只剩顶部状态条（物理像素高度，按 DPI 换算）
COLLAPSED_H = max(44, int(round(44 * SCALE)))


class _PT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RC(ctypes.Structure):
    _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                ("r", ctypes.c_long), ("b", ctypes.c_long)]




def px(logical):
    return int(round(logical * SCALE))


# ---------------------------------------------------------------- 配置
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if cfg.get("geom_unit") != "px":  # 老配置存的是逻辑坐标，换算成物理像素
        s = SCALE or 1.0
        for k in ("x", "y", "width", "height"):
            if isinstance(cfg.get(k), (int, float)):
                cfg[k] = int(round(cfg[k] * s))
        cfg["geom_unit"] = "px"
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# ---------------------------------------------------------------- 开机自启
# 写 HKCU\...\Run：登录后由 pythonw.exe 静默拉起（不带黑窗）。
# 用注册表而不是启动文件夹快捷方式：不依赖 pywin32，也方便程序自己开关。
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "FeishuLogWidget"


def _autostart_cmd():
    if FROZEN:
        # 打包后自启就是 exe 本身（--noconsole，无黑窗）
        return '"%s"' % sys.executable
    exe = sys.executable or "python.exe"
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return '"%s" "%s"' % (exe, os.path.join(APP_DIR, "app.py"))


def autostart_get():
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            winreg.QueryValueEx(k, AUTOSTART_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def autostart_set(flag):
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if flag:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_cmd())
            else:
                try:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return autostart_get() == bool(flag)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 逻辑层
class Core:
    def __init__(self):
        self.cfg = load_config()
        self.model = bridge.load_cache()
        self.win = None
        self._busy = False
        self._error = ""

    def state(self, **extra):
        model = self.model or {}
        st = {
            "ok": True,
            "doc_url": self.cfg.get("doc_url", ""),
            "doc_title": model.get("doc_title") or ("飞书文档" if self.cfg.get("doc_url") else ""),
            # 已知标题缓存（域名+路径 → 标题）与最近打开过的文档
            "doc_titles": self.cfg.get("doc_titles", {}),
            "doc_recent": self.cfg.get("doc_recent", []),
            "synced_at": model.get("synced_at"),
            "groups": model.get("groups", []),
            "stats": model.get("stats", {"today": 0, "total": 0, "groups": 0}),
            "on_top": self.cfg.get("on_top", True),
            "collapsed": self.cfg.get("collapsed", False),
            "alpha_in": int(self.cfg.get("alpha_in", 100)),
            "alpha_out": int(self.cfg.get("alpha_out", 70)),
            "anim_dock_ms": int(self.cfg.get("anim_dock_ms", 160)),
            "anim_alpha_ms": int(self.cfg.get("anim_alpha_ms", 300)),
            "autostart": autostart_get(),
            "dock_side": self.cfg.get("dock_side", ""),
            # 贴边缩进后露出条的 CSS 像素宽度（页面据此画拉手）
            "dock_strip": max(10, int(round(int(self.cfg.get("dock_px", 20)) / window_scale()))),
            "busy": self._busy,
            "error": self._error,
        }
        st.update(extra)
        return st

    def refresh(self, silent=False):
        url = self.cfg.get("doc_url")
        if not url:
            return self.state(error="还没设置文档链接")
        if self._busy:
            return self.state()
        self._busy = True
        try:
            res = bridge.fetch_doc(url)
            if res.get("ok"):
                self.model = res
                self._error = ""
            else:
                self._error = res.get("error", "同步失败")
        except Exception as e:  # noqa: BLE001
            self._error = str(e)
        finally:
            self._busy = False
        return self.state()

    def set_doc(self, url):
        url = (url or "").strip()
        if not url:
            return self.state(error="请填入飞书文档链接")
        self.cfg["doc_url"] = url
        save_config(self.cfg)
        self.model = None
        return self.refresh()

    def save_text(self, block_id, text):
        text = (text or "").strip()
        if not text:
            return self.state(error="内容不能为空")
        if not self.cfg.get("doc_url") or not self.model:
            return self.state(error="请先设置文档并同步")
        res = bridge.update_block(self.cfg["doc_url"], self.model, block_id, text)
        if not res.get("ok"):
            return self.state(error=f"保存失败：{res.get('error')}")
        for g in self.model.get("groups", []):
            for it in g.get("items", []):
                if it.get("id") == block_id:
                    it["text"] = text
        self.model["synced_at"] = int(time.time())
        self._error = ""
        threading.Thread(target=self.refresh, daemon=True).start()
        return self.state()

    def toggle(self, block_id, done):
        if not self.cfg.get("doc_url") or not self.model:
            return self.state(error="请先设置文档并同步")
        target = None
        for g in self.model.get("groups", []):
            for it in g.get("items", []):
                if it.get("id") == block_id:
                    target = it
        if not target:
            return self.state(error="条目已失效，请刷新")
        res = bridge.update_block(self.cfg["doc_url"], self.model, block_id, target["text"], done=done)
        if not res.get("ok"):
            return self.state(error=f"保存失败：{res.get('error')}")
        target["done"] = done
        self._error = ""
        return self.state()

    def add(self, text):
        text = (text or "").strip()
        if not text:
            return self.state(error="内容不能为空")
        if not self.cfg.get("doc_url") or not self.model:
            return self.state(error="请先设置文档并同步")
        res = bridge.add_entry(self.cfg["doc_url"], self.model, text)
        if not res.get("ok"):
            return self.state(error=f"新增失败：{res.get('error')}")
        return self.refresh()

    # ---------------- 窗口行为
    def set_on_top(self, flag):
        self.cfg["on_top"] = bool(flag)
        save_config(self.cfg)
        try:
            if self.win:
                self.win.on_top = bool(flag)
        except Exception:
            pass
        return self.state()

    def collapse(self, flag):
        flag = bool(flag)
        self.cfg["collapsed"] = flag
        save_config(self.cfg)
        w = int(self.cfg.get("width", 700))
        h = COLLAPSED_H if flag else int(self.cfg.get("height", 1080))
        _set_size(w, h)
        return self.state()

    def update_settings(self, data):
        """设置：透明度 / 动画时长 / 开机自启。只更新前端传上来的字段。"""
        def clamp(v, lo, hi, dft):
            try:
                return max(lo, min(hi, int(v)))
            except (TypeError, ValueError):
                return dft
        if "alpha_in" in data:
            self.cfg["alpha_in"] = clamp(data.get("alpha_in"), 30, 100, self.cfg.get("alpha_in", 100))
        if "alpha_out" in data:
            self.cfg["alpha_out"] = clamp(data.get("alpha_out"), 20, 100, self.cfg.get("alpha_out", 70))
        if "anim_dock_ms" in data:
            self.cfg["anim_dock_ms"] = clamp(data.get("anim_dock_ms"), 0, 800,
                                             self.cfg.get("anim_dock_ms", 160))
        if "anim_alpha_ms" in data:
            self.cfg["anim_alpha_ms"] = clamp(data.get("anim_alpha_ms"), 0, 1200,
                                              self.cfg.get("anim_alpha_ms", 300))
        err = ""
        if "autostart" in data:
            want = bool(data.get("autostart"))
            if not autostart_set(want):
                err = "开机自启设置失败，可能被系统策略拦截"
        save_config(self.cfg)
        return self.state(error=err) if err else self.state()

    def open_doc(self):
        import webbrowser
        if self.cfg.get("doc_url"):
            webbrowser.open(self.cfg["doc_url"].strip())
        return self.state()

    def close(self):
        self._persist_geometry()
        _tray_remove()
        try:
            self.win.destroy()
        except Exception:
            os._exit(0)
        return self.state()

    def _persist_geometry(self):
        hwnd = _find_hwnd()
        if hwnd:
            _save_geometry(hwnd)


CORE = Core()


# ---------------------------------------------------------------- HTTP 层
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if self.path.endswith((".html", ".js", ".css")):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_file(self, path, mime=None):
        import mimetypes
        try:
            with open(path, "rb") as f:
                body = f.read()
        except Exception:
            return self._json({"ok": False, "error": "not found"}, 404)
        mime = mime or mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        self.wfile.write(body)

    def _serve_image(self):
        from urllib.parse import urlparse, parse_qs, unquote
        u = urlparse(self.path)
        token = unquote(u.path[len("/api/img/"):]).strip()
        want_thumb = (parse_qs(u.query).get("s") or ["thumb"])[0] != "full"
        if not token:
            return self._json({"ok": False, "error": "no token"}, 404)
        mime = (parse_qs(u.query).get("m") or ["image/png"])[0]
        try:
            path = bridge.ensure_thumb(token, mime) if want_thumb else bridge.ensure_image(token, mime)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": str(e)}, 500)
        if not path:
            return self._json({"ok": False, "error": "图片下载失败"}, 404)
        return self._send_file(path)

    def do_GET(self):
        if self.path.startswith("/api/img/"):
            return self._serve_image()
        if self.path.startswith("/api/probe"):
            js = ("JSON.stringify({ih:innerHeight,iw:innerWidth,dpr:devicePixelRatio,"
                  "strip:getComputedStyle(document.body).getPropertyValue('--strip'),"
                  "db:document.getElementById('dockbar').getBoundingClientRect().toJSON(),"
                  "gp:document.querySelector('.grip').getBoundingClientRect().toJSON(),"
                  "hid:document.body.classList.contains('docked-hidden'),"
                  "dsrc:(document.getElementById('doc')||{}).src||'',"
                  "hl:(typeof docHist!=='undefined')?docHist.length:0,"
                  "hi:(typeof docHi!=='undefined')?docHi:-1})")
            try:
                return self._json({"ok": True, "r": CORE.win.evaluate_js(js)})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)})
        if self.path.startswith("/api/state"):
            return self._json(CORE.state())
        if self.path.startswith("/api/test-newwin"):
            # 临时：模拟文档里点了一个 target=_blank 的链接，验证新窗口拦截链路
            # 可选 ?url=<链接> 指定目标，默认用当前文档
            try:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                u = (qs.get("url") or [CORE.cfg.get("doc_url", "")])[0]
                js = ("const a=document.createElement('a');a.href=%s;a.target='_blank';"
                      "document.body.appendChild(a);a.click();a.remove();" % json.dumps(u))
                CORE.win.evaluate_js(js)
                return self._json({"ok": True, "url": u})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)})
        return super().do_GET()

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            data = {}
        route = self.path.split("?")[0]
        try:
            if route in ("/api/boot", "/api/state"):
                out = CORE.state()
            elif route == "/api/refresh":
                out = CORE.refresh()
            elif route == "/api/set-doc":
                out = CORE.set_doc(data.get("url", ""))
            elif route == "/api/save":
                out = CORE.save_text(data.get("id"), data.get("text"))
            elif route == "/api/toggle":
                out = CORE.toggle(data.get("id"), bool(data.get("done")))
            elif route == "/api/add":
                out = CORE.add(data.get("text"))
            elif route == "/api/on-top":
                out = CORE.set_on_top(bool(data.get("flag")))
            elif route == "/api/collapse":
                out = CORE.collapse(bool(data.get("flag")))
            elif route == "/api/settings":
                out = CORE.update_settings(data)
            elif route == "/api/resize-start":
                out = start_resize(data.get("edge", "bottomright"))
            elif route == "/api/drag-start":
                out = start_drag()
            elif route == "/api/metrics":
                with open(os.path.join(DATA_DIR, "metrics.log"), "w", encoding="utf-8") as f:
                    f.write(json.dumps(data))
                out = {"ok": True}
            elif route == "/api/open-doc":
                out = CORE.open_doc()
            elif route == "/api/recent":
                lst = data.get("list") or []
                CORE.cfg["doc_recent"] = [str(u) for u in lst if isinstance(u, str)][-20:]
                save_config(CORE.cfg)
                out = {"ok": True}
            elif route == "/api/nav-doc":
                # 后退/前进后同步"当前文档"（轻量：只持久化，不触发 refresh）
                url = str(data.get("url") or "").strip()
                if url:
                    CORE.cfg["doc_url"] = url
                    save_config(CORE.cfg)
                    _fetch_title_async(url)
                out = {"ok": True}
            elif route == "/api/close":
                self._json(CORE.state())
                threading.Timer(0.2, CORE.close).start()
                return
            else:
                return self._json({"ok": False, "error": "unknown route"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": str(e)}, 500)
        return self._json(out)


def serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "port.txt"), "w") as f:
            f.write(str(port))
    except Exception:
        pass
    return port


# ---------------------------------------------------------------- 启动
def _life(msg):
    """轻量生命周期日志，方便排查「双击了没窗口」这类问题。"""
    try:
        with open(os.path.join(DATA_DIR, "life.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%F %T ") + msg + "\n")
    except Exception:
        pass


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("rcMonitor", _RC), ("rcWork", _RC),
                ("dwFlags", ctypes.c_uint), ("szDevice", ctypes.c_wchar * 32)]


def _monitor_workarea(x, y, w, h):
    """返回窗口所在显示器的工作区（物理像素）。"""
    u = ctypes.windll.user32
    rc = _RC(int(x), int(y), int(x + w), int(y + h))
    try:
        mon = u.MonitorFromRect(ctypes.byref(rc), 2)  # MONITOR_DEFAULTTONEAREST
        m = _MONITORINFO()
        m.cbSize = ctypes.sizeof(_MONITORINFO)
        if u.GetMonitorInfoW(mon, ctypes.byref(m)):
            return m.rcWork.l, m.rcWork.t, m.rcWork.r, m.rcWork.b
    except Exception:
        pass
    return 0, 0, int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))


def _clamp_rect(x, y, w, h):
    """把窗口矩形（物理像素）夹进所在显示器的工作区，避免跑到看不见的地方。"""
    try:
        wa_l, wa_t, wa_r, wa_b = _monitor_workarea(x, y, w, h)
        w = max(200, min(w, wa_r - wa_l - 16))
        h = max(80, min(h, wa_b - wa_t - 16))
        x = min(max(wa_l + 8, x), wa_r - w - 8)
        y = min(max(wa_t + 8, y), wa_b - h - 8)
    except Exception:
        pass
    return int(x), int(y), int(w), int(h)


def _set_size(w, h):
    """只改尺寸、保持左上角不动（物理像素）。"""
    hwnd = _find_hwnd()
    if not hwnd:
        return
    rc = _RC()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rc))
    ctypes.windll.user32.SetWindowPos(hwnd, 0, rc.l, rc.t, int(w), int(h),
                                      _SWP_NOZORDER | _SWP_NOACTIVATE)
    threading.Timer(0.12, lambda: _apply_webview_scale("resize")).start()


# ---------------------------------------------------------------- WebView2 缩放
def _webview2_controller(form):
    """反射拿 WebView2 控制器（必须在 UI 线程里调用）。"""
    wv = getattr(form, "webview", None)
    if wv is None:
        return None
    try:
        flags = Reflection.BindingFlags.NonPublic | Reflection.BindingFlags.Instance
        fld = wv.GetType().GetField("_coreWebView2Controller", flags)
        if fld is not None:
            return fld.GetValue(wv)
        prop = wv.GetType().GetProperty("CoreWebView2Controller", flags)
        return prop.GetValue(wv) if prop is not None else None
    except Exception:
        return None


def _apply_webview_scale(reason=""):
    """把 WebView2 控制器的光栅化比例对齐到窗口真实 DPI。

    实测规则：网页视口(CSS) = 控制器 Bounds / RasterizationScale。
    所以 Bounds 用客户区物理像素、RasterizationScale 用 dpi/96。
    某些启动时序下 RasterizationScale 会停在 1.0，网页就会按物理像素排版、
    只铺满窗口左上角 2/3；这里重设一次即可（正常情况是幂等的空操作）。
    """
    try:
        form = CORE.win.native
        hwnd = int(form.Handle.ToInt32())
        cr = _RC()
        ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(cr))
        scale = max(1.0, ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0)
        bw = max(1, int(cr.r))
        bh = max(1, int(cr.b))

        def apply():
            try:
                ctrl = _webview2_controller(form)
                if ctrl is None:
                    _life("缩放校准：拿不到 WebView2 控制器")
                    return False
                ctrl.Bounds = Rectangle(0, 0, bw, bh)
                ctrl.RasterizationScale = scale
                return True
            except Exception as e:  # noqa: BLE001
                _life(f"缩放校准异常 {e}")
                return False

        ok = bool(form.Invoke(Func[bool](apply)))
        _life(f"缩放校准 {reason} ok={ok} bounds={bw}x{bh} scale={scale}")
        return ok
    except Exception as e:  # noqa: BLE001
        _life(f"缩放校准失败 {e}")
        return False


def _find_hwnd():
    """拿到贴片窗口的 HWND。

    只用 EnumWindows：不要碰 CORE.win.native 的 WinForms 对象，
    后台线程直接读控件属性会跨线程封送，UI 线程一忙就会卡死。
    """
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _l):
        n = ctypes.create_unicode_buffer(64)
        ctypes.windll.user32.GetWindowTextW(h, n, 64)
        if n.value in WINDOW_TITLES:
            found.append(h)
        return True

    ctypes.windll.user32.EnumWindows(cb, 0)
    return found[0] if found else None


# SetWindowPos flags
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010


def _save_geometry(hwnd):
    """把窗口物理矩形写进配置（收起时只记位置，不记高度）。"""
    try:
        rc = _RC()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rc))
        CORE.cfg["x"] = int(rc.l)
        CORE.cfg["y"] = int(rc.t)
        if not CORE.cfg.get("collapsed"):
            CORE.cfg["width"] = max(200, int(rc.r - rc.l))
            CORE.cfg["height"] = max(80, int(rc.b - rc.t))
        CORE.cfg["geom_unit"] = "px"
        save_config(CORE.cfg)
    except Exception:
        pass


def _drag_loop(hwnd):
    """拖动窗口：全程用物理像素跟随鼠标。

    JS 里拿到的 clientX/screenY 是 CSS 像素，跨到 DPI 不同的屏幕时
    换算基准会变，窗口就会"跳"一下、鼠标从标题栏跑到窗口中间。
    这里改成 Python 端轮询真实光标位置（物理像素），位置只由位移决定，
    与 DPI 无关，所以跨屏拖动能一直贴住鼠标按下的那一点。
    """
    u = ctypes.windll.user32
    pt = _PT()
    rc = _RC()
    u.GetCursorPos(ctypes.byref(pt))
    u.GetWindowRect(hwnd, ctypes.byref(rc))
    ox, oy = rc.l, rc.t
    x0, y0 = pt.x, pt.y
    dpi0 = u.GetDpiForWindow(hwnd)
    _FX["busy"] = True
    # 拖动即取消贴边，否则特效线程会和拖动抢窗口位置
    if CORE.cfg.get("dock_side"):
        CORE.cfg["dock_side"] = ""
        save_config(CORE.cfg)
        _FX["hidden"] = False
    try:
        while u.GetAsyncKeyState(0x01) & 0x8000:  # 左键仍按着
            u.GetCursorPos(ctypes.byref(pt))
            u.SetWindowPos(hwnd, 0, int(ox + pt.x - x0), int(oy + pt.y - y0), 0, 0,
                           _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE)
            time.sleep(0.008)
    except Exception:
        pass
    _FX["busy"] = False
    _save_geometry(hwnd)
    _dock_check(hwnd)
    dpi1 = u.GetDpiForWindow(hwnd)
    if dpi1 != dpi0:
        # 跨到 DPI 不同的显示器，重算一次 WebView2 的 DIP 尺寸，内容才不会错位
        _apply_webview_scale(f"跨屏 {dpi0}->{dpi1}")
    _life(f"拖动结束 {pt.x},{pt.y} dpi={dpi0}->{dpi1}")


def start_drag():
    hwnd = _find_hwnd()
    _life(f"drag-start hwnd={hwnd} btn={bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)}")
    if not hwnd:
        return {"ok": False, "error": "no window"}
    threading.Thread(target=_drag_loop, args=(hwnd,), daemon=True).start()
    return {"ok": True}


def _resize_loop(hwnd, edge):
    """拖边缩放：跟随鼠标调整窗口大小（frameless 窗口本身没有缩放边框）。"""
    u = ctypes.windll.user32

    pt = _PT()
    u.GetCursorPos(ctypes.byref(pt))
    rc = _RC()
    u.GetWindowRect(hwnd, ctypes.byref(rc))
    ox, oy, ow, oh = rc.l, rc.t, rc.r - rc.l, rc.b - rc.t
    x0, y0 = pt.x, pt.y
    minw, minh = px(340), px(90)
    _FX["busy"] = True

    while u.GetAsyncKeyState(0x01) & 0x8000:  # 左键仍按着
        u.GetCursorPos(ctypes.byref(pt))
        dx, dy = pt.x - x0, pt.y - y0
        nx, ny, nw, nh = ox, oy, ow, oh
        if "left" in edge:
            nx, nw = ox + dx, ow - dx
        if "right" in edge:
            nw = ow + dx
        if "top" in edge:
            ny, nh = oy + dy, oh - dy
        if "bottom" in edge:
            nh = oh + dy
        if nw < minw:
            if "left" in edge:
                nx = ox + ow - minw
            nw = minw
        if nh < minh:
            if "top" in edge:
                ny = oy + oh - minh
            nh = minh
        # 注意：这里不能带 SWP_NOMOVE —— 拖左边/上边时位置也要跟着变，
        # 否则只有右边/下边在动（就是"从左边缩小结果右边变大小"的原因）。
        u.SetWindowPos(hwnd, 0, int(nx), int(ny), int(nw), int(nh),
                       _SWP_NOZORDER | _SWP_NOACTIVATE)
        time.sleep(0.008)

    # 结束后保存新位置/尺寸（换算成逻辑坐标，下次启动按主屏 DPI 还原）
    _FX["busy"] = False
    _save_geometry(hwnd)
    if CORE.cfg.get("dock_side"):
        # 贴边状态下改大小：重新贴齐，展开状态保持
        rc2 = _RC()
        u.GetWindowRect(hwnd, ctypes.byref(rc2))
        (_hx, _hy), (sx, sy) = _dock_pos(CORE.cfg["dock_side"], rc2)
        u.SetWindowPos(hwnd, 0, int(sx), int(sy), 0, 0,
                       _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE)
        _save_geometry(hwnd)
    _life(f"拖拽缩放完成 edge={edge}")


def start_resize(edge):
    hwnd = _find_hwnd()
    if not hwnd:
        return {"ok": False, "error": "no window"}
    threading.Thread(target=_resize_loop, args=(hwnd, edge or "bottomright"), daemon=True).start()
    return {"ok": True}


# ------------------------------------------------- 半透明 & 贴边隐藏
_FX = {
    "busy": False,       # 拖动/缩放进行中，特效线程暂停干预
    "alpha": None,       # 当前整窗不透明度（%），None=未初始化
    "hidden": False,     # 贴边状态下窗口是否已缩进（只留一条）
    "grip": None,        # 页面里拉手当前的显隐状态（None=还没下发过）
    "last_hover": 0.0,   # 最近一次鼠标在窗口上的时间
}

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_LWA_ALPHA = 0x00000002


def _ensure_layered(hwnd):
    """给窗口加 WS_EX_LAYERED，之后才能用 SetLayeredWindowAttributes 调透明度。"""
    u = ctypes.windll.user32
    ex = u.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    if not ex & _WS_EX_LAYERED:
        u.SetWindowLongW(hwnd, _GWL_EXSTYLE, ex | _WS_EX_LAYERED)
    u.SetLayeredWindowAttributes(hwnd, 0, 255, _LWA_ALPHA)


def _set_alpha(hwnd, pct):
    """整窗不透明度（%）。必须走系统层：页面 opacity 只会让内容变淡、
    底下的窗口白底还在，看着仍然是"不透明的一张卡"。"""
    pct = max(10, min(100, int(pct)))
    ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0, int(pct * 2.55), _LWA_ALPHA)


def _set_grip(show):
    """通知页面显示/隐藏贴边拉手（拉手只在缩进状态出现）。

    页面可能还没加载完，失败就先不记状态，下一轮继续重试。
    """
    try:
        CORE.win.evaluate_js(
            "document.body.classList.toggle('docked-hidden',%s)" % ("true" if show else "false")
        )
        _FX["grip"] = bool(show)
        return True
    except Exception:
        return False


def _cursor_over(hwnd, margin=4):
    """鼠标（物理坐标）是否在窗口矩形（外扩 margin）内。"""
    u = ctypes.windll.user32
    pt = _PT()
    u.GetCursorPos(ctypes.byref(pt))
    rc = _RC()
    u.GetWindowRect(hwnd, ctypes.byref(rc))
    return (rc.l - margin <= pt.x <= rc.r + margin and
            rc.t - margin <= pt.y <= rc.b + margin)


def _dock_pos(side, rc):
    """返回 (缩进位置, 展开位置) 的窗口左上角坐标（物理像素）。"""
    w, h = rc.r - rc.l, rc.b - rc.t
    wa_l, wa_t, wa_r, wa_b = _monitor_workarea(rc.l, rc.t, w, h)
    strip = int(CORE.cfg.get("dock_px", 20))
    y = min(max(wa_t, rc.t), wa_b - h)
    x = min(max(wa_l, rc.l), wa_r - w)
    if side == "left":
        return (wa_l - w + strip, y), (wa_l, y)
    if side == "right":
        return (wa_r - strip, y), (wa_r - w, y)
    if side == "top":
        return (x, wa_t - h + strip), (x, wa_t)
    return None, None


def _animate_move(hwnd, tx, ty, alpha_to=None):
    """窗口平移动画（物理像素）。

    位移与透明度各走自己的时长：位移用 anim_dock_ms（收起/展开），
    透明度用 anim_alpha_ms（明暗渐变）。两段时长不同也互不牵制 ——
    同一帧里各按自己的进度插值，短的那段先到位、长的继续走完。
    """
    u = ctypes.windll.user32

    def _ms(key, dft):
        try:
            return max(0, int(CORE.cfg.get(key, dft)))
        except (TypeError, ValueError):
            return dft

    dock_ms = _ms("anim_dock_ms", 160)
    alpha_ms = _ms("anim_alpha_ms", 300) if alpha_to is not None else 0
    rc = _RC()
    u.GetWindowRect(hwnd, ctypes.byref(rc))
    x0, y0 = rc.l, rc.t
    a0 = _FX.get("alpha")
    dur = max(dock_ms, alpha_ms)
    if dur <= 0:
        u.SetWindowPos(hwnd, 0, int(tx), int(ty), 0, 0,
                       _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE)
        if alpha_to is not None:
            _set_alpha(hwnd, int(alpha_to))
            _FX["alpha"] = int(alpha_to)
        return
    steps = max(2, min(40, int(round(dur / 20.0))))
    interval = (dur / 1000.0) / steps
    for i in range(1, steps + 1):
        t = interval * i * 1000.0                      # 已过去多少毫秒
        kd = 1.0 if dock_ms <= 0 else min(1.0, t / dock_ms)
        ka = 1.0 if alpha_ms <= 0 else min(1.0, t / alpha_ms)
        nx = int(round(x0 + (tx - x0) * kd))
        ny = int(round(y0 + (ty - y0) * kd))
        u.SetWindowPos(hwnd, 0, nx, ny, 0, 0,
                       _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE)
        if alpha_to is not None and a0 is not None:
            av = int(round(a0 + (alpha_to - a0) * ka))
            if av != _FX.get("alpha"):
                _set_alpha(hwnd, av)
                _FX["alpha"] = av
        time.sleep(interval)


def _dock_check(hwnd):
    """拖动结束后调用：靠近屏幕边缘就吸附，拖走了就取消贴边。"""
    u = ctypes.windll.user32
    rc = _RC()
    u.GetWindowRect(hwnd, ctypes.byref(rc))
    w, h = rc.r - rc.l, rc.b - rc.t
    wa_l, wa_t, wa_r, wa_b = _monitor_workarea(rc.l, rc.t, w, h)
    near = px(18)
    side = ""
    if rc.l - wa_l <= near:
        side = "left"
    elif wa_r - rc.r <= near:
        side = "right"
    elif rc.t - wa_t <= near:
        side = "top"
    CORE.cfg["dock_side"] = side
    save_config(CORE.cfg)
    _FX["hidden"] = False
    if side:
        # 吸附：先贴齐边缘完全展开，随后的特效线程接管隐藏/弹出
        (_hx, _hy), (sx, sy) = _dock_pos(side, rc)
        u.SetWindowPos(hwnd, 0, int(sx), int(sy), 0, 0,
                       _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE)
        _save_geometry(hwnd)
        _life(f"贴边吸附 side={side}")
    return side


def _fx_loop():
    """特效主循环：半透明渐变 + 贴边缩进/弹出动画 + 拉手显隐。"""
    u = ctypes.windll.user32
    _FX["last_hover"] = time.time()
    started = False
    _life("特效循环已启动")
    while True:
        time.sleep(0.06)
        try:
            hwnd = _find_hwnd()
            if not hwnd or not u.IsWindow(hwnd):
                continue
            if not started:
                _ensure_layered(hwnd)
                started = True
            if _FX["busy"]:
                _FX["last_hover"] = time.time()
                continue
            rc = _RC()
            u.GetWindowRect(hwnd, ctypes.byref(rc))
            w, h = rc.r - rc.l, rc.b - rc.t
            if w <= 0 or h <= 0:
                continue
            hover = _cursor_over(hwnd)
            if hover:
                _FX["last_hover"] = time.time()

            # ---- 贴边隐藏：鼠标离开就缩回；透明度目标先算好，交给同一段动画插值
            cfg = CORE.cfg
            side = cfg.get("dock_side")
            docked = side in ("left", "right", "top")
            if docked:
                (hx, hy), (sx, sy) = _dock_pos(side, rc)
                want_hidden = (not hover) and (time.time() - _FX["last_hover"] > 0.15)
            else:
                want_hidden = False
            changed = want_hidden != _FX["hidden"]
            _FX["hidden"] = want_hidden

            # ---- 透明度目标
            # 贴边状态：移开时不先变淡（否则会"先透明再收回"），缩进后拉手处保持不透明；
            # 未贴边：移入 alpha_in、移出 alpha_out。
            if _FX["hidden"]:
                target = 100
            elif docked:
                target = int(cfg.get("alpha_in", 100))
            else:
                target = int(cfg.get("alpha_in", 100)) if hover else int(cfg.get("alpha_out", 70))

            if docked:
                if changed:
                    _animate_move(hwnd, hx if want_hidden else sx,
                                  hy if want_hidden else sy, alpha_to=target)
                elif want_hidden and (abs(rc.l - hx) > 2 or abs(rc.t - hy) > 2):
                    # 被外部挪过（启动时 _apply_geometry 会摆正一次），重新贴回缩进位
                    _animate_move(hwnd, hx, hy, alpha_to=target)

            # 拉手在缩进完成后才出现；页面没加载好会自动重试
            if _FX.get("grip") is not _FX["hidden"]:
                _set_grip(_FX["hidden"])

            # ---- 未贴边时的明暗渐变：按 anim_alpha_ms 折算每帧步长（本循环约 60ms 一跳）
            cur = _FX["alpha"]
            if cur is None:
                _FX["alpha"] = target
                _set_alpha(hwnd, target)
            elif cur != target:
                try:
                    ams = max(0, int(cfg.get("anim_alpha_ms", 300)))
                except (TypeError, ValueError):
                    ams = 300
                if ams <= 0:
                    _FX["alpha"] = target
                    _set_alpha(hwnd, target)
                else:
                    step = max(1.0, 100.0 * 0.06 / (ams / 1000.0))
                    if abs(target - cur) <= step:
                        nv = target
                    else:
                        nv = int(round(cur + (step if target > cur else -step)))
                    if nv != cur:
                        _FX["alpha"] = nv
                        _set_alpha(hwnd, nv)
        except Exception as e:  # noqa: BLE001
            if time.time() - _FX.get("lasterr", 0) > 5:
                _FX["lasterr"] = time.time()
                _life(f"特效循环异常: {e!r}")


def _polish_window():
    """窗口出现后做外观打磨：DWM 系统圆角（Win11 生效，Win10 自动跳过）。

    原理：去掉 transparent 后窗口由 DWM 合成，直接用
    DWMWA_WINDOW_CORNER_PREFERENCE(33) = DWMWCP_ROUND(2) 裁圆角，
    比 WebView2 透明背景 + CSS 圆角的 hack 稳定得多。
    """
    hwnd = None
    for _ in range(40):  # 最多等 20 秒
        hwnd = _find_hwnd()
        if hwnd:
            break
        time.sleep(0.5)
    if not hwnd:
        return
    _hide_from_taskbar(hwnd)
    try:
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
        _life(f"DWM圆角已设置 hr={hr}" if hr == 0 else f"DWM圆角不支持 hr={hr}(Win10)")
    except Exception:
        _life("DWM圆角设置异常(Win10)，保持直角")


# ------------------------------------------------ 任务栏隐藏 + 托盘图标
TRAY_CALLBACK = 0x8001   # WM_APP+1：托盘鼠标事件回传消息
_tray = {"added": False, "nid": None}


def _hide_from_taskbar(hwnd):
    """加 WS_EX_TOOLWINDOW：不出现在任务栏和 Alt+Tab，只留托盘入口。"""
    u = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x80
    WS_EX_APPWINDOW = 0x40000
    try:
        ex = u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                            (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        # SWP_NOSIZE|SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE|SWP_FRAMECHANGED
        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x1 | 0x2 | 0x4 | 0x10 | 0x20)
        _life("已从任务栏隐藏(WS_EX_TOOLWINDOW)")
    except Exception as e:  # noqa: BLE001
        _life(f"任务栏隐藏失败: {e!r}")


def _tray_icon_handle():
    """托盘图标：没有现成 ico 就用 PIL 现画一个（蓝底白色文档线条）。"""
    if _tray.get("hicon"):
        return _tray["hicon"]
    u = ctypes.windll.user32
    path = os.path.join(DATA_DIR, "app.ico")
    try:
        if not os.path.exists(path):
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((4, 4, 60, 60), 14, fill=(51, 112, 255, 255))
            for y0, x1 in ((20, 48), (32, 48), (44, 36)):
                d.rounded_rectangle((16, y0, x1, y0 + 6), 3, fill=(255, 255, 255, 255))
            img.save(path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
        hicon = u.LoadImageW(None, path, 1, 0, 0, 0x10)  # IMAGE_ICON | LR_LOADFROMFILE
        if hicon:
            _tray["hicon"] = hicon
    except Exception as e:  # noqa: BLE001
        _life(f"托盘图标生成失败: {e!r}")
    return _tray.get("hicon")


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint), ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint), ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint), ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128), ("dwState", ctypes.c_uint),
        ("dwStateMask", ctypes.c_uint), ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint), ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint),
    ]


def _tray_add(hwnd):
    u = ctypes.windll.shell32
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(nid)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = 0x1 | 0x2 | 0x4               # NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = TRAY_CALLBACK
    nid.hIcon = _tray_icon_handle() or ctypes.windll.user32.LoadIconW(None, 32512)
    nid.szTip = WINDOW_TITLE
    if u.Shell_NotifyIconW(0, ctypes.byref(nid)):       # NIM_ADD
        _tray["added"] = True
        _tray["nid"] = nid
        _life("托盘图标已添加")


def _tray_remove():
    if _tray.get("added"):
        try:
            ctypes.windll.shell32.Shell_NotifyIconW(2, ctypes.byref(_tray["nid"]))  # NIM_DELETE
        except Exception:  # noqa: BLE001
            pass
        _tray["added"] = False


def _tray_toggle():
    u = ctypes.windll.user32
    hwnd = _find_hwnd()
    if not hwnd:
        return
    if u.IsWindowVisible(hwnd):
        u.ShowWindow(hwnd, 0)                  # SW_HIDE
        _life("托盘：隐藏贴片")
    else:
        u.ShowWindow(hwnd, 5)                  # SW_SHOW
        u.SetForegroundWindow(hwnd)
        _life("托盘：显示贴片")


def _tray_menu(hwnd):
    u = ctypes.windll.user32
    import ctypes.wintypes as wt
    hmenu = u.CreatePopupMenu()
    u.AppendMenuW(hmenu, 0, 1, "显示 / 隐藏")
    u.AppendMenuW(hmenu, 0x800, 0, None)       # 分隔线
    u.AppendMenuW(hmenu, 0, 2, "退出")
    pt = wt.POINT()
    u.GetCursorPos(ctypes.byref(pt))
    # 先 SetForegroundWindow 再 TrackPopupMenu：托盘菜单点外面收不回去的老毛病
    u.SetForegroundWindow(hwnd)
    u.TrackPopupMenu.restype = ctypes.c_int
    cmd = u.TrackPopupMenu(hmenu, 0x0100 | 0x0080, pt.x, pt.y, 0, hwnd, None)  # TPM_RETURNCMD | TPM_NONOTIFY
    u.DestroyMenu(hmenu)
    u.PostMessageW(hwnd, 0, 0, 0)              # WM_NULL
    if cmd == 2:
        threading.Timer(0, CORE.close).start()


def _tray_wndproc_impl(hwnd, msg, wparam, lparam):
    u = ctypes.windll.user32
    if msg == TRAY_CALLBACK:
        ev = lparam & 0xFFFF
        if ev in (0x0202, 0x0203):             # WM_LBUTTONUP / 双击
            _tray_toggle()
        elif ev == 0x0205:                     # WM_RBUTTONUP
            _tray_menu(hwnd)
    return u.DefWindowProcW(hwnd, msg, wparam, lparam)


TRAY_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
_tray_wndproc_ref = TRAY_WNDPROC(_tray_wndproc_impl)   # 保活，防 GC 回调失效


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint), ("lpfnWndProc", TRAY_WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p),
    ]


class TRAY_MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt_x", ctypes.c_long), ("pt_y", ctypes.c_long),
    ]


def _tray_loop():
    """托盘常驻线程：建一个消息窗口接回调，注册 Shell_NotifyIcon，然后跑消息泵。"""
    try:
        u = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        u.CreateWindowExW.restype = ctypes.c_void_p
        # HMODULE 是 64 位指针：必须按 c_void_p 取，否则 ASLR 高地址时会 int 溢出
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        hInstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = _tray_wndproc_ref
        wc.lpszClassName = "FeishuLogTray"
        wc.hInstance = hInstance
        if not u.RegisterClassW(ctypes.byref(wc)):
            _life("托盘：窗口类注册失败")
            return
        hwnd = u.CreateWindowExW(0, wc.lpszClassName, "FeishuLogTray", 0,
                                 0, 0, 0, 0, ctypes.c_void_p(-3),  # HWND_MESSAGE
                                 None, ctypes.c_void_p(hInstance), None)
        if not hwnd:
            _life("托盘：消息窗口创建失败")
            return
        _tray_add(hwnd)
        msg = TRAY_MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
    except Exception as e:  # noqa: BLE001
        _life(f"托盘线程异常: {e!r}\n{traceback.format_exc()}")




def _offscreen_guard():
    """兜底看门狗：窗口若落在所有显示器之外（DPI/多屏坐标问题），自动拉回主屏右上角。"""
    u = ctypes.windll.user32
    while True:
        time.sleep(10)
        try:
            hwnd = _find_hwnd()
            if not hwnd or not u.IsWindow(hwnd):
                continue
            if CORE.cfg.get("dock_side"):
                continue  # 贴边隐藏时窗口本来就有一大截在屏幕外
            if u.MonitorFromWindow(hwnd, 0):  # MONITOR_DEFAULTTONULL：0 表示完全在屏幕外
                continue
            class RC(ctypes.Structure):
                _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                            ("r", ctypes.c_long), ("b", ctypes.c_long)]
            class MI(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("rcMonitor", RC),
                            ("rcWork", RC), ("dwFlags", ctypes.c_uint),
                            ("szDevice", ctypes.c_wchar * 32)]
            m = MI()
            m.cbSize = ctypes.sizeof(MI)
            u.GetMonitorInfoW(u.MonitorFromWindow(0, 1), ctypes.byref(m))  # 主屏
            r = RC()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            w = max(200, r.r - r.l)
            nx = max(m.rcMonitor.l + 20, m.rcMonitor.r - w - int(60 * SCALE))
            ny = m.rcMonitor.t + 120
            u.SetWindowPos(hwnd, 0, int(nx), int(ny), 0, 0,
                           0x0001 | 0x0002 | 0x0010)  # SWP_NOSIZE|SWP_NOZORDER|SWP_NOACTIVATE
            _life(f"看门狗：窗口在屏幕外，已拉回 x={int(nx)} y={int(ny)}")
        except Exception:
            pass


def _apply_geometry(x, y, w, h):
    """窗口出来后按物理像素摆正位置/尺寸，再校准 WebView2 的 DIP 尺寸。

    pywebview 创建窗口时会把 x/y/width/height 当 DIP 再乘一次 DPI，且在
    DPI 还没确定时可能算错（窗口偏小、网页只铺左上角 2/3）。这里统一用
    物理像素收口，页面的可视区域就一定和窗口一致。
    """
    hwnd = None
    for _ in range(80):
        hwnd = _find_hwnd()
        if hwnd:
            break
        time.sleep(0.25)
    if not hwnd:
        _life("摆正失败：找不到窗口")
        return
    u = ctypes.windll.user32
    time.sleep(0.5)
    u.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h), _SWP_NOZORDER | _SWP_NOACTIVATE)
    _life(f"几何已摆正 x={x} y={y} {w}x{h}")
    time.sleep(1.0)
    _apply_webview_scale("启动")
    time.sleep(2.0)
    _apply_webview_scale("启动+2s")  # WebView2 初始化可能晚于第一次校准


# ------------------------------------------------- 新窗口拦截
# 飞书文档里的链接都是 target=_blank，WebView2 会触发 NewWindowRequested，
# pywebview 默认把它交给系统浏览器。这里接管：飞书域名的链接留在贴片 iframe
# 里打开（并记住为当前文档），其他外链仍交给系统浏览器。
_FEISHU_HOSTS = ("feishu.cn", "feishu.net", "larksuite.com")


def _is_feishu_url(url):
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in _FEISHU_HOSTS)
    except Exception:
        return False


def _title_key(url):
    """标题缓存的键：域名 + 路径（去掉 ?from=... 之类的查询串，避免同一文档多条记录）。"""
    try:
        from urllib.parse import urlparse
        o = urlparse(url)
        return (o.hostname or "") + o.path.rstrip("/")
    except Exception:
        return url


def _fetch_title_async(url):
    """后台取文档标题（走 lark CLI），成功后回填「最近文档」列表，失败就算了。"""
    titles = CORE.cfg.setdefault("doc_titles", {})
    if not url or _title_key(url) in titles:
        return

    def run():
        try:
            m = bridge.fetch_doc(url)
            title = (m or {}).get("doc_title")
            if m.get("ok") and title:
                CORE.cfg.setdefault("doc_titles", {})[_title_key(url)] = title
                save_config(CORE.cfg)
                try:
                    CORE.win.evaluate_js(
                        "window.refreshDocTitle && refreshDocTitle(%s,%s)"
                        % (json.dumps(_title_key(url)), json.dumps(title)))
                except Exception:
                    pass
                _life(f"文档标题: {title}")
        except Exception as e:  # noqa: BLE001
            _life(f"取文档标题失败: {e!r}")

    threading.Thread(target=run, daemon=True).start()


def _open_in_doc(url):
    """把文档链接装进内容 iframe，并记住为当前文档（重启后还打开它）。"""
    try:
        CORE.cfg["doc_url"] = url
        save_config(CORE.cfg)
    except Exception:
        pass
    try:
        CORE.win.evaluate_js("loadDoc(%s)" % json.dumps(url))
    except Exception as e:  # noqa: BLE001
        _life(f"iframe 加载链接失败: {e!r}")
    _fetch_title_async(url)


def _patch_new_window():
    try:
        import webbrowser
        from webview.platforms import edgechromium

        def on_new_window_request(self, sender, args):
            args.set_Handled(True)
            url = str(args.get_Uri())
            if _is_feishu_url(url):
                _life(f"新窗口请求 → 贴片内打开: {url}")
                # 注意：本事件回调运行在 WebView2 的 UI 线程上，evaluate_js 要等
                # UI 线程空闲 —— 直接调用会死锁。必须丢到别的线程异步执行。
                threading.Timer(0, _open_in_doc, args=(url,)).start()
            else:
                _life(f"新窗口请求 → 系统浏览器: {url}")
                webbrowser.open(url)

        edgechromium.EdgeChrome.on_new_window_request = on_new_window_request
        _life("已接管新窗口请求（飞书链接在贴片内打开）")
    except Exception as e:  # noqa: BLE001
        _life(f"新窗口补丁失败: {e!r}")


def main():
    _life("启动")
    # 单实例保护：已有一个贴片在跑时，再次双击启动直接退出，避免叠出多个窗口
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False, "WorkLogDesktopWidget")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            _life("已有实例在运行，本次直接退出")
            return
    except Exception:
        pass
    port = serve()
    cfg = CORE.cfg
    w = int(cfg.get("width", 700))     # 物理像素
    h = int(cfg.get("height", 1080))
    if cfg.get("collapsed"):
        h = COLLAPSED_H
    if cfg.get("x") is not None and cfg.get("y") is not None:
        x, y, w, h = _clamp_rect(int(cfg["x"]), int(cfg["y"]), w, h)
    else:
        # 首次启动贴在主屏右上角
        wa_l, wa_t, wa_r, wa_b = _monitor_workarea(0, 0, 1, 1)
        x, y, w, h = _clamp_rect(wa_r - w - int(60 * SCALE), wa_t + int(80 * SCALE), w, h)
    kwargs = dict(
        title=WINDOW_TITLE,
        url=f"http://127.0.0.1:{port}/index.html",
        # 先按逻辑值给个近似尺寸，等窗口出来再用物理像素摆正（见 _apply_geometry）
        width=max(200, int(w / SCALE)),
        height=max(60, int(h / SCALE)),
        x=int(x / SCALE),
        y=int(y / SCALE),
        frameless=True,
        easy_drag=False,  # 只在顶部状态条拖动（自己实现，跨屏不跳）
        on_top=cfg.get("on_top", True),
        transparent=False,  # 透明在 WebView2 上不可靠，圆角改用 DWM 系统圆角
        background_color="#FFFFFF",
        shadow=True,
        resizable=True,
        min_size=(260, 44),
    )
    window = webview.create_window(**kwargs)
    CORE.win = window
    _life(f"窗口已创建，目标物理矩形 x={x} y={y} {w}x{h}")
    # 尺寸/位置统一由拖动、缩放循环结束时按物理像素写回（_save_geometry），
    # 这里不监听 resized —— 事件给的是 DIP 值，和物理像素记录会打架。
    try:
        window.events.loaded += lambda: _life("页面已加载")
        window.events.shown += lambda: _life("窗口已显示")
    except Exception:
        pass
    threading.Timer(1.0, lambda: CORE.refresh()).start()
    threading.Timer(4.0, lambda: _fetch_title_async(cfg.get("doc_url", ""))).start()
    threading.Thread(target=_polish_window, daemon=True).start()
    threading.Thread(target=_tray_loop, daemon=True).start()
    threading.Thread(target=_apply_geometry, args=(x, y, w, h), daemon=True).start()
    threading.Thread(target=_offscreen_guard, daemon=True).start()
    threading.Thread(target=_fx_loop, daemon=True).start()
    _patch_new_window()
    # 关闭隐私模式 + 指定存储目录：飞书登录态（cookie）才能跨重启保留
    profile = os.path.join(DATA_DIR, "webview-profile")
    os.makedirs(profile, exist_ok=True)
    webview.start(debug=False, private_mode=False, storage_path=profile)
    _life("主循环退出")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "crash.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%F %T ") + traceback.format_exc() + "\n")
        raise
