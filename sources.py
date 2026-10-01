#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
sources.py —— 统一的文献检索源（2026-10-01 新增）

为什么要它
----------
原来的检索只有「4 个 RSS + OpenAlex 6 组查询」。实测：
  · OpenAlex 单查询总量 8,957，摘要率 90%   ← 已经是主力
  · **Crossref 单查询总量 55,505，其中 91% 是 OpenAlex 没有的**  ← 最大增量
  · 2 个 RSS 源是死的（Springer 返回 HTML、Wiley 404）
  · OpenAlex 会 429 限流，静默吃掉一组查询 ⇒ 覆盖量悄悄缩水

本模块提供：
  1. `polite_get()`   —— 统一退避重试：指数退避 + 读 Retry-After + 礼貌间隔
  2. `fetch_crossref()` / `fetch_arxiv()` / `fetch_semanticscholar()`
  3. `fetch_crossref_journals()` —— 用 issn 过滤代替死掉的 RSS（拿期刊目录）
  4. `enrich_abstracts()` —— 某篇没摘要时，拿 DOI 去 Crossref 补
                            （闭源论文的摘要覆盖只有约 24%，这里能补一部分）
  5. `dedupe_by_title()` —— 跨源合并去重

输出的字段与 `paper_radar.OpenAlexFetcher._parse_work` **完全一致**，
所以能直接混进现有候选池。

★ 关于"限流"：这不是"模拟人"，是 **API 礼节**。OpenAlex / Crossref 都提供
  `mailto` 参数进"礼貌池"（配额更宽），Semantic Scholar 官方建议 ≤1 req/s。
  照做既能拿到更全的结果，也不会给别人添麻烦。
"""
import html as _html
import io
import json
import os
import re
import sys
import time
import urllib.parse

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MAILTO = "781005412@qq.com"
UA = ("GeoPaperRadar/3.1 (academic literature radar; "
      "mailto:%s)" % MAILTO)
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

# ★★★ 2026-10-01：OpenAlex 从 2026-02-13 起【强制要求 API key】，
# `mailto` 礼貌池已废弃且被忽略。匿名访问只有 $0.10/天（同 IP 所有人共享），
# 实测已被耗尽 —— 6 组查询全返回 429，OpenAlex 覆盖【全丢】。
# 免费 key 给 $1/天（10 万 credits），我们每次跑约 150 credits，占用 0.3%。
#   申请：https://openalex.org 注册 → https://openalex.org/settings/api 复制
#   填法：.env 里加一行  OPENALEX_API_KEY=<你的key>
OPENALEX_KEY = os.getenv("OPENALEX_API_KEY", "").strip()


def _auth_headers(url):
    """OpenAlex 需要 Authorization 头；其它源不需要。"""
    if OPENALEX_KEY and "api.openalex.org" in url:
        return {"Authorization": "Bearer " + OPENALEX_KEY}
    return {}

# 与 paper_radar.CHINESE_JOURNALS_ISSN 保持一致
CHINESE_ISSN = {"1000-6915", "1000-4548", "1000-2383"}

# 死掉的 RSS 源不再重试；改用 issn 走 API 拿
DEAD_RSS = {
    "https://link.springer.com/search.rss?facet-journal-id=10346&channel-name=Landslides",
    "https://agupubs.onlinelibrary.wiley.com/action/showFeed?jc=1944-7973&type=etoc&feed=rss",
}

# 期刊 ISSN（替代死掉的 RSS）
JOURNAL_ISSN = {
    "Landslides": "1612-510X",
    "Water Resources Research": "0043-1397",
}

_session = requests.Session()
_session.headers.update(HEADERS)
_last_call = [0.0]


# ══════════════════════════════════════════════
# 1. 统一的礼貌请求
# ══════════════════════════════════════════════
def polite_get(url, params=None, timeout=45, retries=4, base_delay=1.5,
               min_gap=1.0, expect_json=True):
    """带指数退避的 GET。

    · 429 / 500 / 502 / 503 / 504 → 退避重试，优先遵守 Retry-After 头
    · 每次调用之间至少隔 min_gap 秒（默认 1 s，Semantic Scholar 的官方建议）
    · 返回解析后的 JSON，失败返回 None（调用方自己决定怎么办，不抛异常打断整批）
    """
    gap = time.time() - _last_call[0]
    if gap < min_gap:
        time.sleep(min_gap - gap)
    delay = base_delay
    for attempt in range(1, retries + 1):
        try:
            _last_call[0] = time.time()
            r = _session.get(url, params=params, timeout=timeout,
                             headers=_auth_headers(url))
            if r.status_code in (429, 500, 502, 503, 504):
                # ★ OpenAlex 的 429 里 retryAfter 可能长达数万秒（等下一天）——
                #   那种情况重试没意义，直接放弃并说明原因，别把整批拖死。
                ra = r.headers.get("Retry-After")
                try:
                    ra_s = float(ra) if ra else 0
                except ValueError:
                    ra_s = 0
                if r.status_code == 429 and ra_s > 300:
                    key_hint = ("" if OPENALEX_KEY else
                                "  ⇒ 需在 .env 里填 OPENALEX_API_KEY"
                                "（https://openalex.org/settings/api）")
                    print(f"    [配额耗尽] 需等 {ra_s/3600:.1f} 小时重置"
                          f"（{url.split('/')[2]}）{key_hint}")
                    return None
                wait = delay
                if ra_s:
                    wait = max(wait, ra_s)
                if attempt < retries:
                    print(f"    [限流] HTTP {r.status_code}，{wait:.0f}s 后重试"
                          f"（{attempt}/{retries}）")
                    time.sleep(wait)
                    delay *= 2
                    continue
                print(f"    [失败] HTTP {r.status_code}，已重试 {retries} 次")
                return None
            if r.status_code != 200:
                print(f"    [失败] HTTP {r.status_code}")
                return None
            return r.json() if expect_json else r.text
        except Exception as e:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            print(f"    [失败] {type(e).__name__}: {str(e)[:70]}")
            return None
    return None


# ══════════════════════════════════════════════
# 2. 统一输出格式
# ══════════════════════════════════════════════
_TAG = re.compile(r"<[^>]+>")


def strip_jats(s):
    """Crossref 的摘要是 JATS XML 片段，剥成纯文本。"""
    if not s:
        return ""
    s = re.sub(r"</?(jats:)?(p|sec|title|italic|bold|sub|sup|xref|br)[^>]*>", " ", s)
    s = _TAG.sub(" ", s)
    s = (s.replace("&lt;", "<").replace("&gt;", ">")
          .replace("&amp;", "&").replace("&quot;", '"').replace("&#x2010;", "-"))
    return re.sub(r"\s+", " ", s).strip()


def norm_doi(d):
    d = (d or "").strip().lower()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", d)


def pack(title, link, summary, source, data_source, doi="", authors=None,
         year=None, issn=None, oa_pdf_url="", is_oa=False, extra=None):
    """打包成与 paper_radar 一致的字段结构。"""
    issn = issn or []
    rec = {
        "title": (strip_jats(title) or "").strip(),
        "link": link or "",
        "summary": strip_jats(summary) or "No abstract available",
        "source": source or "Unknown",
        "data_source": data_source,
        "doi": norm_doi(doi),
        "authors": authors or [],
        "year": year or time.localtime().tm_year,
        "issn": issn,
        "is_chinese_journal": any((i or "").strip() in CHINESE_ISSN for i in issn),
        "openalex_id": "",
        "oa_pdf_url": oa_pdf_url or "",
        "is_oa": bool(is_oa),
    }
    if extra:
        rec.update(extra)
    return rec


# ══════════════════════════════════════════════
# 3. Crossref
# ══════════════════════════════════════════════
CR_SELECT = ("DOI,title,abstract,author,published,container-title,ISSN,"
             "link,is-referenced-by-count,type")


def _cr_to_paper(it):
    t = it.get("title") or []
    title = strip_jats(t[0]) if t else ""
    if not title:
        return None
    src = (it.get("container-title") or [""])[0]
    dp = ((it.get("published") or {}).get("date-parts") or [[None]])[0]
    year = dp[0] if dp and dp[0] else None
    authors = []
    for a in (it.get("author") or [])[:10]:
        nm = " ".join(x for x in (a.get("given"), a.get("family")) if x).strip()
        if nm:
            authors.append(nm)
    doi = it.get("DOI", "")
    # Crossref 的 link 里可能带 PDF 直链
    pdf = ""
    for lk in (it.get("link") or []):
        if "pdf" in (lk.get("content-type") or "").lower():
            pdf = lk.get("URL", "")
            break
    return pack(title=title, link=("https://doi.org/" + doi if doi else ""),
                summary=it.get("abstract", ""), source=src,
                data_source="Crossref", doi=doi, authors=authors, year=year,
                issn=it.get("ISSN") or [], oa_pdf_url=pdf, is_oa=bool(pdf))


def fetch_crossref(queries, days=7, rows=200, per_query=True, max_total=800):
    """按主题查 Crossref。rows 上限 1000（Crossref 单次硬上限）。"""
    from datetime import datetime, timedelta
    frm = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    qs = queries if per_query else [" OR ".join(queries)]
    for i, q in enumerate(qs, 1):
        print(f"  [Crossref] 查询 {i}/{len(qs)}: {q!r}")
        d = polite_get("https://api.crossref.org/works",
                       params={"query.bibliographic": q, "rows": min(rows, 1000),
                               "filter": f"from-pub-date:{frm},type:journal-article",
                               "select": CR_SELECT, "mailto": MAILTO},
                       min_gap=1.0)
        items = ((d or {}).get("message") or {}).get("items", [])
        got = [p for p in (_cr_to_paper(x) for x in items) if p]
        ab = sum(1 for p in got if p["summary"] != "No abstract available")
        print(f"    取到 {len(got)} 篇（有摘要 {ab}）")
        out.extend(got)
        if len(out) >= max_total:
            break
    return out[:max_total]


def fetch_crossref_journals(journals=None, days=7, rows=200):
    """用 issn 过滤拿特定期刊的最新目录 —— 替代已死的 RSS。"""
    from datetime import datetime, timedelta
    journals = journals or JOURNAL_ISSN
    frm = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for name, issn in journals.items():
        print(f"  [Crossref/期刊] {name} (issn:{issn})")
        d = polite_get("https://api.crossref.org/works",
                       params={"filter": f"issn:{issn},from-pub-date:{frm},"
                                         f"type:journal-article",
                               "rows": min(rows, 1000), "select": CR_SELECT,
                               "sort": "published", "order": "desc",
                               "mailto": MAILTO},
                       min_gap=1.0)
        items = ((d or {}).get("message") or {}).get("items", [])
        got = [p for p in (_cr_to_paper(x) for x in items) if p]
        print(f"    取到 {len(got)} 篇")
        out.extend(got)
    return out


# ══════════════════════════════════════════════
# 3b. OpenAlex 学位论文（硕博）★ 2026-10-01 新增
# ══════════════════════════════════════════════
def _oa_to_paper(w, tag="OpenAlex"):
    """OpenAlex work → 统一格式（与 paper_radar._parse_work 一致的字段）。

    tag 用于区分来源子类：期刊论文 / 学位论文(OpenAlex-Diss) / 中文核心刊(OpenAlex-CN)，
    这样在有日报里能一眼看出各源贡献了多少。
    """
    title = w.get("title")
    if not title:
        return None
    pl = w.get("primary_location") or {}
    src = ((pl.get("source") or {}).get("display_name") or
           w.get("type") or "OpenAlex")
    inv = w.get("abstract_inverted_index")
    ab = ""
    if inv:
        pos = {}
        for word, idxs in inv.items():
            for i in idxs:
                pos[i] = word
        ab = " ".join(pos[k] for k in sorted(pos))
    best = w.get("best_oa_location") or {}
    oa = w.get("open_access") or {}
    pdf = (best.get("pdf_url") or oa.get("oa_url") or "").strip()
    doi = w.get("doi") or ""
    auth = [((a.get("author") or {}).get("display_name") or "")
            for a in (w.get("authorships") or [])][:10]
    return pack(title=title, link=doi or w.get("id") or "", summary=ab,
                source=src, data_source=tag,
                doi=doi, authors=[a for a in auth if a],
                year=w.get("publication_year"),
                issn=(((pl.get("source") or {}).get("issn")) or []),
                oa_pdf_url=pdf, is_oa=bool(oa.get("is_oa") or pdf))


def fetch_openalex_dissertations(queries, days=365, per_query=60, max_total=200):
    """学位论文（type:dissertation）。

    实测：`landslide rainfall` 近 3 年 497 篇、`preferential flow soil` 1,153 篇，
    **绝大多数带摘要**。相关性匹配较松（会混进化学/医学的），
    但本地打分免费 ⇒ 交给模型筛，不在这里卡。
    中文硕博在 OpenAlex 几乎为空（滑坡关键词只有 3 篇），CNKI 无公开 API。
    """
    from datetime import datetime, timedelta
    frm = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for i, q in enumerate(queries, 1):
        print(f"  [学位论文] {i}/{len(queries)}: {q!r}")
        d = polite_get("https://api.openalex.org/works",
                       params={"search": q,
                               "filter": f"type:dissertation,"
                                         f"from_publication_date:{frm}",
                               "per-page": min(per_query, 200),
                               "sort": "publication_date:desc",
                               "mailto": MAILTO}, min_gap=1.0)
        got = [p for p in (_oa_to_paper(w, tag="OpenAlex-Diss")
                           for w in ((d or {}).get("results") or [])) if p]
        ab = sum(1 for p in got if p["summary"] != "No abstract available")
        print(f"    取到 {len(got)} 篇（有摘要 {ab}）")
        out.extend(got)
        if len(out) >= max_total:
            break
    return out[:max_total]


# ══════════════════════════════════════════════
# 3c. OpenAlex 按 ISSN 取【中文核心刊】★ 2026-10-01 新增
# ══════════════════════════════════════════════
# 实测（这是唯一能拿到中文核心刊的路子）：
#   岩土工程学报 1000-4548      OpenAlex 3,258 篇 / Crossref 仅 2 篇
#   岩石力学与工程学报 1000-6915 OpenAlex 5,209 篇 / Crossref 445 篇
#   地球科学 1000-2383          OpenAlex 4,964 篇 / Crossref 3,872 篇
# ⚠️ 两个已知限制：
#   1. OpenAlex 的 language 字段不可靠（中文刊常标成 en）⇒ 别用 language 过滤，用 issn
#   2. 近期收录有滞后（岩石力学与工程学报近半年 80 篇，岩土工程学报近半年 0 篇），
#      且**无摘要**、标题是英译 ⇒ 只能到"题目级别"，靠本地模型看标题判断
CN_JOURNAL_ISSN = {
    "岩土工程学报": "1000-4548",
    "岩石力学与工程学报": "1000-6915",
    "地球科学": "1000-2383",
}


def fetch_openalex_journals(issns=None, days=90, per_journal=60, max_total=200):
    """按 ISSN 取特定期刊的最新文献（中文核心刊就靠这条路）。"""
    from datetime import datetime, timedelta
    issns = issns or CN_JOURNAL_ISSN
    frm = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for name, issn in issns.items():
        print(f"  [OpenAlex/期刊] {name} ({issn})")
        d = polite_get("https://api.openalex.org/works",
                       params={"filter": f"primary_location.source.issn:{issn},"
                                         f"from_publication_date:{frm}",
                               "per-page": min(per_journal, 200),
                               "sort": "publication_date:desc",
                               "mailto": MAILTO}, min_gap=1.0)
        got = [p for p in (_oa_to_paper(w, tag="OpenAlex-CN")
                           for w in ((d or {}).get("results") or [])) if p]
        print(f"    取到 {len(got)} 篇")
        out.extend(got)
        if len(out) >= max_total:
            break
    return out[:max_total]


# ══════════════════════════════════════════════
# 4. arXiv（预印本，100% 有摘要）
# ══════════════════════════════════════════════
def fetch_arxiv(queries, max_results=100):
    out = []
    for q in queries:
        print(f"  [arXiv] {q!r}")
        txt = polite_get(
            "http://export.arxiv.org/api/query",
            params={"search_query": q, "start": 0, "max_results": max_results,
                    "sortBy": "submittedDate", "sortOrder": "descending"},
            expect_json=False, min_gap=3.0)      # arXiv 要求 ≥3 秒
        if not txt:
            continue
        for m in re.finditer(r"<entry>(.*?)</entry>", txt, re.S):
            e = m.group(1)

            def g(tag):
                mm = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", e, re.S)
                return strip_jats(mm.group(1)) if mm else ""
            title = g("title")
            if not title:
                continue
            pdf = ""
            mp = re.search(r'<link[^>]+title="pdf"[^>]+href="([^"]+)"', e)
            if mp:
                pdf = mp.group(1)
            out.append(pack(title=title, link=g("id"), summary=g("summary"),
                            source="arXiv", data_source="arXiv",
                            doi=g("arxiv:doi"),
                            authors=[a.strip() for a in re.findall(
                                r"<name>(.*?)</name>", e, re.S)][:10],
                            year=(g("published") or "")[:4] or None,
                            oa_pdf_url=pdf, is_oa=True))
        time.sleep(3)
    return out


# ══════════════════════════════════════════════
# 5. Semantic Scholar（需要 API key，否则匿名池必 429）
# ══════════════════════════════════════════════
def fetch_semanticscholar(queries, limit=100, api_key=None):
    """★ 匿名调用实测【连续退避 26 秒仍然 429】——共享池已被全球用爆。
       必须去 https://www.semanticscholar.org/product/api 申请免费 key。
       它额外提供 `tldr`（AI 生成的一句话摘要），正好补摘要缺失。"""
    key = api_key or os.getenv("S2_API_KEY", "")
    hdr = {"x-api-key": key} if key else {}
    out = []
    for q in queries:
        print(f"  [S2] {q!r}{'（有 key）' if key else '（匿名，很可能 429）'}")
        r = None
        try:
            _last_call[0] = time.time()
            r = _session.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": q, "limit": min(limit, 100),
                        "fields": "title,abstract,tldr,year,venue,externalIds,"
                                  "openAccessPdf,authors"},
                headers=hdr, timeout=45)
        except Exception as e:
            print(f"    [失败] {type(e).__name__}")
            continue
        if r.status_code == 429:
            print("    [限流] 429 —— 需申请 API key")
            continue
        if r.status_code != 200:
            print(f"    [失败] HTTP {r.status_code}")
            continue
        for p in r.json().get("data", []):
            ab = p.get("abstract") or ((p.get("tldr") or {}).get("text") or "")
            pdf = (p.get("openAccessPdf") or {}).get("url", "")
            ex = p.get("externalIds") or {}
            out.append(pack(title=p.get("title"), link=(
                "https://doi.org/" + ex["DOI"] if ex.get("DOI")
                else p.get("url", "")),
                summary=ab, source=p.get("venue", ""), data_source="S2",
                doi=ex.get("DOI", ""),
                authors=[a.get("name", "") for a in (p.get("authors") or [])][:10],
                year=p.get("year"), oa_pdf_url=pdf, is_oa=bool(pdf)))
        time.sleep(1.0)
    return out


# ══════════════════════════════════════════════
# 6. 摘要补全（给闭源论文）
# ══════════════════════════════════════════════
def enrich_abstracts(papers, max_lookups=120):
    """给【没有摘要】的论文，拿 DOI 去 Crossref 补一个。

    为什么要：OpenAlex 对闭源论文的摘要覆盖只有约 24%（Elsevier/Wiley 多半不交摘要），
    而 Crossref 是出版商直接存元数据的地方，覆盖率更高。
    """
    # ★ 已经来自 Crossref 的就别再问 Crossref 了——它没有就是没有，
    #   再查一遍纯属浪费配额。只补【从别的源来的、那边没摘要的】。
    todo = [p for p in papers
            if (p.get("summary") or "").startswith("No abstract")
            and p.get("doi")
            and p.get("data_source") != "Crossref"][:max_lookups]
    if not todo:
        return 0
    print(f"  [摘要补全] {len(todo)} 篇无摘要，去 Crossref 补…")
    got = 0
    for i, p in enumerate(todo, 1):
        d = polite_get("https://api.crossref.org/works/" +
                       urllib.parse.quote(p["doi"]),
                       params={"mailto": MAILTO}, min_gap=0.4, retries=2)
        ab = ((d or {}).get("message") or {}).get("abstract", "")
        if ab:
            p["summary"] = strip_jats(ab)
            p["abstract_from"] = "Crossref"
            got += 1
        if i % 30 == 0:
            print(f"    …{i}/{len(todo)}，已补 {got}")
    print(f"  [摘要补全] 成功补齐 {got}/{len(todo)} 篇")
    return got


# ══════════════════════════════════════════════
# 6b. 从出版社【公开落地页】抓摘要
# ══════════════════════════════════════════════
# 说明：论文摘要是出版社【主动公开】的（用来引流引用），访问落地页看摘要是
# 任何读者都在做的事，**不是绕过付费墙**。本函数只读 meta / JSON-LD 里
# 已经公开的摘要字段，不碰正文、不碰 PDF。
# 若出版商用反爬（ScienceDirect 常见）返回挑战页，就老实返回空，不硬闯。
WEB_META_PATTERNS = [
    r'<meta[^>]+name=["\']citation_abstract["\'][^>]+content=["\']([^"\']{80,}?)["\']',
    r'<meta[^>]+content=["\']([^"\']{80,}?)["\'][^>]+name=["\']citation_abstract["\']',
    r'<meta[^>]+name=["\'](?:dc|DCTERMS)\.description["\'][^>]+content=["\']([^"\']{80,}?)["\']',
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']{80,}?)["\']',
    r'"description"\s*:\s*"((?:[^"\\]|\\.){80,}?)"',
]
_WEB_HDR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_abstract_from_web(doi, timeout=25):
    """抓一篇论文的公开摘要。成功返回文本，失败返回 ""（含被反爬拦的情况）。"""
    doi = norm_doi(doi)
    if not doi:
        return ""
    try:
        r = _session.get("https://doi.org/" + doi, headers=_WEB_HDR,
                         timeout=timeout, allow_redirects=True)
    except Exception:
        return ""
    if r.status_code != 200:
        return ""
    html = r.text
    # 一眼识破挑战页：正文太短，或明确是验证页
    low = html.lower()
    if len(html) < 8000 and ("captcha" in low or "are you a robot" in low
                             or "verify you are human" in low):
        return ""
    for pat in WEB_META_PATTERNS:
        m = re.search(pat, html, re.I | re.S)
        if m:
            t = m.group(1)
            t = t.replace("\\n", " ").replace("\\/", "/").replace('\\"', '"')
            t = _html.unescape(t)              # ★ 反转义 &lt;p&gt; 之类
            t = re.sub(r"<[^>]+>", " ", t)     # 去残留标签
            t = _html.unescape(t)
            t = re.sub(r"^\s*(?:Abstract|ABSTRACT|摘要)\s*[.．:：]?\s*", "", t)
            t = re.sub(r"\s+", " ", t).strip()
            if len(t) >= 120 and not t.lower().startswith(
                    ("download", "share", "copyright", "view ")):
                return t[:3000]
    return ""


def enrich_abstracts_web(papers, max_lookups=40, min_gap=1.5):
    """Crossref 补不到时，改从出版社落地页抓（会尊重间隔，失败静默跳过）。"""
    todo = [p for p in papers
            if (p.get("summary") or "").startswith("No abstract")
            and p.get("doi")
            and not p.get("_web_tried")][:max_lookups]
    if not todo:
        return 0
    print(f"  [网页补摘要] 尝试 {len(todo)} 篇（失败不报错，只是补不到）")
    got = 0
    for i, p in enumerate(todo, 1):
        p["_web_tried"] = True
        ab = fetch_abstract_from_web(p["doi"])
        if ab:
            p["summary"] = ab
            p["abstract_from"] = "web"
            got += 1
        time.sleep(min_gap)
        if i % 10 == 0:
            print(f"    …{i}/{len(todo)}，已补 {got}")
    print(f"  [网页补摘要] 成功 {got}/{len(todo)} 篇")
    return got


# ══════════════════════════════════════════════
# 7. 跨源去重
# ══════════════════════════════════════════════
def dedupe_by_title(papers):
    """按 规范化标题 / DOI 去重。保留先出现的（顺序即优先级）。"""
    seen_t, seen_d, out = set(), set(), []
    for p in papers:
        t = re.sub(r"[^0-9a-z一-鿿 ]", "",
                   re.sub(r"\s+", " ", (p.get("title") or "").lower())).strip()
        d = norm_doi(p.get("doi"))
        if (t and t in seen_t) or (d and d in seen_d):
            continue
        if t:
            seen_t.add(t)
        if d:
            seen_d.add(d)
        out.append(p)
    return out


# ══════════════════════════════════════════════
# 自测
# ══════════════════════════════════════════════
def _test():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ask_image",
        r"C:\Users\zihao\.claude\skills\local-vision\scripts\ask_image.py")
    ai = importlib.util.module_from_spec(spec)

    print("=" * 70)
    print("sources.py 自测")
    print("=" * 70)

    qs = ["landslide rainfall", "preferential flow soil"]

    print("\n① Crossref 主题检索")
    cr = fetch_crossref(qs, days=14, rows=100)
    print(f"   ⇒ {len(cr)} 篇")

    print("\n② Crossref 期刊目录（替代死掉的 RSS）")
    cj = fetch_crossref_journals(days=14, rows=100)
    print(f"   ⇒ {len(cj)} 篇")

    print("\n③ arXiv")
    ar = fetch_arxiv(['all:"preferential flow"'], max_results=30)
    print(f"   ⇒ {len(ar)} 篇")

    print("\n④ Semantic Scholar（没 key 预期 429）")
    s2 = fetch_semanticscholar(["landslide rainfall"], limit=50)
    print(f"   ⇒ {len(s2)} 篇")

    allp = cr + cj + ar + s2
    print(f"\n⑤ 合并去重：{len(allp)} → ", end="")
    ded = dedupe_by_title(allp)
    print(f"{len(ded)} 篇")

    print("\n⑥ 摘要补全")
    enrich_abstracts(ded, max_lookups=15)

    ab = sum(1 for p in ded if not p["summary"].startswith("No abstract"))
    print(f"\n最终摘要覆盖：{ab}/{len(ded)} = {100*ab/max(len(ded),1):.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(_test())
