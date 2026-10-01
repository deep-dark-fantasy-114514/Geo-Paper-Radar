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
import hashlib
import html as _html
import traceback
import io
import json
import os
import random
import re
import sys
import time
import urllib.parse

import feedparser

# ★ 2026-10-01：`datetime` / `timedelta` 原来【只靠下面的 `from config import *`
#   带进来】（config.py 里有 `from datetime import datetime, timedelta, timezone`）。
#   今天能跑，但这个耦合很脆：config 一旦加 `__all__`、或调整自己的 import，
#   这里立刻 NameError —— 而 `_fetch_single_query()` 把异常全吃了，
#   **表现是"OpenAlex 今天 0 篇"，不是报错**。显式导入，掐掉这种静默失效。
from datetime import datetime, timedelta          # noqa: F401

from config import *   # 常量（RSS_SOURCES / OPENALEX_* / CHINESE_JOURNALS_ISSN 等）与工具函数
import requests

# ★ 2026-10-01：`datetime` / `timedelta` 原来【只靠 `from config import *` 带进来】
#   （config.py 里有 `from datetime import datetime, timedelta, timezone`）。
#   今天能跑，但这个耦合很脆：config 一旦加 `__all__`、或调整自己的 import，
#   这里就会立刻 NameError —— 而 `_fetch_single_query()` 把异常全吃了，
#   **表现是"OpenAlex 今天 0 篇"，不是报错**。
#   显式导入，把这类静默失效的可能性直接掐掉。
from datetime import datetime, timedelta      # noqa: F401

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ★ 2026-10-01：原来硬编码私有邮箱。改成优先读环境变量，
#   与 config.SMTP_SENDER 保持一致（换邮箱只需改 .env）。
MAILTO = (os.getenv("PAPER_RADAR_MAILTO", "").strip()
          or os.getenv("SMTP_SENDER", "").strip()
          or "781005412@qq.com")
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
    """OpenAlex 需要 Authorization 头；其它源不需要。

    ★ 2026-10-01：原来是子串判断 `"api.openalex.org" in url` ——
      `https://api.openalex.org.attacker.example/x` 也满足它，
      于是 **OpenAlex 的 API key 会被发给第三方**。
      今天所有 URL 都是我们自己拼的，暂时打不出来；但 `polite_get()`
      是公共函数，将来任何数据源都能调它 —— 边界就该是边界。
      ⇒ 改成按 hostname 精确比较。
    """
    if not OPENALEX_KEY:
        return {}
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return {}
    if host == "api.openalex.org":
        return {"Authorization": "Bearer " + OPENALEX_KEY}
    return {}

# 与 paper_radar.CHINESE_JOURNALS_ISSN 保持一致
# ★ 2026-10-01：原来这里又抄了一份 ISSN。改为直接复用 config 里的那一份，
#   避免"在 config 加了新刊、这里不同步"的问题。
CHINESE_ISSN = set(CHINESE_JOURNALS_ISSN)

# ★ 2026-10-01：删掉了这里的 `DEAD_RSS = {...}`。
#   它是【死代码】—— 定义了却从没人读；而那两个死源早已从
#   config.RSS_SOURCES 里移除，所以"每次跑都请求死源"并不成立。
#   真正生效的防线改在 fetch_papers_from_rss() 里读 config.DEAD_RSS_BLACKLIST
#   （原来那个配置项同样是死的，现在让它真的起作用：
#    万一以后有人又把死源加回 RSS_SOURCES，会被直接跳过而不是每次白试。）

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
def polite_gap(min_gap=1.0):
    """只做【模块级统一礼貌间隔】，不发起请求。

    ★ 2026-10-01：`_last_call` 是模块级的，本意是"所有源串行、总速率封顶"，
      但 `polite_get()` 之外还有三条路自己发请求（RSS 自建 Session、
      Semantic Scholar、网页补摘要），**完全绕开了这个限流器**。
      于是"统一礼貌请求"这个设计只覆盖了一半的数据源。
      这里把间隔拿出来单独成函数，让那些"不能走 polite_get 的调用"
      （要 XML 的 arXiv 式请求、要 HTML 的落地页、S2 的特殊返回结构）
      至少也落进同一道节流里。
    """
    gap = time.time() - _last_call[0]
    if gap < min_gap:
        time.sleep(min_gap - gap + random.uniform(0, min_gap * 0.2))
    _last_call[0] = time.time()


def _parse_retry_after(raw):
    """解析 Retry-After 头，返回秒数（解析不了返回 0）。

    ★ 2026-10-01：原来只做 `float(ra)`。而 HTTP 规范里 `Retry-After`
      还允许 **HTTP-date** 格式（`Retry-After: Wed, 21 Oct 2015 07:28:00 GMT`），
      那是服务器最正式的表达。`float()` 抛 ValueError 后被吞成 0
      ⇒ 我们按自己的 1.5s/3s/6s 去重试，**完全无视服务器说的"等多久"**，
      既不礼貌，也很容易再次撞 429。
    """
    s = (raw or "").strip()
    if not s:
        return 0.0
    try:
        return max(0.0, float(s))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return max(0.0, (dt - datetime.now(dt.tzinfo)).total_seconds())
    except Exception:
        pass
    return 0.0


def polite_get(url, params=None, timeout=45, retries=4, base_delay=1.5,
               min_gap=1.0, expect_json=True, headers_override=None):
    """带指数退避的 GET。

    · 429 / 500 / 502 / 503 / 504 → 退避重试，优先遵守 Retry-After 头
    · 每次调用之间至少隔 min_gap 秒（默认 1 s，Semantic Scholar 的官方建议）
    · 返回解析后的 JSON，失败返回 None（调用方自己决定怎么办，不抛异常打断整批）
    """
    # ★ 礼貌间隔：_last_call 是【模块级共享】的 ⇒ 所有源串行，
    #   总请求速率被这一道统一封顶（默认 1 次/秒），不会对任何一家形成压力。
    #   再叠一个 ±20% 抖动，避免规律性的整点突发。
    gap = time.time() - _last_call[0]
    if gap < min_gap:
        time.sleep(min_gap - gap + random.uniform(0, min_gap * 0.2))
    delay = base_delay
    for attempt in range(1, retries + 1):
        try:
            _last_call[0] = time.time()
            _h = dict(_auth_headers(url))
            if headers_override:
                _h.update(headers_override)   # 单次覆盖（如 arXiv 要 XML）
            r = _session.get(url, params=params, timeout=timeout, headers=_h)
            if r.status_code in (429, 500, 502, 503, 504):
                # ★ OpenAlex 的 429 里 retryAfter 可能长达数万秒（等下一天）——
                #   那种情况重试没意义，直接放弃并说明原因，别把整批拖死。
                ra_s = _parse_retry_after(r.headers.get("Retry-After"))
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
    # ★ 2026-10-01：原来手写替换 5 个实体，漏掉绝大多数
    #   （&#8211; &plusmn; &mu; &ndash; …）。改用标准反转义。
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def norm_doi(d):
    d = (d or "").strip().lower()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", d)


def pack(title, link, summary, source, data_source, doi="", authors=None,
         year=None, issn=None, oa_pdf_url="", is_oa=False, extra=None,
         oa_landing_url=""):
    """打包成与 paper_radar 一致的字段结构。

    ★★ 2026-10-01【oa_pdf_url 与 oa_landing_url 必须分开】★★
      原来只有一个 `oa_pdf_url`，而 OpenAlex 那边写的是
      `best_oa.pdf_url or open_access.oa_url` —— 后者的官方定义是
      **"最佳 OA location 的 URL"**，可能只是出版社/仓储的**落地页**，不是 PDF。
      于是字段名说谎：`oa_pdf_url` 里躺着一个网页。
      表现就是 download.py 常常拉回 HTML、再靠 `citation_pdf_url` 二次找直链
      （那条兜底路径能救回来，所以成功率没归零，但语义一直是错的）。
      ⇒ 现在：
          oa_pdf_url     —— 只放**确定的 PDF 直链**
          oa_landing_url —— OA 落地页（download.py 会拿它去页里找 citation_pdf_url）
      两者都给 download.py 用，但用途分明。
    """
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
        "oa_landing_url": oa_landing_url or "",
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
        ab = sum(1 for p in got if not no_abstract(p))
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
    # ★ 2026-10-01：`best_oa_location.pdf_url` 才是 PDF 直链；
    #   `open_access.oa_url` 按官方定义是"最佳 OA location 的 URL"（可能是落地页）。
    #   原来用 `or` 把两者并成一个字段，等于让落地页冒充 PDF 直链。现已分开。
    pdf = (best.get("pdf_url") or "").strip()
    landing = (oa.get("oa_url") or "").strip()
    if not landing:
        landing = (best.get("landing_page_url") or "").strip()
    doi = w.get("doi") or ""
    auth = [((a.get("author") or {}).get("display_name") or "")
            for a in (w.get("authorships") or [])][:10]
    return pack(title=title, link=doi or w.get("id") or "", summary=ab,
                source=src, data_source=tag,
                doi=doi, authors=[a for a in auth if a],
                year=w.get("publication_year"),
                issn=(((pl.get("source") or {}).get("issn")) or []),
                oa_pdf_url=pdf, oa_landing_url=landing,
                is_oa=bool(oa.get("is_oa") or pdf or landing))


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
        ab = sum(1 for p in got if not no_abstract(p))
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
# ★ 2026-10-01：这里原来又抄了一份三本刊的 {刊名: ISSN} —— 与
#   config.CHINESE_JOURNALS_ISSN 是两个 authority（"加了一本新刊、
#   另一处不同步"的经典坑）。现在统一从 config 取。
CN_JOURNAL_ISSN = dict(CHINESE_JOURNALS)


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
            # ★ arXiv 返回 XML，而模块级 _session 的默认头是 Accept: application/json
            #   —— 显式覆盖，免得某些网关按 406 Not Acceptable 拒掉。
            headers_override={"Accept": "application/atom+xml,text/xml,*/*"},
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
            # ★ 2026-10-01：原来只是把 `_last_call[0]` 拍成 now，**没有等待** ——
            #   那不是限流，只是"记录一下"。现在走统一的 polite_gap()，
            #   与 polite_get 共用同一个模块级节流窗口（S2 官方建议 ≤1 req/s，
            #   这里保守取 3 s，因为它是最容易被 429 的一家）。
            polite_gap(3.0)
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
            if no_abstract(p)
            and p.get("doi")
            and p.get("data_source") != "Crossref"][:max_lookups]
    if not todo:
        return 0
    print(f"  [摘要补全] {len(todo)} 篇无摘要，去 Crossref 补…")
    got = 0
    for i, p in enumerate(todo, 1):
        d = polite_get("https://api.crossref.org/works/" +
                       urllib.parse.quote(p["doi"]),
                       params={"mailto": MAILTO}, min_gap=1.0, retries=2)
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
# ★ 2026-10-01【按可信度分级】。原来这五条是**平铺**的、谁先匹配用谁 ——
#   于是 `og:description`（网页 SEO 描述）和 `"description"`（JSON-LD 里
#   任意对象的描述字段）都能被当成论文摘要送去做四维打分。
#   实测能捞到的例子就像 "Explore cutting-edge research published in…"
#   这种网站自我介绍 —— 喂给 Qwen 会凭空产出一个分数。
#   ⇒ 只让 A/B 级进 summary；C 级单独放 web_description，仅作参考不进打分。
WEB_META_PATTERNS_HIGH = [      # A 级：出版商专门为本文声明的摘要
    r'<meta[^>]+name=["\']citation_abstract["\'][^>]+content=["\']([^"\']{80,}?)["\']',
    r'<meta[^>]+content=["\']([^"\']{80,}?)["\'][^>]+name=["\']citation_abstract["\']',
]
WEB_META_PATTERNS_MID = [       # B 级：DC 元数据，通常是摘要
    r'<meta[^>]+name=["\'](?:dc|DCTERMS)\.description["\'][^>]+content=["\']([^"\']{80,}?)["\']',
]
WEB_META_PATTERNS_LOW = [       # C 级：可能是 SEO 描述 / 网站介绍，不一定是论文摘要
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']{80,}?)["\']',
    r'"description"\s*:\s*"((?:[^"\\]|\\.){80,}?)"',
]
# 兼容旧名（有地方可能引用）
WEB_META_PATTERNS = WEB_META_PATTERNS_HIGH + WEB_META_PATTERNS_MID
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
        # ★ 2026-10-01：这些请求原来【完全绕开】统一限流器（调用方只是
        #   `time.sleep(min_gap)`，而且 429 就直接丢）。现在落进同一道节流窗口。
        polite_gap(1.5)
        r = _session.get("https://doi.org/" + doi, headers=_WEB_HDR,
                         timeout=timeout, allow_redirects=True)
        # 429 / 5xx 时退避重试一次（原来直接放弃）
        if r.status_code in (429, 500, 502, 503, 504):
            _w = _parse_retry_after(r.headers.get("Retry-After")) or 5.0
            print(f"    [限流] HTTP {r.status_code}，{_w:.0f}s 后重试一次")
            time.sleep(min(_w, 30.0))
            polite_gap(1.5)
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
    def _clean(raw):
        t = raw
        t = t.replace("\\n", " ").replace("\\/", "/").replace('\\"', '"')
        t = _html.unescape(t)              # ★ 反转义 &lt;p&gt; 之类
        t = re.sub(r"<[^>]+>", " ", t)     # 去残留标签
        t = _html.unescape(t)
        t = re.sub(r"^\s*(?:Abstract|ABSTRACT|摘要)\s*[.．:：]?\s*", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) < 120 or t.lower().startswith(
                ("download", "share", "copyright", "view ")):
            return ""
        return t[:3000]

    # ★ 只认 A/B 级。C 级（og:description / JSON-LD description）**不再当摘要** ——
    #   那些常常是网站的自我介绍，喂给打分模型会凭空造出一个分数。
    #   真需要的话调用方可以从返回的 "" 里意识到"没抓到"，而不是拿到脏数据。
    for pat in WEB_META_PATTERNS_HIGH + WEB_META_PATTERNS_MID:
        m = re.search(pat, html, re.I | re.S)
        if m:
            t = _clean(m.group(1))
            if t:
                return t
    return ""


def enrich_abstracts_web(papers, max_lookups=40, min_gap=1.5):
    """Crossref 补不到时，改从出版社落地页抓（会尊重间隔，失败静默跳过）。"""
    todo = [p for p in papers
            if no_abstract(p)
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
def _norm_title_key(title):
    """规范化标题，用于跨源去重。

    ★ 2026-10-01：实现搬到 `config.norm_title`（全仓唯一）。本文件、
      `processed.key_of`、`manual_ingest` 原来各写一份，已经漂过一次
      （manual_ingest 那份漏了"剥标签"）。这里只留薄封装，不再自带规则。

    ⚠️ 与 `processed.key_of` 的**唯一**差别：那边返回时截断 120 字，这边不截。
      跨源去重要求全标题比对；而且两者从不互相比较，无碍。
    """
    return norm_title(title)


def dedupe_by_title(papers):
    """按【规范化 DOI（优先）】或【规范化标题（兜底）】去重。

    保留先出现的（顺序即优先级）。跨源（RSS / OpenAlex / Crossref）的标题
    常带细微差异：尾部句点、HTML 实体、<i> 标签、连续空白 —— 统一在
    `_norm_title_key` 里处理。
    """
    seen_t, seen_d, out = set(), set(), []
    for p in papers:
        t = _norm_title_key(p.get("title"))
        d = norm_doi(p.get("doi"))
        # ★ 2026-10-01：标题和 DOI 都空的脏记录直接丢掉。
        #   原来 `if (t and t in seen_t) or (d and d in seen_d)` 在 t=d="" 时为 False，
        #   于是这种记录会【绕过去重逻辑】被加进结果里。
        if not t and not d:
            continue
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
    r"""自测：跑一遍 Crossref / 学位论文 / arXiv 三个源。

    不需要视觉模型（那部分是打分器的事），所以这里【不加载任何本地路径】——
    原版留了一句写死的 `C:\Users\zihao\...` 路径和一个没用到的变量，
    拿到别的机器上就是脏代码。
    """
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

    ab = sum(1 for p in ded if not no_abstract(p))
    print(f"\n最终摘要覆盖：{ab}/{len(ded)} = {100*ab/max(len(ded),1):.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(_test())


# ══════════════════════════════════════════════
# 8. RSS 抓取（2026-10-01 从 paper_radar.py 逐字拆入，未改逻辑）
# ══════════════════════════════════════════════

def fetch_rss_with_retry(url, max_retries=3):
    session = requests.Session()
    session.headers.update(get_chrome_headers())
    for attempt in range(1, max_retries + 1):
        try:
            # ★ 2026-10-01：RSS 原来自己建 Session、完全绕开统一限流器。
            #   现在共用模块级的 polite_gap，总请求速率仍由那一道封顶。
            polite_gap(1.0)
            print(f"  [尝试 {attempt}/{max_retries}] 正在请求 {url}")
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            if feed.bozo and not feed.entries:
                print(f"  [Warning] RSS 解析异常: {feed.bozo_exception}")
                continue
            return feed
        except requests.exceptions.Timeout:
            print(f"  [Warning] 请求超时（尝试 {attempt}/{max_retries}）")
        except requests.exceptions.RequestException as e:
            print(f"  [Warning] 请求失败: {e}（尝试 {attempt}/{max_retries}）")
        except Exception as e:
            print(f"  [Warning] 未知错误: {e}（尝试 {attempt}/{max_retries}）")
        if attempt < max_retries:
            wait = 2 ** attempt
            print(f"  [Info] 等待 {wait}s 后重试...")
            time.sleep(wait)
    return None


def fetch_papers_from_rss():
    """RSS 抓取，返回 list[dict]"""
    all_papers = []
    print("=" * 60)
    print("【RSS 源】文献抓取")
    print("=" * 60)

    for url in RSS_SOURCES:
        # ★ 2026-10-01：让 config.DEAD_RSS_BLACKLIST 真的生效（原来它也是死配置）。
        #   现状下 RSS_SOURCES 里已无死源，这一句是防"以后又被加回来"。
        if url in DEAD_RSS_BLACKLIST:
            print(f"  [跳过] 已知死源：{url[:60]}")
            continue
        name = infer_journal_name(url)
        print(f"\n[进度] 正在抓取 {name} ...")
        feed = fetch_rss_with_retry(url)
        if feed is None or not feed.entries:
            print(f"  [Warning] 跳过 {name}：抓取失败或无有效条目")
            continue
        count = 0
        for entry in feed.entries:
            if not is_within_hours(entry, FETCH_HOURS):
                continue
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", "") or entry.get("description", "") or ""
            if not title:
                continue
            all_papers.append({
                "title": title,
                "link": link,
                "summary": summary,
                "source": name,
                "data_source": "RSS",
            })
            count += 1
        print(f"  [完成] {name}: 获取 {count} 篇新文献")

    # 标题去重
    seen = set()
    unique = []
    for p in all_papers:
        t = p["title"].strip().lower()
        if t not in seen:
            seen.add(t)
            unique.append(p)
    print(f"\n[汇总] RSS 共抓取 {len(all_papers)} 篇，去重后 {len(unique)} 篇")
    return unique


# ══════════════════════════════════════════════
# 9. OpenAlex 搜索式抓取（同上，从 paper_radar.py 拆入）
# ══════════════════════════════════════════════


class OpenAlexFetcher:
    """
    OpenAlex API 文献抓取器（方案B：纯文本搜索，无 concept_id 硬限制）
    """

    def __init__(self, mailto):
        self.mailto = mailto
        # 不建 self.session —— 所有请求都走模块级 polite_get（它自带 _session
        # 和退避重试）。原来这里建的 Session 从不发请求，纯摆设。

    def _build_search_url(self, query, page=1):
        """用单个关键词构造 OpenAlex 查询"""
        from_date = (datetime.now() - timedelta(days=OPENALEX_DAYS_LOOKBACK)).strftime("%Y-%m-%d")
        params = {
            "filter": f"from_publication_date:{from_date}",
            # 使用 title_and_abstract.search 限定在标题和摘要中搜索
            "search": query,
            "sort": "publication_date:desc",
            "per_page": OPENALEX_PER_PAGE,
            "page": page,
            "mailto": self.mailto,
        }
        return f"{OPENALEX_BASE_URL}/works?{urllib.parse.urlencode(params)}"

    def _fetch_single_query(self, query):
        """执行单个关键词查询并解析结果。

        ★ 2026-10-01 修：原来用裸 `requests.get`，**完全没有退避重试** ——
        实测 OpenAlex 一限流就把 6 组查询全打成 0 篇，**910 篇覆盖悄无声息地没了**。
        现在改走 `sources.polite_get()`：指数退避 + 读 Retry-After + 查询间隔 ≥1s。
        """
        url = self._build_search_url(query)
        try:
            # ★ 2026-10-01：原来在方法里 `import sources as _src` 调自己——
            #   polite_get 就是本模块的顶层函数，直接调即可。
            #   （那段"改 sys.path 再自导入"是打补丁留下的，已删。）
            data = polite_get(url, min_gap=1.0, retries=5)
            if data is None:
                return [], 0
            papers = []
            for work in data.get("results", []):
                paper = self._parse_work(work)
                if paper:
                    papers.append(paper)
            return papers, (data.get("meta") or {}).get("count", 0)
        except Exception as e:
            print(f"  [Warning] 查询 '{query[:20]}' 失败: {e}")
            return [], 0

    def fetch_papers(self):
        """
        从 OpenAlex 抓取文献 — 多关键词分次查询后合并
        策略：用 6 个核心英文词分别查询，合并去重
        目的：避免 AND 逻辑太重导致空结果
        """
        all_papers = []
        print("\n" + "=" * 60)
        print("【OpenAlex 源】大规模文献检索（方案B：多关键词分次查询）")
        print("=" * 60)

        # 核心查询词（每个单独查询，OpenAlex 空格=AND 所以每个词尽量短）
        # ★ 2026-10-01：搬到 config.OPENALEX_QUERIES —— 别的源都能在 config 调，
        #   唯独这里不能，很容易"以为改了配置其实没改"。
        queries = list(OPENALEX_QUERIES)

        total_estimated = 0
        for q_idx, query in enumerate(queries, 1):
            print(f"\n[进度] 查询 ({q_idx}/{len(queries)}): '{query}'")
            papers, count = self._fetch_single_query(query)
            total_estimated += count
            if papers:
                for p in papers:
                    # 标记具体由哪个关键词命中
                    p["openalex_query"] = query
                all_papers.extend(papers)
            print(f"  [完成] 获取 {len(papers)} 篇（OpenAlex 估计 {count} 篇）")

        # 全局去重（按标题）
        seen_titles = set()
        unique_papers = []
        for p in all_papers:
            t = p["title"].strip().lower()
            if t and t not in seen_titles:
                seen_titles.add(t)
                unique_papers.append(p)

        print(f"\n[汇总] 多查询合并: {len(all_papers)} 篇 → 去重后 {len(unique_papers)} 篇")
        print(f"  [估计] OpenAlex 总结果数约 {total_estimated} 篇（含跨查询重复）")
        return unique_papers

    def _parse_work(self, work):
        """
        解析单篇 OpenAlex work 对象，转换为统一格式
        """
        try:
            title = work.get("title", "").strip()
            if not title:
                return None

            # 提取 DOI / URL
            # ★ 2026-10-01 更正：原变量名 pdf_url 名不副实——它取的其实是
            #   【落地页】(landing_page_url)，从来不是 PDF 直链。已改名避免误解。
            doi = work.get("doi", "") or ""
            openalex_url = work.get("id", "") or ""
            primary_location = work.get("primary_location", {}) or {}
            landing_url = primary_location.get("landing_page_url", "") or ""

            link = doi or landing_url or openalex_url

            # ★ 2026-10-01 新增：取真正的 OA PDF 直链（用于自动下载）
            best_oa = work.get("best_oa_location") or {}
            oa_info = work.get("open_access") or {}
            # ★ 2026-10-01：同 `_oa_to_paper` —— PDF 直链与 OA 落地页分开取。
            oa_pdf_url = (best_oa.get("pdf_url") or "").strip()
            oa_landing_url = (oa_info.get("oa_url") or "").strip()
            if not oa_landing_url:
                oa_landing_url = (best_oa.get("landing_page_url") or "").strip()
            is_oa = bool(oa_info.get("is_oa")) or bool(oa_pdf_url or oa_landing_url)

            # 提取摘要（OpenAlex 的 abstract_inverted_index）
            abstract = self._extract_abstract(work.get("abstract_inverted_index", {}))

            # 提取期刊信息
            source_obj = primary_location.get("source", {}) or {}
            journal_name = source_obj.get("display_name", "") or "Unknown"
            issn_list = source_obj.get("issn", []) or []

            # 提取作者
            authorships = work.get("authorships", []) or []
            authors = []
            for a in authorships[:10]:
                author_obj = a.get("author", {}) or {}
                name = author_obj.get("display_name", "")
                if name:
                    authors.append(name)

            # 提取年份
            pub_year = work.get("publication_year", datetime.now().year)

            # ★ 2026-10-01：改用 pack() 统一打包。
            #   原来这里【手工拼字典】，DOI 直接塞原始值（"https://doi.org/10.xxx"），
            #   而 _oa_to_paper 走 pack → norm_doi（"10.xxx"）——
            #   两个源产出的 DOI 格式不一致，dedupe_by_title 按 DOI 匹配时就失效了。
            #   现在两边走同一条路，字段结构与清洗标准完全统一。
            return pack(title=title, link=link, summary=abstract,
                        source=journal_name, data_source="OpenAlex",
                        doi=doi, authors=authors, year=pub_year,
                        issn=issn_list, oa_pdf_url=oa_pdf_url,
                        oa_landing_url=oa_landing_url, is_oa=is_oa,
                        extra={"openalex_id": openalex_url})

        except Exception as e:
            print(f"  [Warning] 解析 OpenAlex 条目失败: {e}")
            return None

    @staticmethod
    def _extract_abstract(inverted_index):
        """将 OpenAlex 的倒排索引摘要还原为纯文本"""
        if not inverted_index:
            return ""
        # 按位置排序
        word_positions = []
        for word, positions in inverted_index.items():
            for pos in positions:
                word_positions.append((pos, word))
        word_positions.sort(key=lambda x: x[0])
        return " ".join(word for _, word in word_positions)
