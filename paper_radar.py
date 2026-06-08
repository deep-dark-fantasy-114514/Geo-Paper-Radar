#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Geo_Paper_Radar V2.0 — 全自动地学文献 RSS 抓取与 AI 筛选推送系统
============================================================
核心升级：
  1. RSS 抓取（伪装 UA + 重试容错）
  2. DeepSeek API 多维度智能打分（4维度各10分 + JSON 严格输出）
  3. 双轨制筛选（总分达标 / 单项创新突破）+ 备选泛读列表
  4. EndNote 联动：自动生成 .ris 引文文件
  5. HTML 邮件推送（QQ邮箱 SSL）
  6. 空转保护：无强相关文献时不发邮件
"""

import os
import json
import time
import hashlib
import smtplib
import traceback
import re
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

# 期刊 RSS 源列表 —— 请替换为你确认的链接
RSS_SOURCES = [
    # 1. Landslides (滑坡领域顶级期刊 - Springer 托管)
    "https://link.springer.com/search.rss?facet-journal-id=10346&channel-name=Landslides",

    # 2. Engineering Geology (工程地质顶级期刊 - Elsevier 托管)
    "https://rss.sciencedirect.com/publication/science/00137952",

    # 3. Geomorphology (地貌与斜坡地质 - Elsevier 托管)
    "https://rss.sciencedirect.com/publication/science/0169555X",

    # 4. Water Resources Research (水文与降雨入渗 - Wiley/AGU 托管)
    "https://agupubs.onlinelibrary.wiley.com/action/showFeed?jc=1944-7973&type=etoc&feed=rss"
]

# DeepSeek 配置
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# 筛选阈值（V2.0 双轨制）
TOTAL_SCORE_PASS = 30       # 轨道A：总分 ≥ 30/40 (= 旧版 75/100)
INNOVATION_PASS = 9         # 轨道B：单项创新分 ≥ 9/10
BROWSING_THRESHOLD = 24     # 备选泛读门槛：总分 ≥ 24/40 (= 旧版 60/100)
MAX_EMAIL_RESULTS = 10      # 最终邮件最多保留篇数

# 历史记录文件
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")

# EndNote Watch 文件夹（存放 .ris 引文文件）
ENDNOTE_WATCH_DIR = os.path.join(BASE_DIR, "EndNote_Watch")

# 抓取窗口（小时）
FETCH_HOURS = 24

# 请求超时（秒）
REQUEST_TIMEOUT = 30


# ──────────────────────────────────────────────
# 2. 工具函数
# ──────────────────────────────────────────────

def get_chrome_headers():
    """伪装成 Chrome 浏览器的请求头"""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64 x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def fetch_rss_with_retry(url, max_retries=3):
    """
    带重试机制的 RSS 抓取
    返回 feedparser 解析后的对象，失败返回 None
    """
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


def load_history():
    """加载已推送文献的历史记录（set of link）"""
    if not os.path.exists(HISTORY_FILE):
        return set()
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("pushed", []))
    except (json.JSONDecodeError, FileNotFoundError):
        return set()


def save_history(links):
    """将已推送文献链接追加到历史记录"""
    existing = load_history()
    existing.update(links)
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"pushed": list(existing)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [Warning] 写入历史记录失败: {e}")


def make_link_key(entry):
    """从 entry 中提取唯一标识（优先 Link，回退为 title hash）"""
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
    """判断 RSS entry 是否在指定小时内发布"""
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if not published:
        return True
    pub_time = datetime(*published[:6], tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - pub_time) <= timedelta(hours=hours)


def safe_filename(text, max_len=40):
    """将文本转为安全的文件名"""
    # 移除非法文件名字符
    safe = re.sub(r'[\\/*?:"<>|]', "", text)
    # 截断
    if len(safe) > max_len:
        safe = safe[:max_len]
    return safe.strip()


def infer_journal_name(url):
    """从 RSS URL 推断期刊名称"""
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


# ──────────────────────────────────────────────
# 3. 模块一：RSS 抓取
# ──────────────────────────────────────────────

def fetch_papers_from_rss():
    """
    从所有 RSS 源抓取过去 24h 的文献
    返回: list[dict] 每篇包含 title, link, summary(摘要), source(期刊名)
    """
    all_papers = []
    print("=" * 60)
    print("【模块一】RSS 文献抓取")
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
            })
            count += 1

        print(f"  [完成] {name}: 获取 {count} 篇新文献")

    # 标题去重
    seen_titles = set()
    unique_papers = []
    for p in all_papers:
        t = p["title"].strip().lower()
        if t not in seen_titles:
            seen_titles.add(t)
            unique_papers.append(p)

    print(f"\n[汇总] 共抓取 {len(all_papers)} 篇，去重后 {len(unique_papers)} 篇")
    return unique_papers


# ──────────────────────────────────────────────
# 4. 模块二：DeepSeek API 多维度智能打分 (V2.0)
# ──────────────────────────────────────────────

def build_deepseek_v2_prompt(title, abstract):
    """
    V2.0 多维度 Prompt
    4 个维度各 0-10 分，总分 0-40
    """
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
    """
    调用 DeepSeek API 给一篇文献打分 (V2.0 多维版)
    返回: dict 或 None（失败时）
    """
    system_prompt, user_prompt = build_deepseek_v2_prompt(title, abstract)

    try:
        client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
        )

        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=300,
        )

        content = response.choices[0].message.content.strip()

        # 提取 JSON
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1:
            content = content[start:end + 1]

        result = json.loads(content)

        # 验证必要字段
        required_fields = [
            "slope_stability", "rainfall_infiltration",
            "preferential_flow", "method_innovation",
            "total_score", "reason", "tldr"
        ]
        for field in required_fields:
            if field not in result:
                raise ValueError(f"返回 JSON 缺少字段: {field}")

        # 整数化 + 钳制
        for dim in ["slope_stability", "rainfall_infiltration", "preferential_flow", "method_innovation"]:
            result[dim] = max(0, min(10, int(result[dim])))

        result["total_score"] = max(0, min(40, int(result["total_score"])))

        # 自动计算推荐等级
        ts = result["total_score"]
        if ts >= TOTAL_SCORE_PASS:
            result["recommendation"] = "strong"
        elif ts >= BROWSING_THRESHOLD:
            result["recommendation"] = "normal"
        else:
            result["recommendation"] = "weak"

        # 兼容旧版：计算映射总分（0-100）
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


def score_all_papers(papers):
    """
    对所有文献进行 DeepSeek V2.0 多维度打分
    返回: list[dict] 每篇增加 slope_stability, rainfall_infiltration, etc.
    """
    print("\n" + "=" * 60)
    print("【模块二】DeepSeek AI 多维度智能打分 (V2.0)")
    print("=" * 60)

    scored = []
    total = len(papers)

    for idx, paper in enumerate(papers, 1):
        title = paper["title"]
        abstract = paper["summary"]
        source = paper["source"]

        print(f"\n[进度] ({idx}/{total}) 正在打分 [{source}]: {title[:60]}...")

        result = score_paper_with_deepseek(title, abstract)

        if result is None:
            print(f"  [跳过] 该篇打分失败，已跳过")
            continue

        # 合并结果
        paper.update(result)
        scored.append(paper)

        dims = (f"S:{result['slope_stability']} "
                f"R:{result['rainfall_infiltration']} "
                f"P:{result['preferential_flow']} "
                f"M:{result['method_innovation']}")
        print(f"  [得分] 总分 {result['total_score']}/40 {dims} | {result['reason']}")

        time.sleep(0.5)

    print(f"\n[汇总] 成功打分 {len(scored)}/{total} 篇")
    return scored


# ──────────────────────────────────────────────
# 5. 新增模块：EndNote .ris 文件生成 (V2.0)
# ──────────────────────────────────────────────

def generate_ris_file(paper):
    """
    为一篇文献生成 .ris 格式引文文件
    返回: str 文件路径，失败返回 None
    """
    # 确保目录存在
    os.makedirs(ENDNOTE_WATCH_DIR, exist_ok=True)

    try:
        title = paper.get("title", "Untitled")
        score = paper.get("total_score", 0)
        tldr = paper.get("tldr", "")
        link = paper.get("link", "")
        source = paper.get("source", "")
        summary = paper.get("summary", "")

        # 从摘要中尝试提取作者（RSS 中的 dc:creator）
        authors_raw = paper.get("authors", "")
        if isinstance(authors_raw, list):
            authors = [a.get("name", "") for a in authors_raw if isinstance(a, dict)]
        else:
            authors = []

        # 尝试从 summary HTML 中提取作者
        if not authors:
            author_match = re.search(r'(?:Author|Authors?|creator)[:：]\s*([^.<]+)', summary)
            if author_match:
                authors = [a.strip() for a in author_match.group(1).split(",")]

        # 构建 .ris 内容
        ris_lines = ["TY  - JOUR"]
        ris_lines.append(f"TI  - {title}")

        for author in authors[:10]:  # 最多 10 个作者
            if author:
                ris_lines.append(f"AU  - {author}")

        ris_lines.append(f"PY  - {datetime.now().year}//")
        ris_lines.append(f"JO  - {source}")
        if link:
            ris_lines.append(f"UR  - {link}")
            doi_match = re.search(r'10\.\d{4,}/[\w\.\-]+', link)
            if doi_match:
                ris_lines.append(f"DO  - {doi_match.group(0)}")

        # 摘要
        ab_clean = re.sub(r'<[^>]+>', '', summary).strip()
        if ab_clean:
            ris_lines.append(f"AB  - {ab_clean}")

        # 关键词
        ris_lines.append("KW  - Geo_Paper_Radar")
        ris_lines.append(f"KW  - Score:{score}/40")
        if tldr:
            ris_lines.append("N1  - " + tldr)

        ris_lines.append("ER  - ")
        ris_content = "\n".join(ris_lines) + "\n"

        # 安全文件名
        safe_title = safe_filename(title, 40)
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"{date_str}_{score}分_{safe_title}.ris"
        filepath = os.path.join(ENDNOTE_WATCH_DIR, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(ris_content)

        return filepath

    except Exception as e:
        print(f"  [Warning] 生成 .ris 文件失败: {e}")
        return None


# ──────────────────────────────────────────────
# 6. 模块三：双轨制筛选 + HTML 邮件 (V2.0)
# ──────────────────────────────────────────────

def dual_track_filter(papers):
    """
    V2.0 双轨制筛选
    返回: (pass_list, browsing_list)
      - pass_list: 通关文献（总分≥30 或 创新≥9）
      - browsing_list: 备选泛读（未通关但总分≥24）
    """
    print("\n" + "=" * 60)
    print("【模块三】双轨制筛选 (V2.0)")
    print("=" * 60)

    # 按总分降序
    papers.sort(key=lambda x: x.get("total_score", 0), reverse=True)

    # 去重
    history = load_history()
    deduped = []
    skipped = 0
    for p in papers:
        link_key = make_link_key(p)
        if link_key in history:
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

        # 轨道A：总分达标
        track_a = (ts >= TOTAL_SCORE_PASS)
        # 轨道B：单项创新突破
        track_b = (mi >= INNOVATION_PASS)

        if track_a or track_b:
            p["pass_track"] = "A" if track_a and not track_b else ("B" if track_b and not track_a else "A+B")
            pass_list.append(p)
        elif ts >= BROWSING_THRESHOLD:
            browsing_list.append(p)

    # 取 Top
    pass_list = pass_list[:MAX_EMAIL_RESULTS]
    browsing_list = browsing_list[:MAX_EMAIL_RESULTS]

    print(f"  [轨道A] 总分≥{TOTAL_SCORE_PASS}/40: {sum(1 for p in pass_list if 'A' in p.get('pass_track',''))} 篇")
    print(f"  [轨道B] 创新分≥{INNOVATION_PASS}/10: {sum(1 for p in pass_list if 'B' in p.get('pass_track',''))} 篇")
    print(f"  [通关] {len(pass_list)} 篇 → 推送邮件 + 生成 .ris")
    print(f"  [备选] {len(browsing_list)} 篇 → 仅终端打印 + 生成 .ris")

    return pass_list, browsing_list


def build_html_email_v2(papers):
    """
    V2.0 HTML 邮件正文（展示多维度分数）
    """
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

        # 总分颜色
        pct = round(ts / 40 * 100)
        if pct >= 90:
            score_color = "#e74c3c"
        elif pct >= 75:
            score_color = "#e67e22"
        else:
            score_color = "#27ae60"

        # 轨道徽章
        track_badge = {"A": "📐 总分达标", "B": "💡 创新突破", "A+B": "🏆 双轨通关"}.get(track, "✅ 通关")

        # 维度条（用 emoji 表示星级）
        def dim_bar(val):
            return "⭐" * val + "☆" * (10 - val)

        cards_html += f"""
        <div style="background:#ffffff; border:1px solid #e0e0e0; border-radius:12px; padding:20px; margin-bottom:16px; box-shadow:0 2px 8px rgba(0,0,0,0.06);">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;">
                <div>
                    <span style="display:inline-block; background:{score_color}; color:#fff; font-weight:bold; font-size:18px; padding:4px 14px; border-radius:20px; margin-right:12px;">{ts}/40</span>
                    <span style="color:#7f8c8d; font-size:13px;">{source}</span>
                    <span style="display:inline-block; background:#8e44ad; color:#fff; font-size:12px; padding:2px 10px; border-radius:12px; margin-left:8px;">{track_badge}</span>
                </div>
            </div>
            <div style="font-size:16px; font-weight:bold; color:#2c3e50; margin-bottom:8px;">
                {i}. {title}
            </div>
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
            <div style="margin-top:10px;">
                <a href="{link}" target="_blank" style="display:inline-block; background:#3498db; color:#fff; text-decoration:none; padding:8px 18px; border-radius:6px; font-size:14px;">🔗 阅读原文</a>
            </div>
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
    </head>
    <body style="background:#f5f7fa; padding:20px; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;">
        <div style="max-width:680px; margin:0 auto;">

            <div style="background:linear-gradient(135deg, #1a2a6c, #2d4373); border-radius:16px; padding:30px; text-align:center; margin-bottom:24px;">
                <h1 style="color:#ffffff; font-size:26px; margin:0 0 8px 0;">🌍 今日地学前沿 Top {len(papers)}</h1>
                <p style="color:#a8c8ff; font-size:14px; margin:0;">
                    {today} · Geo_Paper_Radar V2.0 · 双轨制筛选
                </p>
                <p style="color:#a8c8ff; font-size:13px; margin:6px 0 0 0;">
                    评分维度：斜坡稳定性 / 降雨入渗 / 优先流 / 方法创新
                </p>
            </div>

            {cards_html}

            <div style="text-align:center; padding:20px; color:#95a5a6; font-size:13px; border-top:1px solid #e0e0e0; margin-top:10px;">
                <p style="margin:4px 0;">📡 本邮件由 <strong>Geo_Paper_Radar V2.0</strong> 全自动生成</p>
                <p style="margin:4px 0;">🤖 评分引擎：DeepSeek AI · 双轨制：总分≥30 或 创新分≥9</p>
                <p style="margin:4px 0;">📁 .ris 引文文件已同步存入 EndNote_Watch 文件夹</p>
            </div>

        </div>
    </body>
    </html>
    """

    return html


def send_email(html_content):
    """
    通过 SMTP (SSL) 发送 HTML 邮件
    """
    print("\n" + "=" * 60)
    print("【模块四】发送邮件")
    print("=" * 60)

    if not all([SMTP_SENDER, SMTP_PASSWORD, SMTP_RECEIVER]):
        print("  [Error] 邮箱配置不完整，请检查 .env 文件")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🌍 今日地学前沿推送 V2.0 — {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = SMTP_SENDER
    msg["To"] = SMTP_RECEIVER
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        print(f"  [进度] 正在连接 SMTP 服务器 {SMTP_SERVER}:{SMTP_PORT} ...")
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.login(SMTP_SENDER, SMTP_PASSWORD)
            server.sendmail(SMTP_SENDER, [SMTP_RECEIVER], msg.as_string())
        print(f"  [成功] 邮件已发送至 {SMTP_RECEIVER}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("  [Error] SMTP 认证失败，请检查授权码是否正确")
    except smtplib.SMTPException as e:
        print(f"  [Error] SMTP 发送失败: {e}")
    except Exception as e:
        print(f"  [Error] 发送邮件时发生未知错误: {e}")
        traceback.print_exc()

    return False


# ──────────────────────────────────────────────
# 7. 主流程 (V2.0)
# ──────────────────────────────────────────────

def main():
    print("\n" + "🌟" * 30)
    print("  Geo_Paper_Radar V2.0 — 地学文献雷达启动")
    print("🌟" * 30 + "\n")

    start_time = time.time()

    # ---- 步骤 1: 抓取 RSS ----
    papers = fetch_papers_from_rss()
    if not papers:
        print("\n[结果] 今日 RSS 源暂无新文献，任务结束")
        return

    # ---- 步骤 2: DeepSeek V2.0 多维度打分 ----
    scored_papers = score_all_papers(papers)
    if not scored_papers:
        print("\n[结果] 所有文献打分均失败，任务结束")
        return

    # ---- 步骤 3: 双轨制筛选 ----
    pass_list, browsing_list = dual_track_filter(scored_papers)

    # ---- 步骤 4: 生成 .ris 引文文件 (EndNote 联动) ----
    ris_generated = 0
    if pass_list:
        print(f"\n{'=' * 60}")
        print("【EndNote 联动】生成 .ris 引文文件")
        print("=" * 60)
        for p in pass_list:
            fp = generate_ris_file(p)
            if fp:
                ris_generated += 1
                print(f"  ✅ {os.path.basename(fp)}")

    if browsing_list:
        print(f"\n{'=' * 60}")
        print("【备选泛读列表】（仅终端显示 + .ris，不发送邮件）")
        print("=" * 60)
        for idx, p in enumerate(browsing_list, 1):
            ts = p.get("total_score", 0)
            mi = p.get("method_innovation", 0)
            title = p.get("title", "")[:80]
            print(f"  {idx}. [{ts}/40] {title} (创新:{mi}/10)")
            fp = generate_ris_file(p)
            if fp:
                ris_generated += 1
                print(f"     📄 {os.path.basename(fp)}")

    print(f"\n  [汇总] 共生成 {ris_generated} 个 .ris 文件 → {ENDNOTE_WATCH_DIR}")

    # ---- 空转保护 ----
    if not pass_list:
        print("\n" + "=" * 60)
        print("【结果】今日无强相关文献通关（未有文献满足双轨制条件）")
        if browsing_list:
            print(f"📖 但有 {len(browsing_list)} 篇备选泛读文献已存入 EndNote_Watch")
        print("📭 未发送邮件，避免打扰。")
        print("=" * 60)
        elapsed = time.time() - start_time
        print(f"\n🏁 任务完成！总耗时: {elapsed:.1f} 秒")
        print(f"📊 共处理 {len(papers)} 篇 → 通关 0 篇 → 备选 {len(browsing_list)} 篇")
        return

    # ---- 步骤 5: 生成邮件并发送 ----
    print(f"\n[进入] 准备推送 {len(pass_list)} 篇通关文献...")
    html_content = build_html_email_v2(pass_list)
    success = send_email(html_content)

    # ---- 步骤 6: 记录历史 ----
    if success:
        links_to_save = [make_link_key(p) for p in pass_list]
        save_history(links_to_save)
        print("\n✅ 历史记录已更新")

    # ---- 结束 ----
    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"🏁 任务完成！总耗时: {elapsed:.1f} 秒")
    print(f"📊 共处理 {len(papers)} 篇 → 通关 {len(pass_list)} 篇 → 备选 {len(browsing_list)} 篇")
    print(f"📬 请查收邮箱: {SMTP_RECEIVER}")
    print(f"📁 .ris 文件已存入: {ENDNOTE_WATCH_DIR}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()