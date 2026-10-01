#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Geo_Paper_Radar V3.0 — 全自动地学文献雷达（OpenAlex + 两阶段过滤 + 中文期刊追踪）
============================================================
V3.0 新特性：
  1. RSS 抓取（保留 V2.0 逻辑，伪装 UA + 重试）
  2. OpenAlex API 大规模数据源（方案B：纯文本关键词检索，无概念硬限制）
  3. 中文核心期刊 ISSN 定向追踪（预留扩展列表）
  4. 两阶段过滤：本地 Regex 粗筛 → DeepSeek 细筛（防 API 费用暴涨）
  5. 中英双语关键词匹配
  6. V2.0 延续：四维度打分 + 双轨制筛选 + .ris 引文导出 + HTML邮件
  7. 空转保护
"""

import os
import json
import time
import hashlib
import smtplib
import traceback
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI

# ──────────────────────────────────────────────
# 0. 加载环境变量
# ──────────────────────────────────────────────
# override=False: 不覆盖已有的环境变量（GitHub Actions Secrets 注入的优先）
load_dotenv(override=False)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_SENDER = os.getenv("SMTP_SENDER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_RECEIVER = os.getenv("SMTP_RECEIVER", "")

# ──────────────────────────────────────────────
# 1. 配置区（你可以在这里修改）
# ──────────────────────────────────────────────

# ---- RSS 源 ----
RSS_SOURCES = [
    "https://rss.sciencedirect.com/publication/science/00137952",
    "https://rss.sciencedirect.com/publication/science/0169555X",
]

# ★ 2026-10-01 拉黑：下面两个 RSS 已【确认死亡】，每次跑都失败重试浪费时间。
#   两个期刊改由 Crossref 按 issn 拿最新目录（见 sources.JOURNAL_ISSN），覆盖更全。
DEAD_RSS_BLACKLIST = [
    # HTTP 200 但返回 HTML（RSS 端点已撤/被拦）
    "https://link.springer.com/search.rss?facet-journal-id=10346&channel-name=Landslides",
    # HTTP 404，端点已撤
    "https://agupubs.onlinelibrary.wiley.com/action/showFeed?jc=1944-7973&type=etoc&feed=rss",
]

# ---- OpenAlex 配置 ----
OPENALEX_BASE_URL = "https://api.openalex.org"
OPENALEX_PER_PAGE = 200          # 每页最多 200
OPENALEX_MAX_PAGES = 1           # 只取最近 1 页（200篇足够）
OPENALEX_DAYS_LOOKBACK = 7       # 抓取过去 7 天

# ---- DeepSeek 配置 ----
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# ---- 筛选阈值（双轨制） ----
TOTAL_SCORE_PASS = 30       # 轨道A：总分 ≥ 30/40
INNOVATION_PASS = 9         # 轨道B：单项创新分 ≥ 9/10
BROWSING_THRESHOLD = 24     # 备选泛读门槛：总分 ≥ 24/40
MAX_EMAIL_RESULTS = 10      # 最终邮件最多保留篇数
MAX_DEEPSEEK_INPUT = 40     # 进入 DeepSeek 阶段的文献上限（防 API 费用暴涨）

# ---- 中文核心期刊 ISSN（预留扩展列表）----
CHINESE_JOURNALS_ISSN = [
    "1000-6915",    # 岩石力学与工程学报
    "1000-4548",    # 岩土工程学报
    "1000-2383",    # 地球科学
    # 在此继续添加更多中文期刊 ISSN
]

# ---- 中英双语关键词库（用于第一层 Regex 粗筛）----
KEYWORD_LIST_EN = [
    "slope stability", "landslide", "rainfall infiltration",
    "preferential flow", "root reinforcement", "root cohesion",
    "unsaturated soil", "debris flow", "soil erosion",
    "hydraulic conductivity", "pore water pressure", "suction",
    "shallow landslide", "slope failure", "soil water",
    "runoff", "infiltration", "pore structure",
    "groundwater", "seepage", "eco-hydrology",
    "vegetation", "root system", "soil mechanics",
    "slope angle", "factor of safety", "limit equilibrium",
    "finite element", "numerical simulation", "stability analysis",
    "early warning", "landslide prediction", "rainfall threshold"
]

KEYWORD_LIST_CN = [
    "滑坡", "斜坡", "边坡", "稳定性", "降雨入渗",
    "优先流", "根系加固", "非饱和土", "泥石流",
    "土壤侵蚀", "渗透系数", "孔隙水压力", "基质吸力",
    "浅层滑坡", "边坡失稳", "土壤水", "径流",
    "入渗", "孔隙结构", "地下水", "渗流",
    "生态水文", "植被", "根系", "土力学",
    "安全系数", "极限平衡", "有限元", "数值模拟",
    "稳定性分析", "预警", "滑坡预测", "降雨阈值",
    "水土保持", "护坡", "加固"
]

# ---- 历史记录 & EndNote ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")
ENDNOTE_WATCH_DIR = os.path.join(BASE_DIR, "EndNote_Watch")

# ---- ★ 2026-10-01 新增：OA PDF 自动下载 ----
# 为什么要它：EndNote 的「PDF 自动导入文件夹」只认 PDF，不认 .ris
# （官方只提供了 PDF Handling 的自动导入）。所以要让流程"无需人工介入"，
# 必须把开放获取(OA)论文的 PDF 也抓下来，丢进 EndNote 的自动导入文件夹。
# 首次配置（只做一次）：EndNote → Edit → Preferences → PDF Handling
#   ☑ Enable automatic importing，PDF Auto Import Folder = 下面这个目录
PDF_INBOX_DIR = os.path.join(BASE_DIR, "PDF_Inbox")
PDF_DOWNLOAD_ENABLED = True      # 想临时关掉就设 False
PDF_MAX_PER_RUN = 20             # 单次最多下几篇，防止失控
PDF_MAX_MB = 60                  # 单个 PDF 体积上限（MB）
PDF_MIN_BYTES = 20 * 1024        # 小于 20 KB 的多半是错误页，丢弃

# ---- ★ 2026-10-01 新增：本地打分 + 自动归档 ----
# 用本地 Qwen3.5-9B 打分：0.7~1.1 s/篇、成本为 0 ⇒ 候选规模可以从 40 放开到几百
#
# ★★ 本地/云端自动判别 ★★
# GitHub Actions 跑在 ubuntu-latest 上，拿不到本机的 Qwen、视觉桥、Library 目录，
# 也没有 EndNote 的自动导入文件夹。所以按平台自动降级：
#   · 本机(Windows) → 完整流程：本地打分 + 下载 + 重命名 + 归档 + 简报
#   · 云端(Linux)   → 轻量流程：抓取 + DeepSeek 打分 + .ris + 邮件（老行为）
# 这样同一份代码两边都能跑，不用维护两个分支。
LOCAL_MODE = (os.name == "nt")
PDF_DOWNLOAD_ENABLED = LOCAL_MODE   # 云端下载了也没处放，直接关掉

SCORER = os.getenv("PAPER_RADAR_SCORER",
                   "local" if LOCAL_MODE else "deepseek")   # local | deepseek
MAX_CANDIDATES = 800          # 送入打分的候选上限（仅对 local 生效）
                              # 2026-10-01 由 500 提到 800：接入 Crossref 后候选变多，
                              # 再加上"无摘要全收"，500 会被撞满切掉正经论文
COARSE_MIN_HITS = 1           # 粗筛命中阈值（只作用于【有摘要】的）
COARSE_NO_ABSTRACT_BYPASS = True   # ★ 无摘要的（多是闭源）不卡关键词，全放行
                                   #   让本地模型看标题判 —— 实测能救回 440 篇被误杀的
                                   #   代价：多约 8 分钟打分（免费）
AUTO_RENAME_PDF = LOCAL_MODE      # 下载后用本地视觉模型重命名为【中文名】
AUTO_FILE_TO_LIBRARY = LOCAL_MODE  # 归档到 Library\<主题>\ 并复制一份到 PDF_Inbox\

# ---- ★★ 2026-10-01 新增：云端"攒候选"模式 ----
# 动机：新流程的打分/下载/重命名/归档全依赖本机（GPU + 磁盘 + EndNote），
#       云端跑到不了那半步。而笔记本（本机就是笔记本）合盖就睡，任务跑不了。
# 分工：云端每天只做【抓取】，把粗筛后的候选存成 JSON 提交回仓库（不花钱）；
#       本机开机后把这些候选合并进自己的池子，再跑完整流程。
#       ⇒ 笔记本睡几天也不漏文献。
HARVEST_MODE = os.getenv("PAPER_RADAR_MODE", "").lower() == "harvest"

# ---- ★ 2026-10-01 新增：多源检索 ----
# 实测：Crossref 单查询 55,505 条，其中【91% 是 OpenAlex 没有的】⇒ 最大增量。
# 两个 RSS 源（Springer Landslides / Wiley WRR）已死，改用 issn 走 Crossref 拿目录。
USE_CROSSREF = True
CROSSREF_QUERIES = [
    "landslide rainfall",
    "slope stability unsaturated soil",
    "preferential flow macropore",
    "rainfall infiltration slope",
    "debris flow",
    "unsaturated soil hydraulic",
]
CROSSREF_ROWS = 200           # 每组查询取多少（Crossref 单次硬上限 1000）
USE_ARXIV = False             # 预印本，地学覆盖小，默认关

# ---- ★ 摘要补全 ----
# 背景：闭源论文在 OpenAlex 的摘要覆盖只有约 24%（Elsevier/Wiley 不交摘要给 Crossref）。
#   · Crossref 补：对这一领域实测 0/25 —— Elsevier/Springer 根本没交，白搭但便宜
#   · 网页补    ：只对 Copernicus 这类平台有效，Elsevier/Springer 有反爬
# ⇒ 两条都留着但设上限，别为低产出耗时间。真正的希望是 Semantic Scholar（需 key）。
ABSTRACT_ENRICH_CROSSREF = 60   # 去 Crossref 查几篇（每篇约 0.4 s）
ABSTRACT_ENRICH_WEB = 20        # 去出版社落地页抓几篇（每篇约 1.5 s）
HARVEST_DIR = os.path.join(BASE_DIR, "harvest")
HARVEST_KEEP_DAYS = 14        # 本地只回捞最近 N 天的云端候选
HARVEST_MAX = 600             # 云端每天最多存几篇（控制仓库体积）
HARVEST_ABSTRACT_CHARS = 600  # 摘要截断长度
                              # 体积估算：600 篇 × 约 1.1 KB ≈ 660 KB/天
                              #           × 14 天 ≈ 9 MB（工作流会自动删 14 天前的）
HARVEST_CONSUMED = os.path.join(BASE_DIR, ".harvest_consumed.json")

# 仓库 raw 地址（仓库是公开的，无需 token）
REPO_RAW = ("https://raw.githubusercontent.com/"
            "deep-dark-fantasy-114514/Geo-Paper-Radar/main/harvest/")

# ---- 杂项 ----
FETCH_HOURS = 24         # RSS 抓取窗口（小时）
REQUEST_TIMEOUT = 30     # HTTP 请求超时


# ══════════════════════════════════════════════
# 2. 工具函数
# ══════════════════════════════════════════════

def get_chrome_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64 x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return set()
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("pushed", []))
    except (json.JSONDecodeError, FileNotFoundError):
        return set()


def save_history(links):
    existing = load_history()
    existing.update(links)
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"pushed": list(existing)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [Warning] 写入历史记录失败: {e}")


def make_link_key(entry):
    if isinstance(entry, dict):
        link = entry.get("link", "").strip()
        if link:
            return link
        entry_id = entry.get("id", "") or entry.get("guid", "") or ""
        if entry_id.startswith("http"):
            return entry_id
        title = entry.get("title", "")
        return hashlib.md5(title.encode("utf-8")).hexdigest()
    return str(entry)


def is_within_hours(entry, hours=24):
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if not published:
        return True
    pub_time = datetime(*published[:6], tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - pub_time) <= timedelta(hours=hours)


def safe_filename(text, max_len=40):
    safe = re.sub(r'[\\/*?:"<>|]', "", text)
    if len(safe) > max_len:
        safe = safe[:max_len]
    return safe.strip()


def infer_journal_name(url):
    if "10346" in url:
        return "Landslides"
    elif "00137952" in url:
        return "Engineering Geology"
    elif "0169555X" in url:
        return "Geomorphology"
    elif "1944-7973" in url:
        return "Water Resources Research"
    else:
        return "未知期刊"


# ══════════════════════════════════════════════
# 3. 模块一：RSS 数据源（保留 V2.0 逻辑）
# ══════════════════════════════════════════════

def fetch_rss_with_retry(url, max_retries=3):
    session = requests.Session()
    session.headers.update(get_chrome_headers())
    for attempt in range(1, max_retries + 1):
        try:
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
# 4. 模块二：OpenAlex 数据源（V3.0 新增）
# ══════════════════════════════════════════════

class OpenAlexFetcher:
    """
    OpenAlex API 文献抓取器（方案B：纯文本搜索，无 concept_id 硬限制）
    """

    def __init__(self, mailto):
        self.mailto = mailto
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64 x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        })

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
        """执行单个关键词查询并解析结果"""
        url = self._build_search_url(query)
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            papers = []
            for work in results:
                paper = self._parse_work(work)
                if paper:
                    papers.append(paper)
            meta = data.get("meta", {})
            count = meta.get("count", 0)
            return papers, count
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
        queries = [
            "landslide",
            "slope stability",
            "rainfall infiltration",
            "preferential flow",
            "debris flow",
            "unsaturated soil",
        ]

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
            oa_pdf_url = (best_oa.get("pdf_url") or
                          oa_info.get("oa_url") or "").strip()
            is_oa = bool(oa_info.get("is_oa")) or bool(oa_pdf_url)

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

            # 检查是否为中文核心期刊
            is_chinese = any(issn.strip() in CHINESE_JOURNALS_ISSN for issn in issn_list)

            return {
                "title": title,
                "link": link,
                "summary": abstract or "No abstract available",
                "source": journal_name,
                "data_source": "OpenAlex",
                "doi": doi,
                "authors": authors,
                "year": pub_year,
                "issn": issn_list,
                "is_chinese_journal": is_chinese,
                "openalex_id": openalex_url,
                "oa_pdf_url": oa_pdf_url,     # ★ 新增：OA PDF 直链（可为空）
                "is_oa": is_oa,               # ★ 新增：是否开放获取
            }

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


# ══════════════════════════════════════════════
# 5. 模块三：两阶段过滤（V3.0 核心）
# ══════════════════════════════════════════════

def _no_abstract(paper):
    """OpenAlex 对闭源论文不给摘要时，summary 会被填成 "No abstract available"。"""
    s = (paper.get("summary") or "").strip()
    return (not s) or s.startswith("No abstract")


def local_regex_coarse_filter(papers, min_hits=None):
    """
    第一层：本地 Regex 粗筛

    ★ 2026-10-01 更改：粗筛阈值可调，且 SCORER=local 时自动降到 1。
      原因：粗筛原本的唯一目的是【省 DeepSeek 的 API 费】。改用本地打分后
      这个目的不复存在，而硬卡"≥2 命中"实测只剩 79/1033 篇——**丢掉 93%**，
      正是"漏掉优质文献"的真凶。放宽到 ≥1 后再由本地模型做精细判断。
    """
    if min_hits is None:
        min_hits = COARSE_MIN_HITS if SCORER == "local" else 2
    print("\n" + "=" * 60)
    print("【第一阶段】本地 Regex 粗筛")
    print("=" * 60)

    # 编译所有关键词为正则（忽略大小写）
    all_keywords = KEYWORD_LIST_EN + KEYWORD_LIST_CN
    # 按长度降序排列以确保长词优先匹配
    all_keywords_sorted = sorted(all_keywords, key=len, reverse=True)

    passed, n_bypass = [], 0
    for idx, paper in enumerate(papers, 1):
        title = paper.get("title", "")
        summary = paper.get("summary", "")
        text = (title + " " + summary).lower()

        # ★ 2026-10-01：无摘要的（多是闭源）【不卡关键词】，全放行给本地模型判。
        #   原因：闭源论文只有标题可比，而关键词表是多词精确短语
        #   （如 "slope stability"），标题里换个说法（"rooted slope instability"）
        #   就命中不了 —— 实测 654 篇无摘要里有 440 篇（67%）因此被误杀，
        #   其中包括 Engineering Geology / Journal of Hydrology 的正经论文。
        #   反过来，放宽成"slope/flow/soil"这类宽泛词更糟：会把
        #   "Tafel slope"（电化学）、"soil respiration"（生态）也捞进来。
        #   本地打分免费，让模型看标题判断，比任何词表都准。
        if _no_abstract(paper) and COARSE_NO_ABSTRACT_BYPASS:
            paper["regex_hits"] = ["<无摘要·全收>"]
            passed.append(paper)
            n_bypass += 1
            continue

        # 统计命中关键词数
        hit_count = 0
        hit_words = []
        for kw in all_keywords_sorted:
            if kw.lower() in text:
                hit_count += 1
                hit_words.append(kw)
                if hit_count >= min_hits:
                    break

        if hit_count >= min_hits:
            paper["regex_hits"] = hit_words[:5]  # 记录前 5 个命中词
            passed.append(paper)

    print(f"  [输入] {len(papers)} 篇 → 粗筛后 {len(passed)} 篇"
          f"（其中 {n_bypass} 篇是无摘要直接放行）")
    print(f"  [规则] 有摘要者命中 ≥{min_hits} 个核心关键词；"
          f"无摘要者全收（交本地模型判）")
    print(f"  [中文关键词数] {len(KEYWORD_LIST_CN)} 个   [英文关键词数] {len(KEYWORD_LIST_EN)} 个")

    # 打印几个样本
    for p in passed[:3]:
        hits = p.get("regex_hits", [])
        print(f"    例: [{p.get('source','?')}] {p['title'][:50]}... → 命中: {hits}")

    return passed


def limit_for_deepseek(papers, max_count=MAX_DEEPSEEK_INPUT):
    """
    控制进入 DeepSeek 的文献数量，防止 API 费用暴涨
    """
    if len(papers) <= max_count:
        return papers
    print(f"\n  [限流] 粗筛后 {len(papers)} 篇超出阈值 {max_count}，随机采样中...")
    # 按 source 分层采样，尽量保证各来源都有代表
    from collections import defaultdict
    by_source = defaultdict(list)
    for p in papers:
        by_source[p.get("source", "Unknown")].append(p)

    sampled = []
    # 轮询各源
    while len(sampled) < max_count:
        added = 0
        for src, lst in by_source.items():
            if lst:
                sampled.append(lst.pop(0))
                added += 1
                if len(sampled) >= max_count:
                    break
        if added == 0:
            break

    print(f"  [结果] 最终送入 DeepSeek: {len(sampled)} 篇")
    return sampled


# ══════════════════════════════════════════════
# 6. 模块四：DeepSeek 多维度智能打分（V2.0 复用）
# ══════════════════════════════════════════════

def build_deepseek_prompt(title, abstract):
    system_prompt = (
        "你是一位资深地学审稿专家，专攻地质灾害与水文地质领域。\n\n"
        "请根据以下四个维度对文献进行独立评分（每维度 0-10 分，整数）：\n"
        "1. 斜坡稳定性（Slope Stability）：是否涉及边坡失稳机理、稳定性分析方法、加固技术等\n"
        "2. 降雨入渗（Rainfall Infiltration）：是否涉及雨水入渗过程、渗流场分析、入渗模型等\n"
        "3. 优先流（Preferential Flow）：是否涉及大孔隙流、根土间隙流、裂隙流等非达西流\n"
        "4. 方法创新（Method Innovation）：方法/模型/实验设计的新颖性和突破性\n\n"
        "评分完毕后，计算 total_score = 四维分数之和（0-40 分）。\n"
        "根据总分给出推荐等级：\n"
        "  - 若 total_score >= 30 → recommendation = \"strong\"（强烈推荐）\n"
        "  - 若 total_score >= 24 → recommendation = \"normal\"（值得关注）\n"
        "  - 否则               → recommendation = \"weak\"（参考阅读）\n\n"
        "请严格输出以下 JSON 格式，不要输出任何其他内容：\n"
        '{"slope_stability": <0-10整数>, "rainfall_infiltration": <0-10整数>, '
        '"preferential_flow": <0-10整数>, "method_innovation": <0-10整数>, '
        '"total_score": <0-40整数>, '
        '"reason": "<20字以内的中文推荐理由>", '
        '"tldr": "<一句话中文总结该文创新点>"}'
    )
    user_prompt = f"题目：{title}\n\n摘要：{abstract[:2000]}"
    return system_prompt, user_prompt


def score_paper_with_deepseek(title, abstract):
    system_prompt, user_prompt = build_deepseek_prompt(title, abstract)
    try:
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}],
            temperature=0.3,
            max_tokens=300,
        )
        content = response.choices[0].message.content.strip()
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1:
            content = content[start:end + 1]
        result = json.loads(content)
        required = ["slope_stability", "rainfall_infiltration",
                     "preferential_flow", "method_innovation",
                     "total_score", "reason", "tldr"]
        for field in required:
            if field not in result:
                raise ValueError(f"缺少字段: {field}")
        for dim in ["slope_stability", "rainfall_infiltration", "preferential_flow", "method_innovation"]:
            result[dim] = max(0, min(10, int(result[dim])))
        result["total_score"] = max(0, min(40, int(result["total_score"])))
        ts = result["total_score"]
        result["recommendation"] = "strong" if ts >= TOTAL_SCORE_PASS else ("normal" if ts >= BROWSING_THRESHOLD else "weak")
        result["score"] = round(result["total_score"] / 40 * 100)
        result["tldr"] = result.get("tldr", "")
        return result
    except json.JSONDecodeError as e:
        print(f"  [Error] JSON 解析失败: {e}")
        if 'content' in locals():
            print(f"  [Debug] 原始返回: {content[:200]}")
    except Exception as e:
        print(f"  [Error] API 调用失败: {e}")
    return None


def score_all_papers(papers, phase_label="DeepSeek"):
    """对文献列表打分。

    ★ 2026-10-01：默认走【本地 Qwen3.5-9B】（免费、0.7~1.1 s/篇），
    规模从 40 放开到 MAX_CANDIDATES=500。本地不可用时自动回退 DeepSeek。
    """
    if SCORER == "local" and papers:
        print("\n" + "=" * 60)
        print(f"【第二阶段】本地 Qwen3.5-9B 多维度打分（免费，共 {len(papers)} 篇）")
        print("=" * 60)
        try:
            import sys as _sys
            if BASE_DIR not in _sys.path:
                _sys.path.insert(0, BASE_DIR)
            import local_scorer
            scored = local_scorer.score_all(papers)
        except Exception as e:
            print(f"  [Error] 本地打分模块异常：{e}")
            scored = None
        if scored:
            for p in scored:                       # 补齐与 DeepSeek 版一致的派生字段
                ts = p.get("total_score", 0)
                p["recommendation"] = ("strong" if ts >= TOTAL_SCORE_PASS
                                       else ("normal" if ts >= BROWSING_THRESHOLD
                                             else "weak"))
                p["score"] = round(ts / 40 * 100)
            return scored
        print("  [回退] 本地打分不可用，改用 DeepSeek")

    # ─────────────── 以下为原 DeepSeek 路径（保留作后备） ───────────────
    print("\n" + "=" * 60)
    print(f"【第二阶段】DeepSeek AI 多维度打分 ({phase_label})")
    print("=" * 60)

    scored = []
    total = len(papers)
    for idx, paper in enumerate(papers, 1):
        title = paper["title"]
        abstract = paper["summary"]
        source = paper.get("source", "?")
        print(f"\n[进度] ({idx}/{total}) 正在打分 [{source}]: {title[:60]}...")

        result = score_paper_with_deepseek(title, abstract)
        if result is None:
            print(f"  [跳过] 该篇打分失败，已跳过")
            continue

        paper.update(result)
        scored.append(paper)
        dims = (f"S:{result['slope_stability']} R:{result['rainfall_infiltration']} "
                f"P:{result['preferential_flow']} M:{result['method_innovation']}")
        print(f"  [得分] {result['total_score']}/40 {dims} | {result['reason']}")
        time.sleep(0.5)

    print(f"\n[汇总] 成功打分 {len(scored)}/{total} 篇")
    return scored


# ══════════════════════════════════════════════
# 7. 模块五：双轨制筛选 + .ris + 邮件（V2.0 复用）
# ══════════════════════════════════════════════

def dual_track_filter(papers):
    print("\n" + "=" * 60)
    print("【双轨制筛选】(V3.0)")
    print("=" * 60)

    papers.sort(key=lambda x: x.get("total_score", 0), reverse=True)
    history = load_history()
    deduped = []
    skipped = 0
    for p in papers:
        lk = make_link_key(p)
        if lk in history:
            skipped += 1
            continue
        deduped.append(p)
    if skipped > 0:
        print(f"  [去重] 过滤掉 {skipped} 篇已推送过的文献")

    pass_list = []
    browsing_list = []
    for p in deduped:
        ts = p.get("total_score", 0)
        mi = p.get("method_innovation", 0)
        track_a = ts >= TOTAL_SCORE_PASS
        track_b = mi >= INNOVATION_PASS
        if track_a or track_b:
            p["pass_track"] = "A" if track_a and not track_b else ("B" if track_b and not track_a else "A+B")
            pass_list.append(p)
        elif ts >= BROWSING_THRESHOLD:
            browsing_list.append(p)

    pass_list = pass_list[:MAX_EMAIL_RESULTS]
    browsing_list = browsing_list[:MAX_EMAIL_RESULTS]

    print(f"  [轨道A] 总分≥{TOTAL_SCORE_PASS}/40: {sum(1 for p in pass_list if 'A' in p.get('pass_track',''))} 篇")
    print(f"  [轨道B] 创新分≥{INNOVATION_PASS}/10: {sum(1 for p in pass_list if 'B' in p.get('pass_track',''))} 篇")
    print(f"  [通关] {len(pass_list)} 篇 → 推送邮件 + .ris")
    print(f"  [备选] {len(browsing_list)} 篇 → 仅终端 + .ris")
    return pass_list, browsing_list


def generate_ris_file(paper):
    os.makedirs(ENDNOTE_WATCH_DIR, exist_ok=True)
    try:
        title = paper.get("title", "Untitled")
        score = paper.get("total_score", 0)
        tldr = paper.get("tldr", "")
        link = paper.get("link", "")
        source = paper.get("source", "")
        summary = paper.get("summary", "")
        authors = paper.get("authors", [])
        if not isinstance(authors, list):
            authors = []

        ris_lines = ["TY  - JOUR"]
        ris_lines.append(f"TI  - {title}")
        for author in authors[:10]:
            if author:
                ris_lines.append(f"AU  - {author}")
        ris_lines.append(f"PY  - {datetime.now().year}//")
        ris_lines.append(f"JO  - {source}")
        if link:
            ris_lines.append(f"UR  - {link}")
            doi_match = re.search(r'10\.\d{4,}/[\w\.\-]+', link)
            if doi_match:
                ris_lines.append(f"DO  - {doi_match.group(0)}")
        # 数据源标记
        ds = paper.get("data_source", "RSS")
        ris_lines.append(f"KW  - Geo_Paper_Radar_V3.0")
        ris_lines.append(f"KW  - Source:{ds}")
        ris_lines.append(f"KW  - Score:{score}/40")
        if tldr:
            ris_lines.append("N1  - " + tldr)
        ris_lines.append("ER  - ")
        ris_content = "\n".join(ris_lines) + "\n"

        safe_title = safe_filename(title, 40)
        date_str = datetime.now().strftime("%Y-%m-%d")
        ds_tag = ds[:4]  # 数据源短标签
        filename = f"{date_str}_{ds_tag}_{score}分_{safe_title}.ris"
        filepath = os.path.join(ENDNOTE_WATCH_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(ris_content)
        return filepath
    except Exception as e:
        print(f"  [Warning] 生成 .ris 失败: {e}")
        return None


# ══════════════════════════════════════════════
# ★ 2026-10-01 新增：OA PDF 自动下载
# ══════════════════════════════════════════════

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


def build_html_email_v3(papers):
    """V3.0 HTML 邮件（增加数据源标记）"""
    today = datetime.now().strftime("%Y-%m-%d")
    cards_html = ""
    for i, p in enumerate(papers, 1):
        ts = p.get("total_score", 0)
        ss = p.get("slope_stability", 0)
        ri = p.get("rainfall_infiltration", 0)
        pf = p.get("preferential_flow", 0)
        mi = p.get("method_innovation", 0)
        reason = p.get("reason", "")
        tldr = p.get("tldr", "")
        title = p.get("title", "")
        link = p.get("link", "")
        source = p.get("source", "")
        track = p.get("pass_track", "A")
        ds = p.get("data_source", "RSS")

        pct = round(ts / 40 * 100)
        score_color = "#e74c3c" if pct >= 90 else ("#e67e22" if pct >= 75 else "#27ae60")
        track_badge = {"A": "📐 总分达标", "B": "💡 创新突破", "A+B": "🏆 双轨通关"}.get(track, "✅ 通关")
        ds_badge = "📡 RSS" if ds == "RSS" else "🌐 OpenAlex"

        def dim_bar(val):
            return "⭐" * val + "☆" * (10 - val)

        cards_html += f"""
        <div style="background:#ffffff; border:1px solid #e0e0e0; border-radius:12px; padding:20px; margin-bottom:16px; box-shadow:0 2px 8px rgba(0,0,0,0.06);">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;">
                <div>
                    <span style="display:inline-block; background:{score_color}; color:#fff; font-weight:bold; font-size:18px; padding:4px 14px; border-radius:20px; margin-right:12px;">{ts}/40</span>
                    <span style="color:#7f8c8d; font-size:13px;">{source}</span>
                    <span style="display:inline-block; background:#8e44ad; color:#fff; font-size:12px; padding:2px 10px; border-radius:12px; margin-left:8px;">{track_badge}</span>
                    <span style="display:inline-block; background:#2c3e50; color:#fff; font-size:12px; padding:2px 10px; border-radius:12px; margin-left:6px;">{ds_badge}</span>
                </div>
            </div>
            <div style="font-size:16px; font-weight:bold; color:#2c3e50; margin-bottom:8px;">{i}. {title}</div>
            <div style="background:#f0f7ff; border-left:4px solid #3498db; padding:10px 14px; margin:10px 0; border-radius:4px; font-size:15px; color:#2c3e50;">
                💡 <strong>创新点：</strong>{tldr}
            </div>
            <div style="font-size:13px; color:#555; margin:6px 0;">
                <div style="display:flex; flex-wrap:wrap; gap:8px; margin:8px 0;">
                    <span style="background:#f8f9fa; padding:4px 10px; border-radius:6px;">🏔️ 斜坡 {ss}/10 {dim_bar(ss)}</span>
                    <span style="background:#f8f9fa; padding:4px 10px; border-radius:6px;">🌧️ 降雨 {ri}/10 {dim_bar(ri)}</span>
                    <span style="background:#f8f9fa; padding:4px 10px; border-radius:6px;">💧 优先流 {pf}/10 {dim_bar(pf)}</span>
                    <span style="background:#f8f9fa; padding:4px 10px; border-radius:6px;">🔬 创新 {mi}/10 {dim_bar(mi)}</span>
                </div>
                📌 <strong>推荐理由：</strong>{reason}
            </div>
            <div style="margin-top:10px;"><a href="{link}" target="_blank" style="display:inline-block; background:#3498db; color:#fff; text-decoration:none; padding:8px 18px; border-radius:6px; font-size:14px;">🔗 阅读原文</a></div>
        </div>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="background:#f5f7fa; padding:20px; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;">
<div style="max-width:680px; margin:0 auto;">
    <div style="background:linear-gradient(135deg, #1a2a6c, #2d4373); border-radius:16px; padding:30px; text-align:center; margin-bottom:24px;">
        <h1 style="color:#ffffff; font-size:26px; margin:0 0 8px 0;">🌍 今日地学前沿 Top {len(papers)}</h1>
        <p style="color:#a8c8ff; font-size:14px; margin:0;">{today} · Geo_Paper_Radar V3.0 · OpenAlex + RSS 双源</p>
        <p style="color:#a8c8ff; font-size:13px; margin:6px 0 0 0;">评分维度：斜坡稳定性 / 降雨入渗 / 优先流 / 方法创新 · 双轨制筛选</p>
    </div>
    {cards_html}
    <div style="text-align:center; padding:20px; color:#95a5a6; font-size:13px; border-top:1px solid #e0e0e0; margin-top:10px;">
        <p style="margin:4px 0;">📡 数据源：RSS + OpenAlex · 两阶段过滤：Regex → DeepSeek</p>
        <p style="margin:4px 0;">🤖 AI 评分：DeepSeek · 双轨制：总分≥30 或 创新分≥9</p>
        <p style="margin:4px 0;">📁 .ris 引文已同步存入 EndNote_Watch</p>
    </div>
</div></body></html>"""
    return html


def send_email(html_content, attachments=None):
    print("\n" + "=" * 60)
    print("【发送邮件】")
    print("=" * 60)
    if not all([SMTP_SENDER, SMTP_PASSWORD, SMTP_RECEIVER]):
        print("  [Error] 邮箱配置不完整")
        return False
    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"🌍 地学前沿推送 V3.0 — {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = SMTP_SENDER
    msg["To"] = SMTP_RECEIVER
    # HTML 正文
    related = MIMEMultipart("related")
    related.attach(MIMEText(html_content, "html", "utf-8"))
    msg.attach(related)
    # 添加 .ris 附件
    if attachments:
        for filepath in attachments:
            if os.path.exists(filepath):
                with open(filepath, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition",
                        f"attachment; filename*=UTF-8''{urllib.parse.quote(os.path.basename(filepath))}"
                    )
                    msg.attach(part)
                print(f"  📎 附件: {os.path.basename(filepath)}")
    try:
        print(f"  [进度] 连接 {SMTP_SERVER}:{SMTP_PORT} ...")
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.login(SMTP_SENDER, SMTP_PASSWORD)
            server.sendmail(SMTP_SENDER, [SMTP_RECEIVER], msg.as_string())
        print(f"  [成功] 邮件已发送至 {SMTP_RECEIVER}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("  [Error] SMTP 认证失败")
    except smtplib.SMTPException as e:
        print(f"  [Error] SMTP 失败: {e}")
    except Exception as e:
        print(f"  [Error] 未知错误: {e}")
        traceback.print_exc()
    return False


# ══════════════════════════════════════════════
# ★ 2026-10-01 新增：云端"攒候选" + 本地"回捞"
# ══════════════════════════════════════════════

def fetch_all_candidates():
    """抓 RSS + OpenAlex + Crossref，跨源去重后返回。"""
    all_papers = []
    rss = fetch_papers_from_rss()
    if rss:
        all_papers.extend(rss)
    try:
        oa = OpenAlexFetcher(mailto=SMTP_SENDER or "radar@example.com").fetch_papers()
        if oa:
            all_papers.extend(oa)
    except Exception as e:
        print(f"  [Warning] OpenAlex 抓取失败：{e}")

    # ★ 2026-10-01：Crossref 也要进候选池（实测 91% 是 OpenAlex 没有的）。
    #   云端 harvest 同样用得上 —— 笔记本睡着的那些天，覆盖不能缩水。
    if USE_CROSSREF:
        try:
            import sys as _sys
            if BASE_DIR not in _sys.path:
                _sys.path.insert(0, BASE_DIR)
            import sources as _src
            all_papers.extend(_src.fetch_crossref(
                CROSSREF_QUERIES, days=OPENALEX_DAYS_LOOKBACK, rows=CROSSREF_ROWS))
            all_papers.extend(_src.fetch_crossref_journals(
                days=OPENALEX_DAYS_LOOKBACK))
        except Exception as e:
            print(f"  [Warning] Crossref 抓取失败：{e}")

    try:
        import sources as _src
        return _src.dedupe_by_title(all_papers)
    except Exception:
        seen, deduped = set(), []          # 退回到只按标题去重
        for p in all_papers:
            t = (p.get("title") or "").strip().lower()
            if t and t not in seen:
                seen.add(t)
                deduped.append(p)
        return deduped


HARVEST_FIELDS = ("title", "link", "source", "data_source", "doi", "authors",
                  "year", "issn", "is_chinese_journal", "openalex_id",
                  "oa_pdf_url", "is_oa", "regex_hits")


def run_harvest():
    """★ 云端模式：只抓取 → 粗筛 → 存 JSON。

    不打分、不发邮件、不下载、**不花钱**。存在的意义只有一个：
    笔记本合盖睡着时，云端照样把当天的候选记下来，等开机后回捞。
    """
    print("\n" + "#" * 62)
    print("#  云端 HARVEST 模式 —— 只攒候选，不打分不通知（免费）")
    print("#" * 62)
    cand = fetch_all_candidates()
    if not cand:
        print("[结果] 没抓到任何候选")
        return 0
    kept = local_regex_coarse_filter(cand, min_hits=COARSE_MIN_HITS)
    slim = []
    for p in kept[:HARVEST_MAX]:
        rec = {k: p.get(k) for k in HARVEST_FIELDS}
        rec["summary"] = (p.get("summary") or "")[:HARVEST_ABSTRACT_CHARS]
        slim.append(rec)
    os.makedirs(HARVEST_DIR, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(HARVEST_DIR, f"{day}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": day, "count": len(slim), "papers": slim},
                  f, ensure_ascii=False)
    print(f"\n[完成] 已写 {path}（{len(slim)} 篇，"
          f"{os.path.getsize(path)/1024:.0f} KB）")
    return 0


def _load_consumed():
    try:
        with open(HARVEST_CONSUMED, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_consumed(s):
    try:
        with open(HARVEST_CONSUMED, "w", encoding="utf-8") as f:
            json.dump(sorted(s), f)
    except Exception:
        pass


def merge_harvest(cand):
    """把云端攒的候选（最近 N 天、且本地尚未消费过的）合并进本地候选池。

    ★ 刻意【不用 git pull】：本地在 v3.0-dev 分支且有未提交改动，
      pull 会冲突。仓库是公开的，直接走 raw HTTP 取，无副作用。
    """
    consumed = _load_consumed()
    seen = set((p.get("title") or "").strip().lower() for p in cand)
    added, marks = 0, []
    for i in range(HARVEST_KEEP_DAYS):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        if day in consumed:
            continue
        try:
            r = requests.get(REPO_RAW + day + ".json", timeout=20)
            if r.status_code != 200:
                continue
            d = r.json()
        except Exception:
            continue                     # 那天云端没跑 / 网络不通，跳过即可
        got = 0
        for p in d.get("papers", []):
            t = (p.get("title") or "").strip().lower()
            if t and t not in seen:
                seen.add(t)
                p["from_harvest"] = day
                cand.append(p)
                got += 1
        consumed.add(day)
        marks.append("%s(+%d)" % (day, got))
        added += got
    if marks:
        _save_consumed(consumed)
        print("  [云端候选] 回捞 %d 篇：%s" % (added, ", ".join(marks)))
    else:
        print("  [云端候选] 无新的可回捞（或仓库不可达）")
    return cand


# ══════════════════════════════════════════════
# 8. 主流程 (V3.0)
# ══════════════════════════════════════════════

def main():
    # ★ 云端模式：只攒候选就退出（不打分、不发邮件、不下载）
    if HARVEST_MODE:
        return run_harvest()

    print("\n" + "🌟" * 30)
    print("  Geo_Paper_Radar V3.0 — 地学文献雷达启动")
    print("  数据源: RSS + OpenAlex  |  过滤: 两阶段  |  双轨制筛选")
    print("🌟" * 30 + "\n")

    start_time = time.time()
    all_papers = []

    # ==========================
    # 阶段 A: 双数据源抓取
    # ==========================

    # A1: RSS 抓取
    rss_papers = fetch_papers_from_rss()
    if rss_papers:
        all_papers.extend(rss_papers)

    # A2: OpenAlex 抓取
    oa_fetcher = OpenAlexFetcher(mailto=SMTP_SENDER)
    oa_papers = oa_fetcher.fetch_papers()
    if oa_papers:
        all_papers.extend(oa_papers)

    # A3: ★ Crossref 抓取（2026-10-01 新增）—— 实测 91% 是 OpenAlex 没有的
    if USE_CROSSREF:
        try:
            import sys as _sys
            if BASE_DIR not in _sys.path:
                _sys.path.insert(0, BASE_DIR)
            import sources as _src
            print("\n" + "=" * 60)
            print("【Crossref 源】主题检索 + 期刊目录（替代已死的 RSS）")
            print("=" * 60)
            cr = _src.fetch_crossref(CROSSREF_QUERIES,
                                     days=OPENALEX_DAYS_LOOKBACK,
                                     rows=CROSSREF_ROWS)
            cr += _src.fetch_crossref_journals(days=OPENALEX_DAYS_LOOKBACK)
            all_papers.extend(cr)
            print(f"  [Crossref] 合计 {len(cr)} 篇")
        except Exception as e:
            print(f"  [警告] Crossref 抓取失败：{type(e).__name__}: {e}")
    if USE_ARXIV:
        try:
            import sources as _src
            all_papers.extend(_src.fetch_arxiv(
                ['all:"preferential flow"', 'all:"slope stability"'], 60))
        except Exception as e:
            print(f"  [警告] arXiv 抓取失败：{e}")

    # ★ 2026-10-01：把云端（GitHub Actions）在笔记本睡着时攒下的候选合并进来。
    #   放在"无数据就退出"之前——万一本地网络抽风抓不到，云端攒的还是能兜住。
    all_papers = merge_harvest(all_papers)

    if not all_papers:
        print("\n[结果] 所有数据源均无新文献，任务结束")
        return

    # 全局去重
    seen = set()
    deduped_global = []
    for p in all_papers:
        t = p.get("title", "").strip().lower()
        if t and t not in seen:
            seen.add(t)
            deduped_global.append(p)

    total = len(deduped_global)
    rss_count = sum(1 for p in deduped_global if p.get("data_source") == "RSS")
    oa_count = sum(1 for p in deduped_global if p.get("data_source") == "OpenAlex")
    print(f"\n{'=' * 60}")
    print(f"📊 全局合并: RSS {rss_count} 篇 + OpenAlex {oa_count} 篇 = {total} 篇")
    print(f"{'=' * 60}")

    if not deduped_global:
        print("\n[结果] 合并后无有效文献，任务结束")
        return

    # ==========================
    # 阶段 B: 两阶段过滤
    # ==========================

    # B1: 第一层 — 本地 Regex 粗筛
    coarse_papers = local_regex_coarse_filter(deduped_global)
    if not coarse_papers:
        print("\n[结果] 粗筛后无文献通过，任务结束")
        return

    # B2: ★ 全局去重（2026-10-01 新增）
    # 为什么必须在打分之前：OpenAlex 回看 7 天 ⇒ 同一篇会连续 7 天进候选池。
    # 以前每天重下重命名 ⇒ Library 里 xxx.pdf / xxx (1).pdf / xxx (2).pdf 一路堆。
    # 放在这里还能顺手省掉重复打分的时间。
    try:
        import sys as _sys
        if BASE_DIR not in _sys.path:
            _sys.path.insert(0, BASE_DIR)
        import processed
        coarse_papers = processed.filter_new(coarse_papers)
    except Exception as e:
        print(f"  [警告] 去重表不可用，本轮按不去重处理：{e}")

    # B3: 限流
    # ★ 2026-10-01：本地打分免费 ⇒ 上限从 40 放开到 MAX_CANDIDATES=500
    #   （DeepSeek 路径仍按 MAX_DEEPSEEK_INPUT=40 卡住，防止 API 费用暴涨）
    _cap = MAX_CANDIDATES if SCORER == "local" else MAX_DEEPSEEK_INPUT
    deepseek_input = limit_for_deepseek(coarse_papers, _cap)

    # B4: ★ 摘要补全（2026-10-01 新增）
    # 只对【将要打分的那些】做，避免为低产出耗时间。
    # 顺序：先 Crossref（便宜、快），再网页（慢、且只在 Copernicus 类平台有效）。
    if ABSTRACT_ENRICH_CROSSREF or ABSTRACT_ENRICH_WEB:
        try:
            import sources as _src
            n0 = sum(1 for p in deepseek_input
                     if (p.get("summary") or "").startswith("No abstract"))
            print(f"\n  [摘要补全] 待打分 {len(deepseek_input)} 篇，"
                  f"其中无摘要 {n0} 篇")
            if n0:
                if ABSTRACT_ENRICH_CROSSREF:
                    _src.enrich_abstracts(deepseek_input,
                                          max_lookups=ABSTRACT_ENRICH_CROSSREF)
                if ABSTRACT_ENRICH_WEB:
                    _src.enrich_abstracts_web(deepseek_input,
                                              max_lookups=ABSTRACT_ENRICH_WEB)
                n1 = sum(1 for p in deepseek_input
                         if (p.get("summary") or "").startswith("No abstract"))
                print(f"  [摘要补全] 无摘要 {n0} → {n1} 篇")
        except Exception as e:
            print(f"  [警告] 摘要补全失败：{type(e).__name__}: {e}")

    # ==========================
    # 阶段 C: DeepSeek 打分 + 双轨制
    # ==========================

    # C1: DeepSeek 细筛
    scored_papers = score_all_papers(deepseek_input, phase_label="细筛")
    if not scored_papers:
        print("\n[结果] DeepSeek 打分全部失败，任务结束")
        return

    # C2: 双轨制筛选
    pass_list, browsing_list = dual_track_filter(scored_papers)

    # ★ 2026-10-01：把打过分的一律登记（不管分高分低），下次不再重复打分
    try:
        import processed as _proc
        _proc.mark_many(scored_papers, "seen")
    except Exception as e:
        print(f"  [警告] 去重表登记失败：{e}")

    # ==========================
    # 阶段 D: .ris 生成
    # ==========================

    ris_generated = 0
    ris_files = []  # 收集所有 .ris 文件路径，用于邮件附件
    if pass_list:
        print(f"\n{'=' * 60}")
        print("【EndNote 联动】生成 .ris 引文文件")
        print("=" * 60)
        for p in pass_list:
            fp = generate_ris_file(p)
            if fp:
                ris_generated += 1
                ris_files.append(fp)
                ds = p.get("data_source", "?")
                print(f"  ✅ [{ds}] {os.path.basename(fp)}")

    if browsing_list:
        print(f"\n{'=' * 60}")
        print("【备选泛读列表】（仅终端 + .ris，不发送邮件）")
        print("=" * 60)
        for idx, p in enumerate(browsing_list, 1):
            ts = p.get("total_score", 0)
            mi = p.get("method_innovation", 0)
            ds = p.get("data_source", "?")
            title = p.get("title", "")[:70]
            print(f"  {idx}. [{ds}][{ts}/40] {title} (创新:{mi}/10)")
            fp = generate_ris_file(p)
            if fp:
                ris_generated += 1
                ris_files.append(fp)
                print(f"     📄 {os.path.basename(fp)}")

    print(f"\n  [汇总] 共生成 {ris_generated} 个 .ris 文件")

    # ==========================
    # 阶段 D2: ★ OA PDF 自动下载（2026-10-01 新增）
    # ==========================
    # 下到 PDF_Inbox ⇒ EndNote 的「PDF 自动导入文件夹」会自动入库，
    # 从而免去"去 QQ 邮箱手动下载 .ris 再导入"这一步。
    pdf_saved, filed_keys = download_all_oa_pdfs(pass_list + browsing_list)

    # ==========================
    # 阶段 F2: ★ 待下载清单（2026-10-01 新增）
    # ==========================
    # 高分但【没能自动拿到 PDF】的（闭源，或 Wiley/MDPI 的 403 拦截）
    # ⇒ 出一份含 DOI 的清单；你手动下载后丢进 E:\论文\手动下载\，
    #   跑一次 manual_ingest.py 就自动重命名+归类+进 EndNote。
    dl_csv, dl_n = None, 0
    try:
        import sys as _sys
        if BASE_DIR not in _sys.path:
            _sys.path.insert(0, BASE_DIR)
        import library_manager as _lm2
        _lm2.ensure_dirs()
        dl_csv, dl_n = _lm2.build_download_list(pass_list + browsing_list,
                                                filed_keys)
        if dl_csv:
            # ★ 登记为 listed，避免同一篇明天又被列一次
            try:
                import processed as _proc
                for _p in (pass_list + browsing_list):
                    if _proc.key_of(_p) not in filed_keys:
                        _proc.mark(_p, "listed")
                _proc._save()
            except Exception:
                pass
            print(f"\n📥 待下载清单（{dl_n} 篇）：{dl_csv}")
            print(f"   手动下载后丢进 {_lm2.MANUAL_DROP_DIR}，"
                  f"再跑 python manual_ingest.py")
    except Exception as e:
        print(f"\n[警告] 待下载清单生成失败：{type(e).__name__}: {e}")

    # ==========================
    # 阶段 F: ★ 文献简报（2026-10-01 新增）
    # ==========================
    # 告诉你"什么文献可能需要精读"——按总分排序，附四维分与中文理由
    digest_path = None
    try:
        import sys as _sys
        if BASE_DIR not in _sys.path:
            _sys.path.insert(0, BASE_DIR)
        import library_manager as _lm
        _lm.ensure_dirs()
        digest_path = _lm.build_digest(pass_list, browsing_list,
                                       len(scored_papers),
                                       time.time() - start_time)
        print(f"\n📋 文献简报：{digest_path}")
    except Exception as e:
        print(f"\n[警告] 简报生成失败：{type(e).__name__}: {e}")

    # ==========================
    # 阶段 E: 空转保护 + 邮件
    # ==========================

    if not pass_list:
        print("\n" + "=" * 60)
        print("【结果】今日无通关文献")
        if browsing_list:
            print(f"📖 有 {len(browsing_list)} 篇备选泛读已存 EndNote_Watch")
        print("📭 未发送邮件。")
        print("=" * 60)
    else:
        print(f"\n[进入] 准备推送 {len(pass_list)} 篇通关文献...")
        html_content = build_html_email_v3(pass_list)
        success = send_email(html_content, attachments=ris_files)
        if success:
            links = [make_link_key(p) for p in pass_list]
            save_history(links)
            print("✅ 历史记录已更新")

    # ==========================
    # 结束
    # ==========================

    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"🏁 V3.0 任务完成！总耗时: {elapsed:.1f} 秒")
    print(f"📊 RSS {rss_count} + OpenAlex {oa_count} → 粗筛 {len(coarse_papers)} "
          f"→ DeepSeek {len(scored_papers)} → 通关 {len(pass_list)} → 备选 {len(browsing_list)}")
    print(f"📬 邮箱: {SMTP_RECEIVER}  |  📁 .ris: {ENDNOTE_WATCH_DIR}")
    print(f"📥 OA PDF: {PDF_INBOX_DIR}（已存 {len(pdf_saved)} 篇）")
    if pdf_saved:
        print("   ↳ 若 EndNote 已配置「PDF 自动导入文件夹」指向该目录，将自动入库")
    if digest_path:
        print(f"📋 文献简报: {digest_path}")
    if dl_csv:
        print(f"📥 待下载清单: {dl_csv}（{dl_n} 篇）")
    print(f"🧠 打分器: {SCORER}（local = 本地 Qwen3.5-9B，免费）")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()