#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""download.py —— 开放获取(OA) PDF 的下载、重命名与归档

从 paper_radar.py 拆出（2026-10-01，逐字搬运，未改逻辑）。

实测自动下载成功率约 30%：Copernicus / Frontiers 可下，Wiley / MDPI 硬 403。
下不到的进「待下载清单」，由 manual_ingest.py 处理你手动下载的 PDF。
"""
import hashlib
import html as _html
import os
import re
import sys
import tempfile
import urllib.parse
from datetime import datetime

import requests

from config import *


def _grab(url, hdr, sink=None):
    """取一个 URL 的内容，带体积上限。

    ★ 2026-10-01 两处修正：
      1. 用 `with requests.get(..., stream=True) as r:` —— 原来 stream=True 却
         没有关连接，截断（break）或异常退出时套接字会一直挂着不释放。
      2. 传了 sink（一个已打开的二进制文件）就**边下边写**，不再
         `chunks.append` 再 `b"".join()` —— 后者会把整个文件（上限 60 MB）
         堆在内存里再拷一遍。

    返回 (blob, content_type, status, truncated)。
    传了 sink 时 blob 为 b""（内容已落盘），用 file_size 取大小。
    """
    cap = PDF_MAX_MB * 1024 * 1024
    chunks, size, trunc = [], 0, False
    with requests.get(url, headers=hdr, timeout=REQUEST_TIMEOUT,
                      stream=True, allow_redirects=True) as r:
        if r.status_code != 200:
            return b"", (r.headers.get("Content-Type") or "").lower(), r.status_code, False
        for ch in r.iter_content(65536):
            if not ch:
                continue
            size += len(ch)
            if size > cap:
                trunc = True
                break
            if sink is not None:
                sink.write(ch)
            else:
                chunks.append(ch)
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
            # ★ 2026-10-01：出版商 meta 里的 URL 常写成 /dl?id=1&amp;type=pdf，
            #   不反转义就带着 &amp; 去请求 ⇒ 服务器当非法参数返 400/404。
            raw = _html.unescape(m.group(1).strip())
            u = urllib.parse.urljoin(base_url, raw)
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
        # ★ 2026-10-01：文件名加一段【URL 哈希尾缀】。
        #   原来只截标题前 40 字 —— 地学论文标题高度同质
        #   （"Numerical simulation of rainfall infiltration in…"），
        #   同分同源的两篇一旦前 40 字相同就会撞名，后一篇被
        #   `os.path.exists` 误判为"已下载"而直接跳过。
        _h = hashlib.md5(url.encode("utf-8")).hexdigest()[:6]
        filename = (f"{date_str}_{ds[:4]}_{score}分_"
                    f"{safe_filename(title, 34)}_{_h}.pdf")
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
        # ★ 2026-10-01：7 宽松魔数（BOM / 前导空白会让 startswith 误杀）
        if b"%PDF" not in blob[:1024]:
            real = _find_pdf_url(blob, url)
            if real:
                # ★ 2026-10-01：从落地页二次抓取时，Referer 要换成【落地页】。
                #   原来一直带着原文的 doi.org 链接，出版商防盗链会判 403。
                hdr2 = dict(hdr)
                hdr2["Referer"] = url
                b2, c2, st2, tr2 = _grab(real, hdr2)
                if st2 == 200 and not tr2 and b2.startswith(b"%PDF"):
                    blob, ctype = b2, c2
        if b"%PDF" not in blob[:1024]:
            print(f"     ⚠ 不是 PDF（{ctype or '未知类型'}）：{title[:40]}")
            return None
        if len(blob) < PDF_MIN_BYTES:
            print(f"     ⚠ PDF 过小（{len(blob)} B），丢弃：{title[:40]}")
            return None

        # ★ 2026-10-01：改为【临时文件 + os.replace 原地重命名】。
        #   PDF_INBOX_DIR 是 EndNote 实时监视的自动导入目录 ——
        #   直接写目标路径的话，写入的那几百毫秒里 EndNote 可能抢读到
        #   半截文件；进程中途被杀也会留下损坏的 PDF。
        #   （processed.py / save_history 早就是原子写，这里补齐。）
        fd, tmp = tempfile.mkstemp(suffix=".downloading", dir=PDF_INBOX_DIR)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, filepath)
        except Exception:
            try:
                os.path.exists(tmp) and os.remove(tmp)
            except Exception:
                pass
            raise
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
        return [], set(), set()
    oa_papers = [p for p in papers if p.get("oa_pdf_url")]
    print(f"\n{'=' * 60}")
    print(f"【OA PDF 下载 → 重命名 → 归档】{len(oa_papers)}/{len(papers)} 篇有 OA 链接"
          f"（本轮上限 {PDF_MAX_PER_RUN}）")
    print("=" * 60)
    if not oa_papers:
        print("  本轮无 OA 链接可直接下载（其余进待下载清单，等手动下载）")
        return [], set(), set()

    batch = oa_papers[:PDF_MAX_PER_RUN]
    # ★ 2026-10-01：记下【因为限额而没下】的那些。
    #   它们不能被打上 "listed" —— 否则 processed.filter_new 次日会永久拦掉它们，
    #   本来能自动下载的 OA 文献（仅因排在第 21 篇之后）被迫转手动，违背自动化初衷。
    deferred_keys = set()
    for _p in oa_papers[PDF_MAX_PER_RUN:]:
        try:
            deferred_keys.add(_p.get("doi", "") or (_p.get("title", "") or ""))
        except Exception:
            pass
    # 先把 PDF 都下下来（不依赖本地模型）
    downloaded = []
    for p in batch:
        fp = download_oa_pdf(p)
        if fp:
            downloaded.append((p, fp))
            print(f"  ⬇ {os.path.basename(fp)[:70]}")
    print(f"  [下载] 成功 {len(downloaded)}/{len(batch)} 篇")

    if not downloaded:
        return [], set(), set()

    # 再统一重命名 + 归档（本地模型只起停一次）
    lm = None
    ai = rn = None
    # ★ 2026-10-01：用【独立标志】记录服务器是否真的起来过。
    #   原来是失败时 `ai = rn = None` —— 这会让 finally 里的
    #   `if ai is not None: ai.stop_server()` 永远执行不到。
    #   ★ 真正可达的触发点：ask_image.start_server() 在**超时**时返回 False
    #     却【不杀进程】，此时把它置 None ⇒ 孤立的 llama-server
    #     一直占着几 GB 显存，后续任务全部 OOM。
    _server_up = [False]
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
                    if ai.start_server():
                        _server_up[0] = True
                    else:
                        print("  [警告] 本地模型起不来，本轮跳过重命名（PDF 仍会归档）")
                        rn = None          # ai 保留，交给 finally 收尾（可能有余留进程）
        except Exception as e:
            print(f"  [警告] 归档模块加载失败：{e}")
            lm = None
            rn = None
            # 注意：**不把 ai 置 None** —— 万一服务器已经起来了，得让它被收掉

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
        # 只要服务器起来过（或可能起过），就一定要收 —— 按标志判断，不看 ai 是不是 None
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
    if deferred_keys:
        print(f"  [本轮限额] 另有 {len(deferred_keys)} 篇有 OA 直链但排在 {PDF_MAX_PER_RUN} 之后，"
              f"下轮自动补下（不会进待下载清单）")
    return filed, filed_keys, deferred_keys
