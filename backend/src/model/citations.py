"""将服务端引用投影为普通 Markdown；原始 provider 文本仍原样用于续接。"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def render_url_citations(content: str, citations: list[dict]) -> str:
    links: dict[int, list[str]] = {}
    for citation in citations:
        url = str(citation.get("url") or "")
        if urlsplit(url).scheme not in {"http", "https"}:
            continue
        end = citation.get("end_index")
        if not isinstance(end, int) or not 0 <= end <= len(content):
            continue
        # 角括号链接保留 URL 中的括号；只转义会打断 Markdown 地址的字符。
        target = url.replace("<", "%3C").replace(">", "%3E").replace("\n", "%0A").replace("\r", "%0D")
        title = str(citation.get("title") or "来源").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")
        number = sum(len(group) for group in links.values()) + 1
        links.setdefault(end, []).append(f'[{number}](<{target}> "{title}")')
    for end, values in sorted(links.items(), reverse=True):
        content = content[:end] + "".join(values) + content[end:]
    # 原生引用标记不能直接由 react-markdown 解析，链接已由上面的 URL 注解补齐。
    return re.sub(r"cite[^]*", "", content) if links else content
