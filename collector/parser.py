from __future__ import annotations
from pathlib import Path
from bs4 import BeautifulSoup


def _extract_html_blob(data: bytes) -> bytes | None:
    # 裁判文书网当前下载的 .doc 是 CFB 容器，但 HTML 正文以连续字节保存在文件内。
    # 直接扫描原始文件可避免依赖 Word/LibreOffice，并已用真实 HAR 下载样本验证。
    low = data.lower()
    starts = [x for x in (low.find(b"<html"), low.find(b"<!doctype")) if x >= 0]
    if not starts:
        return None
    start = min(starts)
    end = low.rfind(b"</html>")
    if end >= 0:
        end += len(b"</html>")
    else:
        end = len(data)
    return data[start:end]


def parse_official_doc(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    html_blob = _extract_html_blob(raw)
    if not html_blob:
        raise ValueError("未在官方 .doc 中找到内嵌 HTML；保留原件并标记解析失败")
    html = html_blob.decode("gb18030", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    return html, text
