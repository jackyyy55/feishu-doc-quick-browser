# -*- coding: utf-8 -*-
"""调试用：拉取文档原始 XML 并打印解析结果。
用法： python probe.py <文档链接>
"""
import json
import os
import sys

import bridge

APP_DIR = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(APP_DIR, "data", "raw.xml")


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    if not url:
        print("用法: python probe.py <飞书文档链接>")
        return 1
    res = bridge.run_lark([
        "docs", "+fetch", "--doc", url,
        "--doc-format", "xml", "--detail", "with-ids",
        "--as", "user", "--format", "json",
    ])
    if not res.get("ok"):
        print("FAIL:", json.dumps(res, ensure_ascii=False)[:1200])
        return 1
    doc = (res.get("data") or {}).get("document") or {}
    content = doc.get("content", "")
    with open(RAW, "w", encoding="utf-8") as f:
        f.write(content)
    parsed = bridge.parse_doc(content)
    groups = bridge._group_blocks(parsed["blocks"])
    print("title:", parsed["title"])
    print("blocks:", len(parsed["blocks"]), "| raw chars:", len(content))
    print("--- 前 12 行原始 XML ---")
    print("\n".join(content.splitlines()[:12])[:1500])
    print("--- 解析结果 ---")
    for b in parsed["blocks"][:15]:
        print(f"  [{b['tag']}] id={b['id']} done={b['done']} :: {b['text'][:60]}")
    print("--- 分组 ---")
    for g in groups[:8]:
        print(f"  {g['label'] or '(无标题)'} today={g['is_today']} items={len(g['items'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
