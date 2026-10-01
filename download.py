#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""download.py —— 开放获取(OA) PDF 的下载、重命名与归档

从 paper_radar.py 拆出（2026-10-01，逐字搬运，未改逻辑）。

实测自动下载成功率约 30%：Copernicus / Frontiers 可下，Wiley / MDPI 硬 403。
下不到的进「待下载清单」，由 manual_ingest.py 处理你手动下载的 PDF。
"""
import os
import re
import sys
import urllib.parse
from datetime import datetime

import requests

from config import *


def _grab(url, hdr):
    """取一个 URL 的内容，带体积上限。返回 (blob, content_type, status, truncated)。"""
    cap = PDF_MAX_MB * 1024 * 1024
    r = requests.get(url, headers=hdr, timeout=REQUEST_TIMEOUT,
                     stream=True, allow_redirects=True)
    if r.status_code != 200:
        return b"", (r.headers.get("Content-Type") or "").lower(), r.status_code, False
    chunks, size, trunc = [], 0, False
    for ch in r.iter_content(65536):
        if not ch:
            continue
        chunks.append(ch)
        size += len(ch)
        if size > cap:
            trunc = True
            break
    return (b"".join(chunks), (r.headers.get("Content-Type") or "").lower(),
            200, trunc)


# 出版商在落地页里官方声明 PDF 直链的标准写法
_META_PDF_PATTERNS = [
    r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
    r'<link[^>]+type=["\']application/pdf["\'][^>]+href=["\']([^"\']+)["\']',
]


def _find_pdf_url(html_bytes, base_url):
    """从落地页 HTML 里找出出版商声明的 PDF 直链。找不到返回 None。"""
    if not html_bytes:
        return None
    html = html_bytes[:500000].decode("utf-8", "ignore")
    for pat in _META_PDF_PATTERNS:
        m = re.search(pat, html, re.I)
        if m:
            u = urllib.parse.urljoin(base_url, m.group(1).strip())
            if u.startswith("http"):
                return u
    return None


def download_oa_pdf(paper):
    """把开放获取(OA)论文的 PDF 下到 PDF_Inbox。

    为什么需要：EndNote 的「PDF 自动导入文件夹」只认 PDF、不认 .ris，
    所以要让整条流程"无需人工介入"，必须把 OA 论文的 PDF 也抓下来。
    下到的 PDF 同时也能直接喂给 pdf2md 做精读，一举两得。

    设计原则：**绝不因下载失败中断主流程**——一律吞异常，只记一行。
    幂等：同名文件已存在则跳过。
    """
    if not PDF_DOWNLOAD_ENABLED:
        return None
    url = (paper.get("oa_pdf_url") or "").strip()
    if not url:
        return None
    os.makedirs(PDF_INBOX_DIR, exist_ok=True)
    try:
        title = paper.get("title", "Untitled")
        score = paper.get("total_score", 0)
        ds = paper.get("data_source", "RSS")
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"{date_str}_{ds[:4]}_{score}分_{safe_filename(title, 40)}.pdf"
        filepath = os.path.join(PDF_INBOX_DIR, filename)
        if os.path.exists(filepath):          # 幂等
            return filepath

        # ★ 带上 Referer：不少出版商（MDPI / ScienceDirect 等）会校验来源页
        hdr = get_chrome_headers()
        ref = (paper.get("link") or "").strip()
        if ref.startswith("http"):
            hdr["Referer"] = ref
        hdr["Accept"] = "application/pdf,text/html,*/*;q=0.8"

        blob, ctype, status, trunc = _grab(url, hdr)
        if status != 200:
            print(f"     ⚠ PDF HTTP {status}：{title[:40]}")
            return None
        if trunc:
            print(f"     ⚠ PDF 超过 {PDF_MAX_MB} MB，放弃：{title[:40]}")
            return None

        # ★ 2026-10-01：拿到 HTML 说明这是【落地页】而不是 PDF。
        #   出版商普遍用 <meta name="citation_pdf_url"> 官方声明 PDF 直链
        #   （Springer / AGU / Wiley 等都遵守），顺着它再取一次。
        if not blob.startswith(b"%PDF"):
            real = _find_pdf_url(blob, url)
            if real:
                b2, c2, st2, tr2 = _grab(real, hdr)
                if st2 == 200 and not tr2 and b2.startswith(b"%PDF"):
                    blob, ctype = b2, c2
        if not blob.startswith(b"%PDF"):
            print(f"     ⚠ 不是 PDF（{ctype or '未知类型'}）：{title[:40]}")
            return None
        if len(blob) < PDF_MIN_BYTES:
            print(f"     ⚠ PDF 过小（{len(blob)} B），丢弃：{title[:40]}")
            return None

        with open(filepath, "wb") as f:
            f.write(blob)
        return filepath
    except Exception as e:
        print(f"     ⚠ PDF 下载失败（{type(e).__name__}）：{str(e)[:60]}")
        return None


def download_all_oa_pdfs(papers):
    """下载 OA PDF → 重命名为中文名 → 归入 Library\\<主题>\\ + 复制到 PDF_Inbox。

    ★ 2026-10-01：由"只下载"升级为"下载+重命名+归档"一条龙。
    返回归档后的文件路径列表。任何一步失败都只跳过该篇，不中断整批。
    """
    if not PDF_DOWNLOAD_ENABLED:
        print("\n  [PDF] 自动下载已关闭（PDF_DOWNLOAD_ENABLED=False）")
        return [], set()
    oa_papers = [p for p in papers if p.get("oa_pdf_url")]
    print(f"\n{'=' * 60}")
    print(f"【OA PDF 下载 → 重命名 → 归档】{len(oa_papers)}/{len(papers)} 篇有 OA 链接"
          f"（本轮上限 {PDF_MAX_PER_RUN}）")
    print("=" * 60)
    if not oa_papers:
        print("  本轮无 OA 链接可直接下载（其余进待下载清单，等手动下载）")
        return [], set()

    batch = oa_papers[:PDF_MAX_PER_RUN]
    # 先把 PDF 都下下来（不依赖本地模型）
    downloaded = []
    for p in batch:
        fp = download_oa_pdf(p)
        if fp:
            downloaded.append((p, fp))
            print(f"  ⬇ {os.path.basename(fp)[:70]}")
    print(f"  [下载] 成功 {len(downloaded)}/{len(batch)} 篇")

    if not downloaded:
        return [], set()

    # 再统一重命名 + 归档（本地模型只起停一次）
    lm = None
    ai = rn = None
    if AUTO_FILE_TO_LIBRARY or AUTO_RENAME_PDF:
        try:
            import sys as _sys
            if BASE_DIR not in _sys.path:
                _sys.path.insert(0, BASE_DIR)
            import library_manager as lm
            lm.ensure_dirs()
            if AUTO_RENAME_PDF:
                ai = lm._load_module(lm.ASK_IMAGE, "ask_image")
                rn = lm._load_module(lm.RENAMER, "rename_pdfs_ai")
                if not ai.health():
                    if not ai.start_server():
                        print("  [警告] 本地模型起不来，本轮跳过重命名（PDF 仍会归档）")
                        ai = rn = None
        except Exception as e:
            print(f"  [警告] 归档模块加载失败：{e}")
            lm = None
            ai = rn = None

    filed, filed_keys = [], set()
    try:
        for p, fp in downloaded:
            if lm is None:
                filed.append(fp)
                continue
            dst, cat = lm.file_paper(
                fp, p, ai=ai, rn=rn,
                do_rename=(AUTO_RENAME_PDF and ai is not None))
            if dst:
                filed.append(dst)
                try:
                    filed_keys.add(lm.key_of(p))
                except Exception:
                    pass
                print(f"  📁 [{cat}] {os.path.basename(dst)[:66]}")
    finally:
        if ai is not None:
            try:
                ai.stop_server()
            except Exception:
                pass

    # ★ 2026-10-01：把已归档的登记为 filed（状态比 seen 更进一步）
    try:
        import processed as _proc
        for p, _fp in downloaded:
            if _proc.key_of(p) in filed_keys:
                _proc.mark(p, "filed")
        _proc._save()
    except Exception as e:
        print(f"  [警告] 归档登记失败：{e}")

    print(f"  [汇总] 已归档 {len(filed)} 篇到 {lm.LIBRARY_DIR if lm else PDF_INBOX_DIR}")
    return filed, filed_keys
