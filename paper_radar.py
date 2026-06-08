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

import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI

# ──────────────────────────────────────────────
# 0. 加载环境变量
# ──────────────────────────────────────────────
load_dotenv()

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
    "https://link.springer.com/search.rss?facet-journal-id=10346&channel-name=Landslides",
    "https://rss.sciencedirect.com/publication/science/00137952",
    "https://rss.sciencedirect.com/publication/science/0169555X",
    "https://agupubs.onlinelibrary.wiley.com/action/showFeed?jc=1944-7973&type=etoc&feed=rss"
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
            doi = work.get("doi", "") or ""
            openalex_url = work.get("id", "") or ""
            primary_location = work.get("primary_location", {}) or {}
            pdf_url = primary_location.get("landing_page_url", "") or ""

            link = doi or pdf_url or openalex_url

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

def local_regex_coarse_filter(papers):
    """
    第一层：本地 Regex 粗筛
    规则：标题或摘要中命中至少 2 个核心关键词（中英文任一）
    目的：300 篇 → 约 20-30 篇，减少 DeepSeek API 调用
    """
    print("\n" + "=" * 60)
    print("【第一阶段】本地 Regex 粗筛")
    print("=" * 60)

    # 编译所有关键词为正则（忽略大小写）
    all_keywords = KEYWORD_LIST_EN + KEYWORD_LIST_CN
    # 按长度降序排列以确保长词优先匹配
    all_keywords_sorted = sorted(all_keywords, key=len, reverse=True)

    passed = []
    for idx, paper in enumerate(papers, 1):
        title = paper.get("title", "")
        summary = paper.get("summary", "")
        text = (title + " " + summary).lower()

        # 统计命中关键词数
        hit_count = 0
        hit_words = []
        for kw in all_keywords_sorted:
            if kw.lower() in text:
                hit_count += 1
                hit_words.append(kw)
                if hit_count >= 2:  # 命中 2 个即满足条件
                    break

        if hit_count >= 2:
            paper["regex_hits"] = hit_words[:5]  # 记录前 5 个命中词
            passed.append(paper)

    print(f"  [输入] {len(papers)} 篇 → 粗筛后 {len(passed)} 篇")
    print(f"  [规则] 标题/摘要命中 ≥2 个核心关键词")
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
    """对文献列表进行 DeepSeek 打分"""
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


def send_email(html_content):
    print("\n" + "=" * 60)
    print("【发送邮件】")
    print("=" * 60)
    if not all([SMTP_SENDER, SMTP_PASSWORD, SMTP_RECEIVER]):
        print("  [Error] 邮箱配置不完整")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🌍 地学前沿推送 V3.0 — {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = SMTP_SENDER
    msg["To"] = SMTP_RECEIVER
    msg.attach(MIMEText(html_content, "html", "utf-8"))
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
# 8. 主流程 (V3.0)
# ══════════════════════════════════════════════

def main():
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

    # B2: 限流（防止 API 费用暴涨）
    deepseek_input = limit_for_deepseek(coarse_papers, MAX_DEEPSEEK_INPUT)

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

    # ==========================
    # 阶段 D: .ris 生成
    # ==========================

    ris_generated = 0
    if pass_list:
        print(f"\n{'=' * 60}")
        print("【EndNote 联动】生成 .ris 引文文件")
        print("=" * 60)
        for p in pass_list:
            fp = generate_ris_file(p)
            if fp:
                ris_generated += 1
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
                print(f"     📄 {os.path.basename(fp)}")

    print(f"\n  [汇总] 共生成 {ris_generated} 个 .ris 文件")

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
        success = send_email(html_content)
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
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()