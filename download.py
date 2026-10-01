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
import shutil
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime

import requests

from config import *


def _grab(url, hdr, sink=None):
    """取一个 URL 的内容，带体积上限。

    ★ 2026-10-01 三处修正：
      1. 用 `with requests.get(..., stream=True) as r:` —— 原来 stream=True 却
         没有关连接，截断（break）或异常退出时套接字会一直挂着不释放。
      2. 传了 sink（一个已打开的二进制文件）就**边下边写**，不再
         `chunks.append` 再 `b"".join()`。
         ⚠️ 提醒：**当前主路径并没有传 sink**（它需要先拿到完整响应才能
         判断"这是 HTML 落地页还是 PDF"）。所以内存里仍会驻留一份完整 PDF。
         对 60 MB 上限、每天几十篇来说可以接受，但别以为已经流式化了。
         （真要流式：# 先按 Content-Type 判，是 PDF 就开 sink 直接落盘。）
      3. 瞬时失败**重试**：429 / 5xx / 连接层异常才重试，
         403 / 404 立即放弃（出版商明确拒绝，重试没意义）。

    返回 `(blob, content_type, status, truncated, complete)`。
      truncated —— 超过 PDF_MAX_MB 被截断
      complete  —— 与服务器声明的 Content-Length 是否吻合（没声明则为 True）
    传了 sink 时 blob 为 b""（内容已落盘）。
    """
    cap = PDF_MAX_MB * 1024 * 1024
    last = (b"", "", 0, False, False)
    for attempt in range(PDF_RETRIES + 1):
        chunks, size, trunc, wrote = [], 0, False, False
        try:
            with requests.get(url, headers=hdr, timeout=REQUEST_TIMEOUT,
                              stream=True, allow_redirects=True) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                if r.status_code != 200:
                    # ★ 2026-10-01【区分"暂时失败"和"根本拿不到"】。
                    #   403 / 404 是出版商在明确拒绝（Wiley / MDPI 的硬拦），
                    #   重试只是浪费时间；429 和 5xx 才是"等会儿再来"。
                    want_retry = r.status_code == 429 or 500 <= r.status_code < 600
                    if want_retry and attempt < PDF_RETRIES:
                        _wait_retry(r, attempt)
                        continue
                    last = (b"", ctype, r.status_code, False, False)
                    return last
                # ★ 完整性判据：服务器声明了 Content-Length 就必须长度吻合。
                #   半截 PDF 比下载失败更坏 —— 失败你知道没成，半截文件会被
                #   当成成功存进库里（而且有些阅读器前几页还能正常打开）。
                try:
                    want_len = int(r.headers.get("Content-Length") or -1)
                except (TypeError, ValueError):
                    want_len = -1
                for ch in r.iter_content(65536):
                    if not ch:
                        continue
                    size += len(ch)
                    if size > cap:
                        trunc = True
                        break
                    if sink is not None:
                        sink.write(ch)
                        wrote = True
                    else:
                        chunks.append(ch)
                complete = (trunc or want_len < 0 or size == want_len)
            return (b"".join(chunks), ctype, 200, trunc, complete)
        except requests.RequestException as e:
            # 连接层失败（超时 / 重置 / DNS）—— 只有"还没往 sink 写过东西"
            # 才敢重试，否则会在同一个文件里叠两份内容。
            if attempt < PDF_RETRIES and not wrote:
                print(f"     ↻ 下载失败（{type(e).__name__}），"
                      f"{PDF_RETRY_WAIT * (attempt + 1)}s 后重试")
                time.sleep(PDF_RETRY_WAIT * (attempt + 1))
                continue
            last = (b"", "", 0, False, False)
            raise
    return last


def _wait_retry(resp, attempt):
    """按 Retry-After 或指数退避等待。"""
    wait = PDF_RETRY_WAIT * (attempt + 1)
    try:
        ra = resp.headers.get("Retry-After")
        if ra:
            wait = min(60, max(wait, int(float(ra))))
    except (TypeError, ValueError):
        pass
    print(f"     ↻ HTTP {resp.status_code}，{wait}s 后重试")
    time.sleep(wait)


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


def _is_pdf(blob):
    """判断一段字节是不是 PDF —— **全文件唯一的一把尺子**。

    ★ 2026-10-01：原来有两套标准：第一次用宽松魔数（`b"%PDF" in blob[:1024]`，
      容忍 BOM / 前导空白），第二次却用严格的 `startswith(b"%PDF")`。
      同一个程序里两种判定会给出不同答案。
    """
    if not blob:
        return False
    head = blob[:1024].lstrip(b"\xef\xbb\xbf \t\r\n")   # 去 BOM 与前导空白
    return head.startswith(b"%PDF-")


def _proc_key(paper):
    """与去重表 / 待下载清单 / 销账用的是同一套身份（processed.key_of）。"""
    try:
        import processed as _proc
        return _proc.key_of(paper) or ""
    except Exception:
        return ""


def _paper_key(paper):
    """论文的稳定身份（12 位十六进制），用于暂存区文件名与幂等复用。

    ★ 2026-10-01：优先用 `processed.key_of`（规范化 DOI 优先、其次规范化标题）
      —— 与去重表、待下载清单、销账用的是**同一套身份**，不会各算各的。
      取不到才退回 URL 的哈希（同一篇论文换个 CDN 就会变，是最后的手段）。
    """
    k = _proc_key(paper)
    if not k or k == "title:":
        u = (paper.get("oa_pdf_url") or "").strip()
        k = ("url:" + u) if u else ("ttl:" + (paper.get("title") or ""))
    return hashlib.sha256(k.encode("utf-8")).hexdigest()[:12]


def _sweep_stage():
    """清掉暂存区里超过 PDF_STAGE_KEEP_DAYS 天的残留（归档反复失败的那些）。"""
    try:
        if not os.path.isdir(PDF_STAGE_DIR):
            return 0
        cutoff = time.time() - PDF_STAGE_KEEP_DAYS * 86400
        n = 0
        for fn in os.listdir(PDF_STAGE_DIR):
            p = os.path.join(PDF_STAGE_DIR, fn)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    n += 1
            except Exception:
                pass
        return n
    except Exception:
        return 0


def download_oa_pdf(paper, dest_dir=None):
    """把开放获取(OA)论文的 PDF 下到【暂存区】（默认），不是 PDF_Inbox。

    为什么需要：EndNote 的「PDF 自动导入文件夹」只认 PDF、不认 .ris，
    所以要让整条流程"无需人工介入"，必须把 OA 论文的 PDF 也抓下来。
    下到的 PDF 同时也能直接喂给 pdf2md 做精读，一举两得。

    ★ 2026-10-01：默认落到 `PDF_STAGE_DIR` 而不是 `PDF_INBOX_DIR`。
      原来直接下进 PDF_Inbox，而下载是【批处理】的（一次 20 篇）、
      重命名却要一篇篇走视觉模型（5–15 s/篇）⇒ 第一个文件要在
      EndNote 监听目录里躺 2–5 分钟。EndNote 抢先读走就加排他锁，
      `shutil.move` 抛 PermissionError ⇒ **该篇的中文重命名与归档全丢**。
      仅在【不归档】时才直接下进 PDF_Inbox —— 那是它唯一的出路。

    设计原则：**绝不因下载失败中断主流程**——一律吞异常，只记一行。
    幂等：**同一篇论文**（按 paper_key）已下过就直接复用，不再重新下载。
    """
    if not PDF_DOWNLOAD_ENABLED:
        return None
    url = (paper.get("oa_pdf_url") or "").strip()
    if not url:
        return None
    if dest_dir is None:
        dest_dir = PDF_INBOX_DIR if not AUTO_FILE_TO_LIBRARY else PDF_STAGE_DIR

    # ★ 2026-10-01【幂等性大修】：文件名里的身份**只能来自论文本身**。
    #   原来叫 `{日期}_{来源}_{分数}分_{标题}_{URL哈希6}.pdf` —— 名字被三个
    #   **运行环境量**污染（日期每天变、分数重打会漂、URL 会换 CDN/签名），
    #   于是那句 `if os.path.exists(filepath): return filepath` 永远命不中。
    #   实测同一篇论文连下三次 → 暂存区躺了三个文件、下载也做了三遍。
    #   这直接让 failed/deferred 那套"暂存区留档、次日复用"的重试设计失效。
    #   ⇒ 改为 `{paper_key 前12位}_{标题}.pdf`：
    #       · paper_key 优先取规范化 DOI（与 processed.key_of 同一套规则），
    #         没有 DOI 才退回规范化标题 —— 都是论文身份，跟环境无关
    #       · 日期/分数/来源不进文件名（它们属于 CSV / 日志，不属于身份）
    #       · 复用检查按【key 前缀】扫目录，不要求标题完全一致
    #         （同一 DOI 在不同源里标题可能有细微出入）
    key = _paper_key(paper)
    _prefix = key + "_"

    try:
        os.makedirs(dest_dir, exist_ok=True)
        try:                                   # ★ 复用检查：按 key 前缀扫
            for _f in os.listdir(dest_dir):
                if _f.startswith(_prefix) and _f.lower().endswith(".pdf"):
                    return os.path.join(dest_dir, _f)
        except OSError:
            pass

        title = paper.get("title", "Untitled")
        filename = f"{key}_{safe_filename(title, 34)}.pdf"
        filepath = os.path.join(dest_dir, filename)
        if os.path.exists(filepath):          # 幂等（同 key 同标题）
            return filepath

        # ★ 带上 Referer：不少出版商（MDPI / ScienceDirect 等）会校验来源页
        hdr = get_chrome_headers()
        ref = (paper.get("link") or "").strip()
        if ref.startswith("http"):
            hdr["Referer"] = ref
        hdr["Accept"] = "application/pdf,text/html,*/*;q=0.8"

        blob, ctype, status, trunc, complete = _grab(url, hdr)
        if status != 200:
            print(f"     ⚠ PDF HTTP {status}：{title[:40]}")
            return None
        if trunc:
            print(f"     ⚠ PDF 超过 {PDF_MAX_MB} MB，放弃：{title[:40]}")
            return None

        # ★ 2026-10-01：拿到 HTML 说明这是【落地页】而不是 PDF。
        #   出版商普遍用 <meta name="citation_pdf_url"> 官方声明 PDF 直链
        #   （Springer / AGU / Wiley 等都遵守），顺着它再取一次。
        if not _is_pdf(blob):
            real = _find_pdf_url(blob, url)
            if real:
                # ★ 2026-10-01：从落地页二次抓取时，Referer 要换成【落地页】。
                #   原来一直带着原文的 doi.org 链接，出版商防盗链会判 403。
                hdr2 = dict(hdr)
                hdr2["Referer"] = url
                b2, c2, st2, tr2, comp2 = _grab(real, hdr2)
                # ★ 两次判定必须同一把尺子。原来这里是 `b2.startswith(b"%PDF")`
                #   （严格，第 1 字节就得是 %），而第一次用的是宽松魔数
                #   （允许 BOM / 前导空白）—— 同一个程序两套标准。
                if st2 == 200 and not tr2 and _is_pdf(b2):
                    blob, ctype, complete = b2, c2, comp2
        if not _is_pdf(blob):
            print(f"     ⚠ 不是 PDF（{ctype or '未知类型'}）：{title[:40]}")
            return None
        if len(blob) < PDF_MIN_BYTES:
            print(f"     ⚠ PDF 过小（{len(blob)} B），丢弃：{title[:40]}")
            return None
        # ★ 2026-10-01【完整性】：服务器声明了 Content-Length 就必须长度吻合。
        #   半截 PDF 比下载失败更坏 —— 失败你知道没成，半截会被当成成功
        #   存进 Library，而且有些阅读器前几页还能正常打开，更难发现。
        #   这里不删暂存文件（留着，下一轮按 key 幂等复用它重试）。
        if not complete:
            print(f"     ⚠ PDF 长度与 Content-Length 不符（半截文件），"
                  f"本轮丢弃：{title[:40]}")
            return None

        # ★ 2026-10-01：改为【临时文件 + os.replace 原地重命名】。
        #   目标目录可能被 EndNote 实时监视（不归档时就是 PDF_Inbox）——
        #   直接写目标路径的话，写入的那几百毫秒里 EndNote 可能抢读到
        #   半截文件；进程中途被杀也会留下损坏的 PDF。
        #   （processed.py / save_history 早就是原子写，这里补齐。）
        fd, tmp = tempfile.mkstemp(suffix=".downloading", dir=dest_dir)
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
    返回 (已归档路径列表, 已归档的键集合, 不要列进手动下载清单的键集合)。
    任何一步失败都只跳过该篇，不中断整批。

    ⚠️ 第三个返回值（`deferred_keys`）的含义在 2026-10-01 扩了：
       原来只是"有 OA 直链但排在 PDF_MAX_PER_RUN 之后 ⇒ 下轮自动补下"；
       现在还包含"本轮下到了但归档失败 ⇒ 已放进 PDF_Inbox 保底，
       并登记 failed 待次日重试"。两类都**不该再进手动下载清单**。
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
    # ★ 2026-10-01：记下【因为限额而没下】的那些，并**登记成可重试的 "deferred"**。
    #   ⚠️ 踩过的坑：原来这里只是把它们收进 deferred_keys 供【本轮】排除，
    #      指望"下轮自动补下"——但根本没有任何机制把它们带回下一轮，
    #      而且 paper_radar 早在下载之前就把所有打过分的一律标成了 "seen"，
    #      filter_new 次日直接拦掉 ⇒ 既不会被自动补下，又被 _settled 排除在
    #      待下载清单之外 —— **彻底掉进黑洞**。
    #      现在显式标 "deferred"，filter_new 见到它会放行并排到批次最前面。
    deferred_keys = set()
    deferred_papers = oa_papers[PDF_MAX_PER_RUN:]
    try:
        import processed as _proc
        for _p in deferred_papers:
            try:
                deferred_keys.add(_proc.key_of(_p))
                _proc.mark(_p, "deferred")
            except Exception:
                pass
        if deferred_papers:
            _proc._save()
    except Exception as e:
        print(f"  [警告] deferred 登记失败：{str(e)[:60]}")
    # 先把 PDF 都下到【暂存区】（不依赖本地模型，也避开 EndNote 的监听目录）
    _n_swept = _sweep_stage()
    if _n_swept:
        print(f"  [暂存区] 清掉 {_n_swept} 个超过 {PDF_STAGE_KEEP_DAYS} 天的残留")
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
    # ★★ 2026-10-01【服务器归属】★★
    #   谁起谁收。`_own_server` 只在"进来时服务【没在跑】、需要我们自己起"
    #   的情况下置位，finally 里据此决定收不收。
    #
    #   原来这里有个 `_server_up = [False]` —— **设了却从来没人读**，
    #   finally 判断的是 `ai is not None`，于是无条件 stop_server()：
    #   如果这个 llama-server 是你自己先起来干别的用的（health() 一进来
    #   就是 True，压根不走 start_server），跑完照样被杀。
    #   （local_scorer.py 上一轮已经改成"谁起谁收"，这里对齐。）
    #
    #   ⚠️ 置位时机很关键：必须在 `start_server()` **之前**。
    #      ask_image.start_server() 在**超时**时返回 False 却**不杀进程**，
    #      若等到返回 True 才置位，那次超时就会留下一个占着几 GB 显存的孤儿。
    _own_server = False
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
                if ai.health():
                    print("  [归档] 复用本机已在运行的本地模型（跑完【不会】关它）")
                else:
                    _own_server = True     # 先置位再启动：超时返回 False 也会留孤儿
                    if ai.start_server():
                        pass
                    else:
                        print("  [警告] 本地模型起不来，本轮跳过重命名（PDF 仍会归档）")
                        rn = None          # ai 保留，交给 finally 收尾（可能有余留进程）
        except Exception as e:
            print(f"  [警告] 归档模块加载失败：{e}")
            lm = None
            rn = None
            # 注意：**不把 ai 置 None** —— 万一服务器已经起来了，得让它被收掉

    filed, filed_keys, failed_p = [], set(), []
    failed_keys = set()
    _nolm_paths = set()      # 其中属于"归档模块压根没起来"的暂存路径
    _want_archive = bool(AUTO_FILE_TO_LIBRARY or AUTO_RENAME_PDF)
    try:
        for p, fp in downloaded:
            # ★ 2026-10-01【每篇独立兜底】。原来这一层没有 try ——
            #   模块声称"任何一步失败都只跳过该篇，不中断整批"，但那其实
            #   完全依赖 file_paper() 自己把异常吃干净。它一旦漏一个，
            #   第 2 篇抛了，第 3~20 篇就全都不执行（finally 只管收服务器）。
            try:
                if lm is None:
                    # 归档模块没起来（或功能本来就没开）
                    if _want_archive:
                        failed_p.append((p, fp))
                        _nolm_paths.add(fp)     # 只有这种才值得回填 Inbox
                    else:
                        # ★ 这时 PDF 已经直接下进 PDF_Inbox 了（就是最终归宿），
                        #   必须连 filed_keys 一起登记 —— 否则上游的
                        #   `_settled` 是空的，这篇**已经下好的 PDF 还会被列进
                        #   「待下载清单」**，让你去手动下一遍。
                        filed.append(fp)
                        try:
                            filed_keys.add(_proc_key(p))
                        except Exception:
                            pass
                    continue
                # ★ file_paper 返回三元组，第三个是"是否已成功投递 PDF_Inbox"。
                #   Library 归档成功 ≠ EndNote 收到了 —— 只有【两个目标都成功】
                #   才算 settled。否则这篇会被登记成 filed、次日既不重试也不进
                #   待下载清单，**PDF 永远进不了 EndNote**。
                #   ⚠️ 这里【故意不做 len>2 的兼容】—— 万一跑的是旧版
                #      library_manager（只返回二元组），让它当场
                #      ValueError 崩掉，好过静默地把它当成"归档成功"。
                dst, cat, inbox_ok = lm.file_paper(
                    fp, p, ai=ai, rn=rn,
                    do_rename=(AUTO_RENAME_PDF and ai is not None))
                if dst and inbox_ok:
                    filed.append(dst)
                    try:
                        filed_keys.add(_proc_key(p))
                    except Exception:
                        pass
                    print(f"  📁 [{cat}] {os.path.basename(dst)[:66]}")
                else:
                    failed_p.append((p, fp))
            except Exception as e:
                print(f"  [警告] 该篇归档异常，跳过（继续下一篇）："
                      f"{type(e).__name__}: {str(e)[:60]}")
                failed_p.append((p, fp))
    finally:
        # ★ 谁起谁收：只有本轮【自己启动的】才关掉。
        #   进来时就在跑的服务（可能是你为别的用途起的）跑完不动它。
        if _own_server and ai is not None:
            try:
                ai.stop_server()
            except Exception:
                pass

    # ★ 2026-10-01：【归档失败的不再静默沉没】。
    #   原来 file_paper 失败只打一行警告就完了，而 paper_radar 早把它标成
    #   "seen"、processed.filter_new 认的是"键在不在表里"（任何状态都拦）
    #   ⇒ 次日永久跳过、永不重试，中文重命名与归档对这篇彻底失效。
    #   现在两条补救：
    #     ① 把暂存文件直接放进 PDF_Inbox —— 至少让 EndNote 能收到（英文名）
    #     ② 登记 "failed"，filter_new 对 failed 放行 ⇒ 次日重试归档
    #   （暂存文件**留着不动**，次日 download_oa_pdf 会命中幂等直接返回它。）
    if failed_p:
        import processed as _proc
        _ok_to_inbox = 0
        for p, fp in failed_p:
            try:
                failed_keys.add(_proc.key_of(p))
            except Exception:
                pass
            # ★ 2026-10-01【回填只在"归档模块根本没起来"时才做】。
            #   原来不分青红皂白一律回填，而 file_paper 失败的两种情形下它
            #   要么没用要么有害：
            #     · "投递 PDF_Inbox 失败" → 回填是同一个操作，必然再失败
            #     · "归档失败"（已 return (None, ..., inbox_ok=True)）
            #        → Inbox【已经有一份了】，再拷一份就是重复投递，
            #          EndNote 会把同一篇论文导入两次（正是你要避免的）
            #   现在只有 lm is None（模块加载失败 ⇒ 压根没人投递过）才补这一枪。
            if fp in _nolm_paths:
                try:
                    if os.path.exists(fp):
                        atomic_copy_into(fp, PDF_INBOX_DIR)   # 仍是原子写
                        _ok_to_inbox += 1
                except Exception as e:
                    print(f"  [警告] 回填 PDF_Inbox 失败：{str(e)[:50]}")
            try:
                _proc.mark(p, "failed")
            except Exception:
                pass
        _tail = (f"（其中 {_ok_to_inbox} 篇因归档模块未启动、已直接放入 PDF_Inbox 保底）"
                 if _ok_to_inbox else "")
        print(f"  [归档失败] {len(failed_p)} 篇未归档{_tail}，"
              f"已登记 failed，次日自动重试")

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
    _deferred_n = len(deferred_keys)
    if _deferred_n:
        print(f"  [本轮限额] 另有 {_deferred_n} 篇有 OA 直链但排在 {PDF_MAX_PER_RUN} 之后，"
              f"下轮自动补下（不会进待下载清单）")
    # ★ 归档失败的也塞进 deferred_keys —— 它们同样"系统自己会处理"，
    #   不能再被 build_download_list 列进手动清单（否则用户会去重复下载
    #   一份已经在 PDF_Inbox 里的文件，而且 mark("listed") 会把 failed 顶掉、
    #   连重试机会都没了）。
    deferred_keys |= failed_keys
    return filed, filed_keys, deferred_keys
