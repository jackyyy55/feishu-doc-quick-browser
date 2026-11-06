# -*- coding: utf-8 -*-
"""
飞书工作日志 —— 数据桥接层
通过 lark-cli 读写飞书云文档，并把文档 XML 解析成 widget 需要的结构化数据。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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

CACHE_PATH = os.path.join(DATA_DIR, "cache.json")
LOG_PATH = os.path.join(DATA_DIR, "bridge.log")
IMG_DIR = os.path.join(DATA_DIR, "img")
THUMB_DIR = os.path.join(IMG_DIR, "t")

_HEADING = re.compile(r"^h[1-9]$")


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


def _err(res):
    err = res.get("error") or {}
    if isinstance(err, dict):
        return err.get("message") or err.get("subtype") or "未知错误"
    return str(err)


# ---------------------------------------------------------------- 解析文档 XML

_TAG_RE = re.compile(
    r"<(?P<tag>h[1-9]|p|li|checkbox|blockquote|quote|title)(?P<attrs>\s[^>]*)?>(?P<body>.*?)</(?P=tag)>"
    r"|<img(?P<iattrs>[^>]*?)/?>",
    re.S,
)
_DATE_PATTERNS = [
    re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})"),
    re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]"),
]


def _strip_tags(xml_text):
    t = re.sub(r"<br\s*/?>", " ", xml_text or "")
    t = re.sub(r"<[^>]+>", "", t)
    t = unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _attr(attrs, keys):
    if not attrs:
        return None
    found = {}
    for m in re.finditer(r'([\w:-]+)\s*=\s*"([^"]*)"', attrs):
        found[m.group(1)] = m.group(2)
    for k in keys:
        if k in found:
            return found[k]
    for k, v in found.items():
        if "block" in k.lower() and "id" in k.lower():
            return v
    return None


def parse_doc(content):
    """把飞书文档 XML 解析为 title + 有序 block 列表。表格内容整体跳过。"""
    content = re.sub(r"<table\b.*?</table>", "", content or "", flags=re.S)
    blocks = []
    title = ""
    for m in _TAG_RE.finditer(content):
        tag = m.group("tag")
        if tag is None:  # <img .../>
            ia = m.group("iattrs") or ""
            src = _attr(ia, ["src", "token", "file-token"])
            if not src:
                continue
            blocks.append({
                "id": src,
                "tag": "img",
                "text": "",
                "done": None,
                "token": src,
                "mime": _attr(ia, ["mime"]) or "image/png",
            })
            continue
        text = _strip_tags(m.group("body"))
        if tag == "title":
            title = text
            continue
        if not text:
            continue
        if tag == "quote":
            tag = "blockquote"
        bid = _attr(m.group("attrs"), ["block-id", "block_id", "id"])
        done = None
        if tag == "checkbox":
            done = 'done="true"' in (m.group("attrs") or "") or "done='true'" in (m.group("attrs") or "")
        blocks.append({"id": bid, "tag": tag, "text": text, "done": done})
    return {"title": title, "blocks": blocks}


# ---------------------------------------------------------------- 文档图片
_IMG_SLOTS = threading.Semaphore(3)  # 限制并发下载，避免滚动时一口气拉起几十个 lark-cli


def _ext_of(mime):
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get((mime or "").lower(), ".png")


def _find_image(token):
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        p = os.path.join(IMG_DIR, token + ext)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None


def ensure_image(token, mime="image/png"):
    """把文档里的图片下载到本地缓存，返回本地路径（失败返回 None）。"""
    token = (token or "").strip()
    if not token or "/" in token or "\\" in token:
        return None
    hit = _find_image(token)
    if hit:
        return hit
    os.makedirs(IMG_DIR, exist_ok=True)
    base = os.path.join(IMG_DIR, token)
    with _IMG_SLOTS:
        hit = _find_image(token)
        if hit:
            return hit
        res = run_lark([
            "docs", "+media-download", "--token", token,
            "--output", base, "--overwrite", "--as", "user", "--format", "json",
        ], timeout=120)
    if not res.get("ok"):
        _log(f"media-download failed {token}: {_err(res)}")
        return None
    saved = ((res.get("data") or {}).get("saved_path")) or ""
    if saved and os.path.exists(saved):
        if not saved.startswith(IMG_DIR):
            target = base + os.path.splitext(saved)[1]
            try:
                shutil.move(saved, target)
                saved = target
            except Exception:
                pass
        return saved
    return _find_image(token)


def ensure_thumb(token, mime="image/png", width=680):
    """生成用于列表内展示的缩略图，避免一口气加载几十兆原图。"""
    src = ensure_image(token, mime)
    if not src:
        return None
    os.makedirs(THUMB_DIR, exist_ok=True)
    dst = os.path.join(THUMB_DIR, token + ".jpg")
    if os.path.exists(dst) and os.path.getsize(dst) > 0 and os.path.getmtime(dst) >= os.path.getmtime(src):
        return dst
    try:
        from PIL import Image
        im = Image.open(src)
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
        im.save(dst, "JPEG", quality=82, optimize=True)
        return dst
    except Exception as e:  # noqa: BLE001
        _log(f"thumb failed {token}: {e}")
        return src


_FULL_DATE = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
_MD_DATE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")


def _label_date(label):
    """从标题里提取日期，返回 (y, m, d)，未知的年份/月份用 None。"""
    m = _FULL_DATE.search(label or "")
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = _MD_DATE.search(label or "")
    if m:
        return None, int(m.group(1)), int(m.group(2))
    return None


def _is_today(label, today):
    d = _label_date(label)
    if not d:
        return False
    y, m, dd = d
    if y is None:
        return (m, dd) == (today[1], today[2])
    return (y, m, dd) == today


def _group_blocks(blocks, now=None):
    lt = time.localtime(now) if now else time.localtime()
    today = (lt.tm_year, lt.tm_mon, lt.tm_mday)
    groups = []
    cur = {"label": "", "is_today": False, "items": []}
    for b in blocks:
        if _HEADING.match(b["tag"]):
            if cur["items"] or cur["label"]:
                groups.append(cur)
            label = b["text"]
            cur = {
                "label": label,
                "is_today": _is_today(label, today),
                "heading_id": b["id"],
                "heading_tag": b["tag"],
                "has_date": _label_date(label) is not None,
                "items": [],
            }
            continue
        cur["items"].append(b)
    if cur["items"] or cur["label"]:
        groups.append(cur)
    return groups


def _today_label(groups):
    """按文档已有写法生成今天的日期标题，例如 2026/11/4。"""
    lt = time.localtime()
    y, m, d = lt.tm_year, lt.tm_mon, lt.tm_mday
    sample = ""
    for g in groups:
        if g.get("has_date"):
            sample = g["label"]
            break
    if "/" in sample:
        return f"{y}/{m}/{d}" if sample.count("/") == 2 else f"{m}/{d}"
    if "年" in sample:
        return f"{y}年{m}月{d}日"
    if "-" in sample:
        return f"{y}-{m}-{d}"
    return f"{y}/{m}/{d}"


def _stats(groups):
    today_items = 0
    total = 0
    images = 0
    for g in groups:
        for it in g["items"]:
            if it.get("tag") == "img":
                images += 1
            else:
                total += 1
                if g["is_today"]:
                    today_items += 1
    return {"today": today_items, "total": total, "groups": len(groups), "images": images}


# ---------------------------------------------------------------- 对外接口

def fetch_doc(doc_url):
    res = run_lark([
        "docs", "+fetch", "--doc", doc_url,
        "--doc-format", "xml", "--detail", "with-ids",
        "--as", "user", "--format", "json",
    ])
    if not res.get("ok"):
        return {"ok": False, "error": _err(res)}
    doc = (res.get("data") or {}).get("document") or {}
    parsed = parse_doc(doc.get("content", ""))
    groups = _group_blocks(parsed["blocks"])
    model = {
        "ok": True,
        "doc_title": parsed["title"] or "工作日志",
        "doc_url": doc_url,
        "revision": doc.get("revision_id"),
        "synced_at": int(time.time()),
        "groups": groups,
        "stats": _stats(groups),
        "block_count": len(parsed["blocks"]),
    }
    save_cache(model)
    return model


def save_cache(model):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(model, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        _log(f"save_cache: {e}")


def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _esc(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _wrap(tag, text, done=None):
    if tag == "checkbox":
        d = "true" if done else "false"
        return f'<checkbox done="{d}">{_esc(text)}</checkbox>'
    if _HEADING.match(tag) or tag in ("p", "li", "blockquote"):
        return f"<{tag}>{_esc(text)}</{tag}>"
    return f"<p>{_esc(text)}</p>"


def update_block(doc_url, model, block_id, new_text, done=None):
    """更新一条内容。优先 str_replace（保留行内样式），失败或不唯一时退回 block_replace。"""
    target = None
    for g in model.get("groups", []):
        for it in g.get("items", []):
            if it.get("id") == block_id:
                target = it
                break
    if not target or not block_id:
        return {"ok": False, "error": "未找到该条目（文档可能已被修改，请刷新）"}

    old = target["text"]
    tag = target["tag"]
    same = sum(1 for g in model["groups"] for it in g["items"] if it["text"] == old)
    changed_attr = done is not None and tag == "checkbox"

    if not changed_attr and old != new_text and same == 1:
        res = run_lark([
            "docs", "+update", "--doc", doc_url, "--command", "str_replace",
            "--pattern", old, "--content", new_text, "--as", "user", "--format", "json",
        ])
        if res.get("ok"):
            return {"ok": True}

    res = run_lark([
        "docs", "+update", "--doc", doc_url, "--command", "block_replace",
        "--block-id", block_id, "--content", _wrap(tag, new_text, done),
        "--as", "user", "--format", "json",
    ])
    if res.get("ok"):
        return {"ok": True}
    return {"ok": False, "error": _err(res)}


def _update(doc_url, *args):
    return run_lark(["docs", "+update", "--doc", doc_url, *args, "--as", "user", "--format", "json"])


def add_entry(doc_url, model, text):
    """把一条记录插到「今天」的位置：
    1) 今天已有分组 → 追加到该组末尾；
    2) 没有今天分组 → 在最近日期分组之前新建一个今天的标题再写内容（日志是最新的在上）；
    3) 都没有 → 追加到文末。
    """
    groups = model.get("groups", [])

    # 1) 今天的分组已存在
    for g in groups:
        texts = [it for it in g["items"] if it.get("tag") != "img" and it.get("id")]
        if g.get("is_today") and texts:
            last = texts[-1]
            res = _update(doc_url, "--command", "block_insert_after", "--block-id", last["id"],
                          "--content", _wrap(last["tag"], text))
            if res.get("ok"):
                return {"ok": True, "where": "today"}
            break

    # 2) 在最近日期分组前插入今天的小节
    htag = "h2"
    anchor = None
    for idx, g in enumerate(groups):
        if g.get("has_date"):
            htag = g.get("heading_tag") or "h2"
            prev = [it for it in (groups[idx - 1]["items"] if idx > 0 else []) if it.get("tag") != "img"]
            if prev:
                anchor = prev[-1]["id"]
            elif idx > 0 and groups[idx - 1].get("heading_id"):
                anchor = groups[idx - 1]["heading_id"]
            break
    if anchor:
        content = (f"<{htag}>{_esc(_today_label(groups))}</{htag}>"
                   f"<ol><li>{_esc(text)}</li></ol>")
        res = _update(doc_url, "--command", "block_insert_after", "--block-id", anchor, "--content", content)
        if res.get("ok"):
            return {"ok": True, "where": "before_latest"}

    # 3) 兜底：文末追加
    res = _update(doc_url, "--command", "append",
                  "--content", f"<p>{_esc(text)}</p>")
    if res.get("ok"):
        return {"ok": True, "where": "end"}
    return {"ok": False, "error": _err(res)}
