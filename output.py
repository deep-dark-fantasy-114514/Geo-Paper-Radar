#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""output.py —— .ris 引文文件与邮件推送

从 paper_radar.py 拆出（2026-10-01，逐字搬运，未改逻辑）。
"""
import html as _html
import os
import re
import smtplib
import traceback
import urllib.parse
from datetime import datetime

from config import *


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

        # ★ 2026-10-01 修（审查第 1-4 条）：
        #   ① PY 原来硬编码成【当前年份】，把回溯 365 天的学位论文、
        #      90 天的中文期刊全写成今年 ⇒ 导入 EndNote 后年份全错。
        #   ② summary 原来读了却从不写进 RIS —— 缺标准 AB 字段，
        #      用户无法在 EndNote 里检索摘要。→ 补上。
        #   ③ TY 原来一律 JOUR ⇒ 学位论文该是 THES、预印本该是 UNPB。
        #   ④ DOI 原来在 link 里做正则（link 是落地页/OpenAlex 页时就抓不到）；
        #      sources.pack() 早就清洗好 paper["doi"] 了。→ 优先用现成字段。
        ds = paper.get("data_source", "RSS")
        ty_map = {"OpenAlex-Diss": "THES", "arXiv": "UNPB"}
        ris_lines = [f"TY  - {ty_map.get(ds, 'JOUR')}"]
        ris_lines.append(f"TI  - {title}")
        for author in authors[:10]:
            if author:
                ris_lines.append(f"AU  - {author}")
        _yr = paper.get("year") or datetime.now().year
        ris_lines.append(f"PY  - {_yr}//")
        ris_lines.append(f"JO  - {source}")
        if link:
            ris_lines.append(f"UR  - {link}")
        _doi = (paper.get("doi") or "").strip()
        if not _doi:
            _m = re.search(r'10\.\d{4,}/[\w\.\-]+', link or "")
            _doi = _m.group(0) if _m else ""
        if _doi:
            ris_lines.append(f"DO  - {_doi}")
        if summary and not no_abstract(summary):   # ★ 统一判空（带 strip）
            ris_lines.append("AB  - " + summary)
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
        # ★ 原子写（与 processed / save_history 一致，今天第 4 处补齐）
        tmp = filepath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(ris_content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, filepath)
        return filepath
    except Exception as e:
        print(f"  [Warning] 生成 .ris 失败: {e}")
        return None


# ══════════════════════════════════════════════
# ══════════════════════════════════════════════


def build_html_email_v3(papers):
    """V3.0 HTML 邮件（增加数据源标记）"""
    today = datetime.now().strftime("%Y-%m-%d")
    # 打分器名按配置动态显示（原来页脚写死 DeepSeek，默认却是本地 Qwen）
    _scorer_label = ("本地 Qwen3.5-9B" if SCORER == "local" else "DeepSeek")
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
        # ★ 2026-10-01：原来是非 RSS 就一律标 "OpenAlex" —— 接入 Crossref /
        #   学位论文 / 中文核心刊之后，邮件里的来源提示完全失真。
        ds_badge = {
            "RSS": "📡 RSS", "OpenAlex": "🌐 OpenAlex",
            "OpenAlex-Diss": "🎓 学位论文", "OpenAlex-CN": "🇨🇳 中文核心",
            "Crossref": "📚 Crossref", "arXiv": "📄 arXiv", "S2": "🧠 S2",
        }.get(ds, "🌐 " + str(ds))
        # ★ 2026-10-01：所有来自爬虫/大模型的文本都要转义。
        #   地学标题里 `<` 极常见（"p < 0.05"、"Slope < 10m"）——
        #   不转义会被邮件引擎当成未闭合标签，【吞噬后面的整段正文】。
        e = _html.escape

        def dim_bar(val):
            return "⭐" * val + "☆" * (10 - val)

        cards_html += f"""
        <div style="background:#ffffff; border:1px solid #e0e0e0; border-radius:12px; padding:20px; margin-bottom:16px; box-shadow:0 2px 8px rgba(0,0,0,0.06);">
            <!-- ★ 2026-10-01：原来是 display:flex —— Outlook 用 Word 做渲染内核，
                 完全不支持 flex / justify-content / gap，会退化成块级堆叠、文字重压。
                 改成纯 div + inline-block（下面那些 span 本来就是 inline-block）。 -->
            <div style="margin-bottom:10px;">
                <div>
                    <span style="display:inline-block; background:{score_color}; color:#fff; font-weight:bold; font-size:18px; padding:4px 14px; border-radius:20px; margin-right:12px;">{ts}/40</span>
                    <span style="color:#7f8c8d; font-size:13px;">{e(source)}</span>
                    <span style="display:inline-block; background:#8e44ad; color:#fff; font-size:12px; padding:2px 10px; border-radius:12px; margin-left:8px;">{track_badge}</span>
                    <span style="display:inline-block; background:#2c3e50; color:#fff; font-size:12px; padding:2px 10px; border-radius:12px; margin-left:6px;">{ds_badge}</span>
                </div>
            </div>
            <div style="font-size:16px; font-weight:bold; color:#2c3e50; margin-bottom:8px;">{i}. {e(title)}</div>
            <div style="background:#f0f7ff; border-left:4px solid #3498db; padding:10px 14px; margin:10px 0; border-radius:4px; font-size:15px; color:#2c3e50;">
                💡 <strong>创新点：</strong>{e(tldr)}
            </div>
            <div style="font-size:13px; color:#555; margin:6px 0;">
                <div style="margin:8px 0;">
                    <span style="background:#f8f9fa; padding:4px 10px; border-radius:6px; margin:0 8px 6px 0;">🏔️ 斜坡 {ss}/10 {dim_bar(ss)}</span>
                    <span style="background:#f8f9fa; padding:4px 10px; border-radius:6px; margin:0 8px 6px 0;">🌧️ 降雨 {ri}/10 {dim_bar(ri)}</span>
                    <span style="background:#f8f9fa; padding:4px 10px; border-radius:6px; margin:0 8px 6px 0;">💧 优先流 {pf}/10 {dim_bar(pf)}</span>
                    <span style="background:#f8f9fa; padding:4px 10px; border-radius:6px; margin:0 8px 6px 0;">🔬 创新 {mi}/10 {dim_bar(mi)}</span>
                </div>
                📌 <strong>推荐理由：</strong>{e(reason)}
            </div>
            <div style="margin-top:10px;"><a href="{e(link)}" target="_blank" style="display:inline-block; background:#3498db; color:#fff; text-decoration:none; padding:8px 18px; border-radius:6px; font-size:14px;">🔗 阅读原文</a></div>
        </div>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="background:#f5f7fa; padding:20px; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;">
<div style="max-width:680px; margin:0 auto;">
    <div style="background:linear-gradient(135deg, #1a2a6c, #2d4373); border-radius:16px; padding:30px; text-align:center; margin-bottom:24px;">
        <h1 style="color:#ffffff; font-size:26px; margin:0 0 8px 0;">🌍 今日地学前沿 Top {len(papers)}</h1>
        <p style="color:#a8c8ff; font-size:14px; margin:0;">{today} · Geo_Paper_Radar V3.0 · 多源检索（OpenAlex/Crossref/学位论文/中文核心/RSS）</p>
        <p style="color:#a8c8ff; font-size:13px; margin:6px 0 0 0;">评分维度：斜坡稳定性 / 降雨入渗 / 优先流 / 方法创新 · 双轨制筛选</p>
    </div>
    {cards_html}
    <div style="text-align:center; padding:20px; color:#95a5a6; font-size:13px; border-top:1px solid #e0e0e0; margin-top:10px;">
        <p style="margin:4px 0;">📡 多源检索 · 两阶段过滤：加权 Regex → {_scorer_label}</p>
        <p style="margin:4px 0;">🤖 双轨制：总分≥30 或 创新分≥9</p>
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
    # ★ 2026-10-01：补纯文本备用正文。
    #   原来只有 text/html，纯文本客户端看到空白，且会拉高反垃圾评分。
    #   结构改为 multipart/alternative 包 plain + html，再嵌进 mixed。
    _plain = re.sub(r"<[^>]+>", " ", html_content)
    _plain = _html.unescape(re.sub(r"\s+", " ", _plain)).strip()[:4000]
    related = MIMEMultipart("related")
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(_plain, "plain", "utf-8"))
    alt.attach(MIMEText(html_content, "html", "utf-8"))
    related.attach(alt)
    msg.attach(related)
    # 添加 .ris 附件
    if attachments:
        n_att = 0
        for filepath in attachments:
            # ★ 2026-10-01：逐个包异常。某个 .ris 被杀毒软件/同步工具/EndNote
            #   加了排他锁时原来会抛 PermissionError 打断整个循环，
            #   连 HTML 正文都发不出去。现在坏一个只跳过它。
            try:
                if not os.path.exists(filepath):
                    continue
                with open(filepath, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    f"attachment; filename*=UTF-8''{urllib.parse.quote(os.path.basename(filepath))}"
                )
                msg.attach(part)
                n_att += 1
            except Exception as _e:
                print(f"  [警告] 附件读取失败，已跳过 {os.path.basename(filepath)}：{_e}")
        print(f"  📎 已挂 {n_att} 个附件")
    try:
        print(f"  [进度] 连接 {SMTP_SERVER}:{SMTP_PORT} ...")
        # ★ 2026-10-01：按端口自动选协议。
        #   原来写死 SMTP_SSL ⇒ 只支持隐式 SSL（465）；换成 587（机构邮箱 /
        #   SendGrid / SES / Office365 常用的 STARTTLS）会直接
        #   SSLError: WRONG_VERSION_NUMBER，邮件彻底发不出。
        if int(SMTP_PORT) == 465:
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
                server.login(SMTP_SENDER, SMTP_PASSWORD)
                server.sendmail(SMTP_SENDER, [SMTP_RECEIVER], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
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
# ══════════════════════════════════════════════
