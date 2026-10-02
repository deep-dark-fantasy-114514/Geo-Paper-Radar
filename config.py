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
import shutil
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
# 空串容错：.env 模板里常见 SMTP_PORT=（空）⇒ int("") 会在【导入阶段】直接崩
_smtp_port = os.getenv("SMTP_PORT", "").strip()
SMTP_PORT = int(_smtp_port) if _smtp_port.isdigit() else 465
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
# ★ 2026-10-02：单次请求超时（秒）。无人值守任务必须有它 ——
#   否则一次连接卡住就会把整轮打分拖死（sources.py 里每个 requests 都有 timeout）。
DEEPSEEK_TIMEOUT = 90

# ---- 筛选阈值（双轨制） ----
TOTAL_SCORE_PASS = 30       # 轨道A：总分 ≥ 30/40
INNOVATION_PASS = 9         # 轨道B：单项创新分 ≥ 9/10
BROWSING_THRESHOLD = 24     # 备选泛读门槛：总分 ≥ 24/40
MAX_EMAIL_RESULTS = 15     # 邮件正文里列几篇（正文越短越好读）
# ★ 2026-10-02：邮件上限只决定"发几篇"，**不再决定"留几篇"**。
#   超出上限的通关文献会进 overflow，照常进简报 / 待下载清单 / .ris，只是不发邮件。
#   同理，给"轨道B（创新分突出但总分未过线）"预留席位 ——
#   否则它们只是贴个 B 标签、再和所有 A 轨去争同一个总分榜，很容易一篇都进不去。
EMAIL_RESERVE_TRACK_B = 5
# ★ 2026-10-01：备选泛读原来也按 MAX_EMAIL_RESULTS=10 截断 ——
#   但泛读列表现在会进【简报 + 待下载清单】，截到 10 篇等于把
#   当天评出的其余优质泛读文献静默丢掉，损害文献库沉淀。单独给配额。
MAX_BROWSING_RESULTS = 60  # 备选泛读最多留几篇（进简报与下载清单）
MAX_DEEPSEEK_INPUT = 40     # 进入 DeepSeek 阶段的文献上限（防 API 费用暴涨）

# ---- 中文核心期刊 ISSN（★ 全仓唯一来源）----
# ★ 2026-10-01：`{刊名: ISSN}` 是权威表；`CHINESE_JOURNALS_ISSN` 由它派生。
#   原来 sources.py 里还另有一份 CN_JOURNAL_ISSN（同样三本），
#   等于"加了一本新刊、另一处不同步"的经典坑。
CHINESE_JOURNALS = {
    "岩土工程学报": "1000-4548",
    "岩石力学与工程学报": "1000-6915",
    "地球科学": "1000-2383",
    # 在此继续添加更多中文期刊
}
CHINESE_JOURNALS_ISSN = list(CHINESE_JOURNALS.values())

# ---- OpenAlex 核心检索词（★ 2026-10-01 从 sources.py 搬来）----
# 原来硬编码在 OpenAlexFetcher.fetch_papers() 里 ⇒ 别的源（Crossref / arXiv /
# 学位论文）都能在 config 调，唯独 OpenAlex 主源不能 —— 很容易出现
# "以为改配置改了检索策略，其实 OpenAlex 没变"。
# 注意：OpenAlex 空格 = AND，所以每个词尽量短。
OPENALEX_QUERIES = [
    "landslide",
    "slope stability",
    "rainfall infiltration",
    "preferential flow",
    "debris flow",
    "unsaturated soil",
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

# ══════════════════════════════════════════════
# ★ 2026-10-01：粗筛关键词分级（锚定词 vs 泛化词）
# ══════════════════════════════════════════════
# 背景：关键词表里混了一批【泛地学词】。原规则"命中 ≥1 个即通过"下，
#   一篇只研究"农业灌溉入渗"或"平原地下水超采"的论文，仅凭命中
#   infiltration / 地下水 就过关。实测 963 篇有摘要候选里，
#   **134 篇（13%）是只靠一个泛词混进来的纯噪音** —— 样例有
#   "城市湖泊降温建模"（命中 vegetation）、"机器人时间规整"（命中 stability analysis）、
#   "大肠杆菌污染"（命中 runoff + groundwater）。
#
# 改法：**锚定词×2 + 泛化词×1，达到 COARSE_MIN_SCORE 才算通过**
#   · 锚定词单独命中 = 2 分 ⇒ 过（如 "landslide"、"优先流"）
#   · 泛化词单独命中 = 1 分 ⇒ 不过（正是要砍的噪音）
#   · 两个泛化词   = 2 分 ⇒ 过（如 "groundwater seepage in slopes"，确实相关）
#   实测：275 → 173 篇，砍掉的正是那批噪音。
GENERIC_KEYWORDS = {
    # 英文
    "runoff", "infiltration", "soil water", "groundwater", "vegetation",
    "root system", "soil mechanics", "slope angle", "seepage",
    "stability analysis", "numerical simulation", "finite element",
    "early warning", "limit equilibrium", "factor of safety",
    # 中文
    "径流", "入渗", "地下水", "植被", "根系", "土力学",
    "稳定性", "渗流", "数值模拟", "有限元", "安全系数",
    "极限平衡", "稳定性分析", "预警", "加固", "水土保持", "护坡",
}

COARSE_MIN_SCORE = 2      # 加权分下限（锚定×2 + 泛化×1）

# ---- 历史记录 & EndNote ----
# ---- ★ 2026-10-01：研究画像 + 主题黑名单（定义在 research_profile.py，改那里即可）----
from research_profile import (RESEARCH_PROFILE, BLACKLIST_TOPICS,
                              BLACKLIST_TITLE_ONLY, QUALITATIVE_FIELDS,
                              SCORE_RUBRIC)

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

# ★ 2026-10-01 新增：PDF 暂存区。
# 为什么必须要有：PDF_Inbox 是 EndNote **实时监听**的自动导入目录，
# 而下载是【批处理】的（PDF_MAX_PER_RUN=20 一起下），重命名却要一篇篇
# 走视觉模型（render_pages + 一次多模态调用，5–15 s/篇）。
# 原来直接下进 PDF_Inbox ⇒ 第一个文件要在 EndNote 眼皮底下躺 2–5 分钟。
# 被 EndNote 抢先读走就加上排他锁，shutil.move 抛 PermissionError ⇒
# **该篇的中文重命名与 Library 归档全部丢失**，且次日不会再试。
# ⇒ 改为：下到 PDF_Temp → 重命名 → 归档 Library → 最后才单向复制进 PDF_Inbox。
PDF_STAGE_DIR = os.path.join(BASE_DIR, "PDF_Temp")
PDF_STAGE_KEEP_DAYS = 7          # 暂存区里超过这个天数的残留自动清掉
PDF_MAX_PER_RUN = 20             # 单次最多下几篇，防止失控
PDF_MAX_MB = 60                  # 单个 PDF 体积上限（MB）
PDF_MIN_BYTES = 20 * 1024        # 小于 20 KB 的多半是错误页，丢弃
# ★ 2026-10-01 新增：瞬时失败的重试策略。
#   只有 429 和 5xx、以及连接层异常（超时/重置/DNS）才重试；
#   403 / 404 是出版商在明确拒绝（Wiley / MDPI 的硬拦），重试纯属浪费时间。
#   顺带按服务器给的 Retry-After 走（上限 60 s）。
PDF_RETRIES = 2                  # 单次请求最多再试几次
PDF_RETRY_WAIT = 3               # 退避基数（秒）：3s → 6s

# ---- ★ 2026-10-01 新增：本地打分 + 自动归档 ----
# 用本地 Qwen3.5-9B 打分：0.7~1.1 s/篇、成本为 0 ⇒ 候选规模可以从 40 放开到几百
#
# ★★ 本地/云端自动判别 ★★
# GitHub Actions 跑在 ubuntu-latest 上，拿不到本机的 Qwen、视觉桥、Library 目录，
# 也没有 EndNote 的自动导入文件夹。所以按平台自动降级：
#   · 本机(Windows) → 完整流程：本地打分 + 下载 + 重命名 + 归档 + 简报
#   · 云端(Linux)   → 轻量流程：抓取 + DeepSeek 打分 + .ris + 邮件（老行为）
# 这样同一份代码两边都能跑，不用维护两个分支。
# 本机/云端判别。默认按平台猜，但**环境变量优先**，便于移植：
#   PAPER_RADAR_ENV=local | cloud
# ⚠️ 注意：即便在 Linux 上设成 local，本地打分仍会失败 —— 因为
#    ask_image.py 的路径是写死的 Windows 路径（C:\Users\zihao\...）。
#    要真正跨平台，得先把那几处路径也改成配置项。
LOCAL_MODE = (os.getenv("PAPER_RADAR_ENV", "").strip().lower()
              or ("local" if os.name == "nt" else "cloud")) == "local"
PDF_DOWNLOAD_ENABLED = LOCAL_MODE   # 云端下载了也没处放，直接关掉

# ---- ★ 2026-10-01：三个原先硬编码在 library_manager.py 里的外部路径 ----
#    原来写死了 E:\ 盘符和 C:\Users\zihao\ —— 换机器 / 换盘 / 上 Linux 就得改代码。
#    现在一律走配置，且**环境变量优先**：
#        PAPER_RADAR_MANUAL_DROP / PAPER_RADAR_RENAMER / PAPER_RADAR_ASK_IMAGE
MANUAL_DROP_DIR = os.getenv("PAPER_RADAR_MANUAL_DROP",
                            os.path.join(os.path.dirname(BASE_DIR), "手动下载"))
# PDF 重命名脚本（复用现成的独立工具，指向它的 .py 文件）
RENAMER = os.getenv("PAPER_RADAR_RENAMER",
                    os.path.join(os.path.dirname(BASE_DIR),
                                 "PDF_Renamer_Skill", "rename_pdfs_ai.py"))
# 本地视觉模型桥（看 PDF 标题页用）
ASK_IMAGE = os.getenv(
    "PAPER_RADAR_ASK_IMAGE",
    os.path.join(os.path.expanduser("~"), ".claude", "skills",
                 "local-vision", "scripts", "ask_image.py"))

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
# ★ 2026-10-01：查询词从 paper_radar.py 里搬过来 —— 原来 harvest.py 的
#   fetch_all_candidates() 是逐字拷贝的，却【漏掉了 arXiv 这一支】。
#   两处共用同一份查询词，以后不会各改各的。
ARXIV_QUERIES = ['all:"preferential flow"', 'all:"slope stability"']
ARXIV_PER_QUERY = 60

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
HARVEST_MIN_PER_SOURCE = 60   # ★ 2026-10-01 新增：每个来源【至少】保住几篇（不足则全留）
                              #   由来：`kept[:HARVEST_MAX]` 是【按列表顺序】截断的，
                              #   而学位论文 / 中文核心刊在 fetch_all_candidates()
                              #   里是【最后追加】的 ⇒ 实测每天被整源砍光：
                              #   48 篇学位论文 + 20 篇中文核心，一篇都进不了存档。
HARVEST_ABSTRACT_CHARS = 2000 # ★ 2026-10-01：600 → 2000（原值把摘要砍掉大半）
                              #   由来：本地打分按 summary[:2000] 喂模型
                              #   （scoring.py:194 / local_scorer.py:243），
                              #   云端却在 600 就截 ⇒ 模型永远看不到后半段。
                              #   实测存档中有摘要的 188 篇里 160 篇被截，
                              #   且常切在 "(i) temporal extraction," 这种要害处。
                              #   代价：约 +300 KB/天（412/600 本就是 "No abstract"）。
HARVEST_HTTP_TIMEOUT = 10     # 回捞单次超时（秒）。实测不通的镜像是在
                              # 【建连阶段就秒失败】，不是等超时；10 s 只是兜底。
HARVEST_NET_FAIL_TOLERANCE = 2  # 连续几天都取不回就判定「网络不通」并停止回捞
                              # （镜像全灭时另有一条立即退出的路径）
HARVEST_CONSUMED = os.path.join(BASE_DIR, ".harvest_consumed.json")

# ── 仓库 raw 地址（仓库是公开的，无需 token）──────────────────────────
# ★ 2026-10-01 实测：**raw.githubusercontent.com 在国内这条网上完全不可达**
#   —— requests 直接抛 SSLError(UNEXPECTED_EOF_WHILE_READING)，这正是
#   "回捞云端候选"这个功能一直没生效的根因（不是代码写错，是网到不了）。
#   实测可达的替代通道（本机，2026-10-01）：
#       gh-proxy.com   0.5 s   ✅
#       api.github.com 0.6 s   ✅（但匿名限流 60 次/小时）
#       ghproxy.net    1.1 s   ✅
#       cdn.jsdelivr   3.0 s   ⚠ 对 @main 有 12 h 缓存，当天更新的文件取不到 ⇒ 不用
#   因此改成【镜像链】，按顺序试，谁先通用谁；失败的当场剔除，本轮到尾
#   不会再等它。可用 PAPER_RADAR_REPO_RAW 覆盖（逗号分隔多个即自定义镜像链；
#   只给一个则不做回退）。
#   ⚠ 顺序按【本机实测速度】排，raw 放最后：它是直连地址，只有在挂了代理/
#     梯子时才通，否则每次都要白等一次超时。有 VPN 的把 PAPER_RADAR_REPO_RAW
#     设成 raw 那个地址即可。
_REPO_SLUG = "deep-dark-fantasy-114514/Geo-Paper-Radar"
_REPO_BRANCH = "main"
_GH_RAW = f"https://raw.githubusercontent.com/{_REPO_SLUG}/{_REPO_BRANCH}/harvest/"

REPO_RAW_MIRRORS = [u.strip() for u in os.getenv(
    "PAPER_RADAR_REPO_RAW",
    ",".join(["https://gh-proxy.com/" + _GH_RAW,      # 实测 0.5 s
              "https://ghproxy.net/" + _GH_RAW,       # 实测 1.1 s
              _GH_RAW])).split(",") if u.strip()]     # 直连（需代理）

# 兼容旧名：仍指向【首选】地址
REPO_RAW = REPO_RAW_MIRRORS[0]

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
    """★ 2026-10-01：改为**原子写入**（写 .tmp 再 os.replace）。

    原来直接 open(..., "w") —— 定时任务被强杀 / 笔记本合盖关机时，
    如果恰好卡在写文件那一瞬，history.json 会被截断成 0 字节或坏 JSON，
    之后所有 load_history() 全部失败。
    `processed.py` 早就是原子写（那是从 COMSOL safe_save 白跑 11.8 小时的教训来的），
    这里保持一致。
    """
    existing = load_history()
    existing.update(links)
    tmp = HISTORY_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pushed": list(existing)}, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, HISTORY_FILE)
    except Exception as e:
        print(f"  [Warning] 写入历史记录失败: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


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


# Windows 保留设备名（这些名字做文件名会被系统拒绝）
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
                 "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2",
                 "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}


def safe_filename(text, max_len=40):
    """清洗成可用的文件名片段。

    ★ 2026-10-01 补两处 Windows 边界：
      · **末尾的点和空格**：Windows 会静默吃掉，导致实际文件名与预期不符
      · **保留设备名**（CON/PRN/AUX/NUL/COM1…）：系统直接拒绝创建
    """
    safe = re.sub(r'[\\/*?:"<>|]', "", text)
    if len(safe) > max_len:
        safe = safe[:max_len]
    safe = safe.strip().rstrip(". ")          # 末尾的点/空格会被 Windows 吃掉
    if safe.upper() in _WIN_RESERVED:         # 撞保留名 ⇒ 加前缀
        safe = "_" + safe
    return safe


def canonical_doi(doi):
    """DOI 规范化：URL/`doi:` 前缀、末尾标点、查询串、大小写外的空白。

    ★ 2026-10-01：**全仓唯一实现**。原来各处各写一遍，且能力参差：
      · `processed.key_of` / `library_manager._clean_doi` 只剥 `https?://(dx.)?doi.org/`
      · 出版商的元数据里还常见 `doi:10.xxxx/yyy`、`10.xxxx/yyy.`（句末点）、
        `10.xxxx/yyy).`（参考文献里带括号的）、`?param=1` 尾巴 —— 全都不处理。
      `key_of` 用 `.lower()` 后的结果当键，CSV/RIS/待下载清单用的是展示形式，
      所以这里**只做规范化、不转大小写**，由调用方决定。

    返回裸 DOI（如 `10.1016/j.enggeo.2026.107001`），取不到就返回 ""。
    """
    d = str(doi or "").strip()
    if not d:
        return ""
    d = re.sub(r"^\s*(?:doi\s*:\s*)", "", d, flags=re.I)          # doi:10.x/y
    d = re.sub(r"^\s*https?://(?:dx\.)?doi\.org/", "", d, flags=re.I)
    d = d.split("?", 1)[0].split("#", 1)[0]                        # 去查询串/锚点
    d = d.strip().strip(" \t\r\n.,;:)]}。，")               # 去首尾标点
    # 只认 `10.xxxx/...` 这种形状。取不到就返回 "" ——
    # 这样 `key_of` 会退回按标题去重，而不是拿一段乱码当唯一键。
    return d if re.match(r"^10\.\d{4,}/\S+$", d) else ""


def no_abstract(p_or_summary):
    """判断"这篇没有摘要"。

    ★ 2026-10-01：**全仓唯一实现**。原来 9 处各写一遍，其中只有
      `filters._no_abstract` 那份带 `.strip()`，另外 8 处（local_scorer /
      scoring / paper_radar / sources / output）都是裸 `startswith`。
      摘要字段里混入前导换行或空格时（Crossref 的 JATS 剥离、XML 转换、
      人工粘贴都可能产生 `"\\nNo abstract available"`），那 8 处会把它
      当成"有摘要"，送去跑四维打分 ⇒ **凭空产出一个虚假分数**。

    兼容两种入参：paper 字典，或摘要字符串本身。
    """
    s = p_or_summary
    if isinstance(s, dict):
        s = s.get("summary")
    s = (s or "").strip()
    return (not s) or s.startswith("No abstract")


def norm_title(title):
    """标题归一化：**剥离 XML 标签 → 压空白 → 去标点 → 转小写**。

    ★ 2026-10-01：这是全仓唯一实现。原来三处各写一份，已经漂了 ——
      `manual_ingest._norm_title` 漏掉"剥标签"这一步，而 `sources._norm_title_key`
      和 `processed.key_of` 有。三份算出三把键，跨清单匹配就可能对不上。

    ⚠️ 处理顺序很关键：**必须先剥标签再删标点**。反过来会把标签里的字母留下 ——
      实测 `<i>Preferential flow</i> in slopes` 会被规范化成
      `ipreferential flowi in slopes`，从而漏判重复。

    调用方：`processed.key_of` / `sources._norm_title_key` / `manual_ingest`。
    """
    t = (title or "").strip().lower()
    t = re.sub(r"<[^>]+>", " ", t)            # 先剥标签（否则字母残留）
    t = re.sub(r"\s+", " ", t)                # 压空白
    t = re.sub(r"[^0-9a-z一-鿿 ]", "", t)     # 去标点
    return t.strip()


def unique_path(path):
    """重名不覆盖：`x.pdf` 已存在就退到 `x (1).pdf`。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{stem} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def atomic_copy_into(src, dst_dir, name=None):
    """把 src 复制进 dst_dir，**先写 .tmp 再 os.replace**，返回最终路径。

    ★ 2026-10-01：抽出来的唯一理由 —— `PDF_Inbox` 是 **EndNote 实时监听**的
      自动导入目录，往里裸写 `shutil.copy2` 有真实风险：地学论文 PDF 常带
      高分辨率遥感/航拍图，几十 MB 要写几百毫秒到数秒；EndNote 若在写完之前
      介入，轻则读取报错，重则**把半截 PDF 导进文献库**。
      本仓库的 download.py 早就是 `.downloading` + `os.replace` 的写法，
      但另外三处（library_manager / manual_ingest / download 的回填）一直是裸拷。

      用法统一走这一个函数，别再各处手写。
    """
    os.makedirs(dst_dir, exist_ok=True)
    target = unique_path(os.path.join(dst_dir, name or os.path.basename(src)))
    tmp = target + ".tmp"
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo, 1024 * 256)
            fo.flush()
            os.fsync(fo.fileno())
        os.replace(tmp, target)          # 原子换名：EndNote 只会看到完整文件
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise
    return target


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


