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
MAX_CANDIDATES = None         # ★ 2026-10-01：设为 None = **不设上限，粗筛通过的全部送打分**
                              #   本地打分免费（1.1 s/篇），实测 1049 篇约 19 分钟，可接受。
                              #   想恢复上限就写个数字（如 800）。
                              #   注意：只有本地打分(SCORER=local)时才不限；
                              #   DeepSeek 路径仍按 MAX_DEEPSEEK_INPUT=40 卡住防费用失控。
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

# ---- ★ 2026-10-01 新增：学位论文 + 中文核心刊 ----
# 学位论文：OpenAlex 实测近 3 年 landslide rainfall 497 篇、preferential flow soil
#   1,153 篇，绝大多数带摘要 ⇒ 值得收。相关性匹配较松（会混进化学/医学的），
#   但打分免费，交给本地模型筛。
# 中文核心刊：**只能靠 ISSN 查 OpenAlex**（Crossref 几乎没有）。
#   ⚠️ OpenAlex 的 language 字段不可靠（中文刊常标成 en）⇒ 用 issn 不用 language。
#   ⚠️ 只能到"题目级别"：近半年收录有滞后，且无摘要、标题是英译。
USE_DISSERTATIONS = True
DISSERTATION_QUERIES = [
    "landslide rainfall slope",
    "preferential flow soil macropore",
    "slope stability unsaturated soil",
    "rainfall infiltration slope failure",
]
DISSERTATION_DAYS = 365       # 学位论文回看 1 年
DISSERTATIONS_PER_QUERY = 60
USE_CN_JOURNALS = True
CN_JOURNAL_DAYS = 90

# ---- ★ 摘要补全 ----
# 背景：闭源论文在 OpenAlex 的摘要覆盖只有约 24%（Elsevier/Wiley 不交摘要给 Crossref）。
#   · Crossref 补：对这一领域实测 0/25 —— Elsevier/Springer 根本没交，白搭但便宜
#   · 网页补    ：只对 Copernicus 这类平台有效，Elsevier/Springer 有反爬
# ⇒ 两条都留着但设上限，别为低产出耗时间。真正的希望是 Semantic Scholar（需 key）。
ABSTRACT_ENRICH_CROSSREF = 150  # 去 Crossref 查几篇（每篇约 1.0 s ⇒ 2.5 min）
ABSTRACT_ENRICH_WEB = 40        # 去出版社落地页抓几篇（每篇约 1.5 s ⇒ 1 min）
                                # ⚠ 对 Elsevier/Springer 实测【0 产出】，
                                #   只有 MDPI/Copernicus/Frontiers 这类能补到。
                                #   真正的解法是 S2（key 到位后接上）。
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
