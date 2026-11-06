# -*- coding: utf-8 -*-
"""
飞书文档快速浏览 —— 数据桥接层
通过 lark-cli 调用飞书开放能力，目前只负责一件事：后台抓取文档标题。
（内容渲染由 iframe 直接加载飞书页面完成，无需在本地解析文档。）
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from html import unescape

# 与 app.py 保持一致：打包后可写目录统一放 %APPDATA%\FeishuDocQuickBrowser
if getattr(sys, "frozen", False):
    APP_DIR = os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"), "FeishuDocQuickBrowser"
    )
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

LOG_PATH = os.path.join(DATA_DIR, "bridge.log")


# ---------------------------------------------------------------- lark-cli 调用

def _lark_candidates():
    cands = []
    node_root = os.path.join(
        os.path.expanduser("~"), ".workbuddy", "binaries", "node", "cli-connector-packages"
    )
    run_js = os.path.join(node_root, "lark-cli", "node_modules", "@larksuite", "cli", "scripts", "run.js")
    nodes = [shutil.which("node")]
    versions = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "node", "versions")
    try:
        for d in sorted(os.listdir(versions), reverse=True):
            nodes.append(os.path.join(versions, d, "node.exe"))
    except Exception:
        pass
    for node in nodes:
        if node and os.path.exists(node) and os.path.exists(run_js):
            cands.append([node, run_js])
            break
    cmd = os.path.join(node_root, "lark-cli.cmd")
    if os.path.exists(cmd):
        cands.append(["cmd", "/c", cmd])
    exe = shutil.which("lark-cli")
    if exe:
        cands.append([exe])
    return cands


_TMP_DIR = os.path.join(DATA_DIR, "tmp")
# 这些参数可能含有 <>&|^" 等字符，走命令行会被 cmd.exe 当成重定向，统一改成 @文件
_FILE_FLAGS = {"--content", "--pattern"}


def _prepare_args(args):
    os.makedirs(_TMP_DIR, exist_ok=True)
    out = []
    for i, a in enumerate(args):
        if i > 0 and args[i - 1] in _FILE_FLAGS and isinstance(a, str) and a:
            path = os.path.join(_TMP_DIR, f"arg_{int(time.time()*1000)}_{i}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(a)
            out.append("@" + path.replace("\\", "/"))
        else:
            out.append(a)
    return out


def _log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def run_lark(args, timeout=90):
    """执行 lark-cli，返回解析后的 JSON（约定 CLI 输出 {"ok":..} 结构）。"""
    args = _prepare_args(args)
    last_err = None
    for base in _lark_candidates():
        try:
            p = subprocess.run(
                base + args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out = (p.stdout or "").strip()
            if not out:
                last_err = (p.stderr or "").strip()[:400] or "lark-cli 无输出"
                continue
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                start = out.find("{")
                if start >= 0:
                    try:
                        return json.loads(out[start:])
                    except Exception:
                        pass
                last_err = out[:400]
        except FileNotFoundError as e:
            last_err = f"未找到 lark-cli: {e}"
        except subprocess.TimeoutExpired:
            last_err = "lark-cli 执行超时"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    _log(f"run_lark failed: {last_err} | args={args[:6]}")
    return {"ok": False, "error": {"message": last_err or "lark-cli 调用失败"}}


# ---------------------------------------------------------------- 文档标题

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)


def fetch_title(doc_url):
    """抓取文档标题（纯文本），失败返回空串。"""
    res = run_lark([
        "docs", "+fetch", "--doc", doc_url,
        "--doc-format", "xml", "--as", "user", "--format", "json",
    ])
    if not res.get("ok"):
        err = res.get("error") or {}
        _log(f"fetch_title failed: {err.get('message') if isinstance(err, dict) else err}")
        return ""
    doc = (res.get("data") or {}).get("document") or {}
    m = _TITLE_RE.search(doc.get("content", ""))
    if not m:
        return (doc.get("title") or "").strip()
    text = unescape(re.sub(r"<[^>]+>", "", m.group(1)))
    return re.sub(r"\s+", " ", text).strip()
