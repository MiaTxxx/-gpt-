# -*- coding: utf-8 -*-
"""修复前端打包产物里 baseURL='/api' 与路径 '/api/...' 重复导致的 /api/api 问题。

策略（只改前端构建产物，不动源码）：
  1. 把 axios.create 里的 baseURL:`/api` 改成 baseURL:``（空串） →
     这样 axios 不再加前缀，所有 `/api/...` 调用直接命中后端正确的路径。
  2. 顺手把已经写死的 `/api/api/...` 修正为 `/api/...`（双斜杠也修一下）。

只在内容真的发生改变时改写文件，避免破坏 hash。
"""
import re
import shutil
from pathlib import Path

ASSETS = Path(__file__).parent / "前端" / "assets"

# 1) baseURL:`/api`  →  baseURL:``
RE_BASEURL = re.compile(r"baseURL:`/api`")

# 2) `/api/api/...`  →  `/api/...`（出现在 LoginPage / RegisterPage / DashboardPage 里）
RE_DOUBLE  = re.compile(r"/api/api/")

changed = []
for js in sorted(ASSETS.glob("*.js")):
    txt = js.read_text(encoding="utf-8")
    new = RE_BASEURL.sub("baseURL:``", txt)
    new = RE_DOUBLE.sub("/api/", new)
    if new != txt:
        bak = js.with_suffix(js.suffix + ".bak")
        if not bak.exists():
            shutil.copy2(js, bak)
        js.write_text(new, encoding="utf-8")
        changed.append(js.name)

print("patched files:")
for n in changed:
    print(" -", n)
print("done, total:", len(changed))
