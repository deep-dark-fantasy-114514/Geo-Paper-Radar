#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
library_manager.py —— 文献归档与简报（2026-10-01 新增）

职责三件事：
  1. rename  —— 用本地视觉模型读 PDF 标题页（可能是第 2/3 页），重命名为中文名
  2. file    —— 按【最高分维度】归入 Library\\<主题>\\，同时复制一份到 PDF_Inbox\\
                （EndNote 的自动导入不扫子目录，所以 PDF_Inbox 必须平铺）
  3. digest  —— 生成 Markdown 文献简报，列出建议精读的篇目

设计原则：**任何一步失败都不中断主流程**，只记一行警告。
"""
import io
import os
import re
import shutil
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PDF_INBOX_DIR = os.path.join(BASE_DIR, "PDF_Inbox")
LIBRARY_DIR = os.path.join(BASE_DIR, "Library")
DIGEST_DIR = os.path.join(LIBRARY_DIR, "简报")
DOWNLOAD_LIST_DIR = os.path.join(LIBRARY_DIR, "待下载清单")
MANUAL_DROP_DIR = r"E:\论文\手动下载"   # 你自己下好 PDF 后丢这里

RENAMER = r"E:\论文\PDF_Renamer_Skill\rename_pdfs_ai.py"
ASK_IMAGE = r"C:\Users\zihao\.claude\skills\local-vision\scripts\ask_image.py"

DEFAULT_TEMPLATE = "{year}_{author}_{title_zh}"

# 维度 → 主题目录名（顺序即优先级，分数相同时靠前者优先）
CATEGORY_OF = [
    ("preferential_flow",     "优先流"),
    ("rainfall_infiltration", "降雨入渗"),
    ("slope_stability",       "边坡稳定"),
    ("method_innovation",     "方法创新"),
]


def _load_module(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pick_category(paper):
    """按四个维度的最高分决定归档主题。全为 0 时归『其他』。"""
    best_key, best_val = None, -1
    for key, cat in CATEGORY_OF:
        v = paper.get(key, 0) or 0
        if v > best_val:
            best_key, best_val = cat, v
    if best_val <= 0:
        return "其他"
    return best_key


def _unique(path):
    """重名不覆盖。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{stem} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def rename_pdf(pdf, ai, rn, template=DEFAULT_TEMPLATE, pages=3, dpi=150):
    """读标题页 → 返回建议的新【文件名】（不落盘）。失败返回原文件名。"""
    base = os.path.basename(pdf)
    pngs = []
    try:
        pngs = rn.render_pages(pdf, pages, dpi)
        if not pngs:
            return base, {}
        reply = ai.ask(pngs, rn.PROMPT.format(n=len(pngs)),
                       max_tokens=700, temperature=0.1)
        meta = rn.parse_json_reply(reply) or {}
        return rn.build_name(meta, template, os.path.splitext(base)[0]), meta
    except Exception as e:
        print(f"     ⚠ 重命名失败（{type(e).__name__}）：{base[:40]}")
        return base, {}
    finally:
        for p in pngs:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


def file_paper(pdf, paper, ai=None, rn=None, template=DEFAULT_TEMPLATE,
               do_rename=True):
    """把已下载的 PDF 归档。返回 (归档后的完整路径, 主题) 或 (None, 原因)。"""
    if not pdf or not os.path.exists(pdf):
        return None, "文件不存在"
    cat = pick_category(paper)
    newname = os.path.basename(pdf)
    if do_rename and ai is not None and rn is not None:
        newname, _ = rename_pdf(pdf, ai, rn, template=template)

    dst_dir = os.path.join(LIBRARY_DIR, cat)
    try:
        os.makedirs(dst_dir, exist_ok=True)
        dst = _unique(os.path.join(dst_dir, newname))
        shutil.move(pdf, dst)
    except Exception as e:
        print(f"     ⚠ 归档失败（{type(e).__name__}）：{str(e)[:50]}")
        return None, "归档失败"

    # 同时放一份到 EndNote 的自动导入文件夹（必须平铺）
    try:
        os.makedirs(PDF_INBOX_DIR, exist_ok=True)
        shutil.copy2(dst, _unique(os.path.join(PDF_INBOX_DIR, newname)))
    except Exception as e:
        print(f"     ⚠ 复制到 PDF_Inbox 失败：{str(e)[:50]}")

    return dst, cat


def build_digest(pass_list, browsing_list, scored_total, elapsed, date_str=None):
    """生成 Markdown 文献简报。返回文件路径。"""
    date_str = date_str or time.strftime("%Y-%m-%d")
    os.makedirs(DIGEST_DIR, exist_ok=True)
    path = os.path.join(DIGEST_DIR, f"{date_str}_文献简报.md")

    def row(p, i):
        ts = p.get("total_score", 0)
        cat = pick_category(p)
        # ★ 2026-10-01：作用(adverse/beneficial/both/none) 是用户的核心命题，
        #   放进主表；尺度与方法放进下面的「逐篇理由」。
        role = {"adverse": "**不利**", "beneficial": "有利",
                "both": "两面", "none": "—"}.get(p.get("dual_role", "none"), "—")
        dims = (f"坡{p.get('slope_stability',0)} "
                f"雨{p.get('rainfall_infiltration',0)} "
                f"优{p.get('preferential_flow',0)} "
                f"新{p.get('method_innovation',0)}")
        title = p.get("title", "")
        link = p.get("link", "")
        src = p.get("source", "")
        return (f"| {i} | **{ts}**/40 | {cat} | {role} | {title} | {dims} | "
                f"[链接]({link}) | {src} |")

    head = ("| # | 总分 | 主题 | 作用 | 标题 | 四维(坡/雨/优/新) | 原文 | 来源 |\n"
            "|---|------|------|------|------|------------------|------|------|\n")

    L = []
    L.append(f"# 地学文献简报 · {date_str}\n")
    L.append(f"扫描候选 **{scored_total}** 篇 → 建议精读 **{len(pass_list)}** 篇 "
             f"→ 备选泛读 **{len(browsing_list)}** 篇　"
             f"（本地模型打分，用时 {elapsed:.0f}s）\n")

    if pass_list:
        L.append("## ★ 建议精读\n")
        L.append(head)
        for i, p in enumerate(pass_list, 1):
            L.append(row(p, i))
        L.append("\n### 逐篇理由\n")
        for i, p in enumerate(pass_list, 1):
            L.append(f"**{i}. {p.get('title','')}**　`{p.get('total_score',0)}/40`\n")
            _role = {"adverse": "讲不利作用（本人重点）", "beneficial": "讲排水有利（对照文献）",
                     "both": "两面都讲", "none": "不涉及优先流的作用"}.get(
                         p.get("dual_role", "none"), "—")
            _scale = {"pore": "孔隙", "slope": "边坡", "catchment": "流域",
                      "regional": "区域", "na": "—"}.get(p.get("scale", "na"), "—")
            _appr = {"numerical": "数值模拟", "experimental": "实验",
                     "theoretical": "理论", "review": "综述",
                     "data-driven": "数据驱动", "na": "—"}.get(
                         p.get("approach", "na"), "—")
            L.append(f"- 优先流作用：{_role}　｜　尺度：{_scale}　｜　方法：{_appr}")
            if p.get("tldr"):
                L.append(f"- 创新点：{p['tldr']}")
            if p.get("reason"):
                L.append(f"- 推荐理由：{p['reason']}")
            if p.get("oa_pdf_url"):
                L.append(f"- [开放获取 PDF]({p['oa_pdf_url']})")
            L.append("")

    if browsing_list:
        L.append("## 备选泛读\n")
        L.append(head)
        for i, p in enumerate(browsing_list, 1):
            L.append(row(p, i))

    with io.open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return path


def has_no_abstract(p):
    """闭源论文经常拿不到摘要（OpenAlex 对 is_oa:false 的摘要覆盖只有约 24%）。
    没有摘要时打分只靠标题，判据弱，必须在清单里标出来。"""
    s = (p.get("summary") or "").strip()
    return (not s) or s.startswith("No abstract")


def key_of(paper):
    """文献的唯一标识：优先 DOI，其次规范化标题。用于跨清单匹配。

    ⚠️ 必须与 `processed.key_of()` **逐字一致** —— 两边算出的键不同的话，
       去重表就认不出同一篇，跨天/跨源重复又会冒出来。
    """
    doi = (paper.get("doi") or "").strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    if doi:
        return "doi:" + doi
    t = (paper.get("title") or "").strip().lower()
    t = re.sub(r"<[^>]+>", " ", t)                    # 去掉 XML 标签
    t = re.sub(r"[^0-9a-z一-鿿 ]", "", re.sub(r"\s+", " ", t))
    return "title:" + t[:120]


def build_download_list(papers, already_filed_keys, date_str=None):
    """为『高分但没能自动拿到 PDF』的文献出一份下载清单。

    什么会进清单：
      · 闭源（无 OA 链接）—— 本来就得手动下
      · OA 但出版商硬拦（Wiley / MDPI 的 403）—— 自动下载失败
    产出两个文件（都在 Library\\待下载清单\\）：
      · YYYY-MM-DD.csv  —— 给 manual_ingest.py 匹配用，也方便 Excel 打开
      · YYYY-MM-DD.md   —— 给人看，含理由与链接
    返回 (csv路径, 条数)。
    """
    date_str = date_str or time.strftime("%Y-%m-%d")
    todo = [p for p in papers if key_of(p) not in already_filed_keys]
    if not todo:
        return None, 0
    os.makedirs(DOWNLOAD_LIST_DIR, exist_ok=True)
    csv_path = os.path.join(DOWNLOAD_LIST_DIR, f"{date_str}.csv")

    import csv
    with io.open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["doi", "title", "year", "journal", "category",
                    "score", "reason", "has_abstract", "link"])
        for p in todo:
            doi = (p.get("doi") or "").strip()
            doi = doi.replace("https://doi.org/", "")
            w.writerow([
                doi,
                p.get("title", ""),
                p.get("year", ""),
                p.get("source", ""),
                pick_category(p),
                p.get("total_score", 0),
                p.get("reason", ""),
                "无" if has_no_abstract(p) else "有",
                p.get("link", ""),
            ])

    md_path = os.path.join(DOWNLOAD_LIST_DIR, f"{date_str}.md")
    L = [f"# 待下载清单 · {date_str}\n",
         f"共 **{len(todo)}** 篇没能自动拿到 PDF（闭源，或出版商拦截）。",
         f"手动下载后丢进 `{MANUAL_DROP_DIR}\\`，再跑 `python manual_ingest.py` 即可自动入库。\n",
         "| # | 分 | 主题 | 文献 | DOI | 摘要 | 链接 |",
         "|---|----|------|------|-----|------|------|"]
    for i, p in enumerate(todo, 1):
        doi = (p.get("doi") or "").replace("https://doi.org/", "")
        has_ab = not has_no_abstract(p)
        L.append(f"| {i} | {p.get('total_score',0)} | {pick_category(p)} | "
                 f"{p.get('title','')} | `{doi}` | {'有' if has_ab else '**无**'} | "
                 f"[链接]({p.get('link','')}) |")
    with io.open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return csv_path, len(todo)


def ensure_dirs():
    for d in (PDF_INBOX_DIR, LIBRARY_DIR, DIGEST_DIR, DOWNLOAD_LIST_DIR):
        os.makedirs(d, exist_ok=True)
