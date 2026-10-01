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

# ★ 2026-10-01：本文件原先自己写死了三样东西，全部收归 config / 单一实现：
#   · 目录常量      → 从 config 取（BASE_DIR 相对，云端也安全）
#   · MANUAL_DROP_DIR / RENAMER / ASK_IMAGE
#                   → 原来是 r"E:\论文\..." 和 r"C:\Users\zihao\..."，
#                     换机器/换盘/上 Linux 就得改代码。现在进 config.py，
#                     且支持 PAPER_RADAR_* 环境变量覆盖。
#   · key_of / has_no_abstract → 本文件曾各写一份，与 processed / filters
#                     里的实现重复。实测两边输出完全一致，但那是巧合；
#                     以后改清洗规则漏掉一处就会"去重表说处理过、下载清单说没有"。
#                     现在直接引用，只有一个真身。
from config import (BASE_DIR, PDF_INBOX_DIR, MANUAL_DROP_DIR, RENAMER, ASK_IMAGE,
                    atomic_copy_into, unique_path)
from processed import key_of                      # noqa: F401 (对外仍以 lm.key_of 暴露)
from filters import _no_abstract as has_no_abstract   # noqa: F401

LIBRARY_DIR = os.path.join(BASE_DIR, "Library")
DIGEST_DIR = os.path.join(LIBRARY_DIR, "简报")
DOWNLOAD_LIST_DIR = os.path.join(LIBRARY_DIR, "待下载清单")

DEFAULT_TEMPLATE = "{year}_{author}_{title_zh}"

# 维度 → 主题目录名（顺序即优先级，分数相同时靠前者优先）
CATEGORY_OF = [
    ("preferential_flow",     "优先流"),
    ("rainfall_infiltration", "降雨入渗"),
    ("slope_stability",       "边坡稳定"),
    ("method_innovation",     "方法创新"),
]


def _load_module(path, name):
    """按路径动态加载一个 .py 模块。

    ★ 2026-10-01：加前置存在性检查。原来直接往下走，
      路径不存在时抛的是 `FileNotFoundError`（栈里是 importlib 内部），
      很难看出"是配置里的路径写错了"。现在直接点名到路径。
    """
    import importlib.util
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"模块不存在：{path}\n"
            f"  （可用环境变量覆盖，见 config.py 的 RENAMER / ASK_IMAGE）")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为该路径建立加载器：{path}")
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
    """重名不覆盖。★ 2026-10-01：实现搬到 config.unique_path（download /
    manual_ingest 都要用，避免又出现三份各自进化的副本），这里只留个别名。"""
    return unique_path(path)


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
    # ★ 2026-10-01：原来裸 shutil.copy2 —— PDF_Inbox 是 EndNote 实时监听的目录，
    #   几十 MB 的 PDF 写几百毫秒到数秒，监视器可能读到半截文件并把它导进库里。
    #   改用 config.atomic_copy_into（.tmp + os.replace）。
    try:
        atomic_copy_into(dst, PDF_INBOX_DIR, newname)
    except Exception as e:
        print(f"     ⚠ 复制到 PDF_Inbox 失败：{str(e)[:50]}")

    return dst, cat


def build_digest(pass_list, browsing_list, scored_total, elapsed, date_str=None,
                 titleonly=None):
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
        title = _md(p.get("title", ""))
        link = p.get("link", "")
        src = _md(p.get("source", ""))
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

    # ★ 2026-10-01：只有标题的论文【不打分】，单独一节供人工判断
    if titleonly:
        L.append("\n## 只有标题 · 未打分（闭源未公开摘要，仅做了相关性判断）\n")
        L.append(f"共 **{len(titleonly)}** 篇被判为与本方向相关。"
                 f"它们**没有四维分数**——标题信息量不足以支撑细粒度评分。"
                 f"已一并进入「待下载清单」，可手动取回后再判断。\n")
        L.append("| # | 作用 | 尺度 | 方法 | 标题 | 原文 | 来源 |")
        L.append("|---|------|------|------|------|------|------|")
        for i, p in enumerate(titleonly, 1):
            _r = {"adverse": "**不利**", "beneficial": "有利",
                  "both": "两面", "none": "—"}.get(p.get("dual_role", "none"), "—")
            _s = {"pore": "孔隙", "slope": "边坡", "catchment": "流域",
                  "regional": "区域"}.get(p.get("scale", "na"), "—")
            _a = {"numerical": "数值", "experimental": "实验", "theoretical": "理论",
                  "review": "综述", "data-driven": "数据"}.get(
                      p.get("approach", "na"), "—")
            L.append(f"| {i} | {_r} | {_s} | {_a} | {_md(p.get('title',''))} | "
                     f"[链接]({p.get('link','')}) | {_md(p.get('source',''))} |")
        L.append("\n### 判定理由\n")
        for i, p in enumerate(titleonly, 1):
            L.append(f"- **{i}.** {_md(p.get('title','')[:110])}")
            L.append(f"  - {p.get('reason','')}")

    with io.open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return path


def _md(s):
    """把任意文本塞进 Markdown 表格单元格前的转义。

    ★ 2026-10-01：地学/力学期刊标题里 `|` 极常见
      （"Landslide Hazard Mapping | A Comparative Study"），
      不转义的话整行会被拆成多余的列，**下面所有行的排版全部错位**。
      顺带把换行折成空格（表格单元格不能跨行）。
    """
    return str(s or "").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _clean_doi(p):
    """把 DOI 洗成裸形式（`10.xxxx/yyyy`）。

    ★ 2026-10-01：原来只 `replace("https://doi.org/", "")` —— 出版商的元数据里
      还常见 `http://dx.doi.org/`、`https://dx.doi.org/`、`http://doi.org/`
      三种写法，全都漏网 ⇒ 脏 DOI 进了 CSV，`manual_ingest.py` 匹配不上。
      同文件的 `key_of` 早就用了标准正则，这里跟它保持一致。
    """
    doi = (p.get("doi") or "").strip()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)


def _load_existing_csv(csv_path):
    """读回当天已存在的清单，返回 {去重键: 原始行 dict}。

    ★ 2026-10-01：清单原来是 `"w"` 直接覆盖的。一天跑两次
      （早上定时任务列出 10 篇、下午手动再跑一次抓到 2 篇）时，
      下午那次会把早上的 10 篇**整份抹掉**，排队等你手动下载的文献凭空消失。
      ⇒ 写入前先合并去重。
    """
    import csv
    if not os.path.exists(csv_path):
        return {}, []
    try:
        with io.open(csv_path, encoding="utf-8-sig", newline="") as f:
            r = csv.DictReader(f)
            header = r.fieldnames
            rows = list(r)
    except Exception as e:
        print(f"  [警告] 旧清单读取失败，将覆盖写：{e}")
        return {}, []
    out = {}
    for row in rows:
        k = key_of({"doi": row.get("doi", ""), "title": row.get("title", "")})
        if k and k != "title:":
            out[k] = row
    return out, (header or [])


def build_download_list(papers, already_filed_keys, date_str=None):
    """为『高分但没能自动拿到 PDF』的文献出一份下载清单。

    什么会进清单：
      · 闭源（无 OA 链接）—— 本来就得手动下
      · OA 但出版商硬拦（Wiley / MDPI 的 403）—— 自动下载失败
    产出两个文件（都在 Library\\待下载清单\\）：
      · YYYY-MM-DD.csv  —— 给 manual_ingest.py 匹配用，也方便 Excel 打开
      · YYYY-MM-DD.md   —— 给人看，含理由与链接
    返回 (csv路径, 条数)。当天已有清单会被**合并**而不是覆盖。
    """
    date_str = date_str or time.strftime("%Y-%m-%d")
    os.makedirs(DOWNLOAD_LIST_DIR, exist_ok=True)
    csv_path = os.path.join(DOWNLOAD_LIST_DIR, f"{date_str}.csv")
    md_path = os.path.join(DOWNLOAD_LIST_DIR, f"{date_str}.md")

    # ── 先读回当天已有的（可能来自当天更早的一次运行）──
    #    以 CSV 为准：它字段稳定、有 doi/title 可算键；.md 是它的渲染产物。
    old_by_key, _hdr = _load_existing_csv(csv_path)

    todo = [p for p in papers if key_of(p) not in already_filed_keys]
    fresh = {}
    for p in todo:
        k = key_of(p)
        if k and k != "title:":
            fresh[k] = p

    merged_keys = list(old_by_key) + [k for k in fresh if k not in old_by_key]
    if not merged_keys:
        return None, 0
    n_old = len([k for k in old_by_key if k not in fresh])
    if n_old:
        print(f"  [清单合并] 沿用当天早先的 {n_old} 条，本轮新增 {len(fresh)} 条")

    FIELDS = ["doi", "title", "year", "journal", "category",
              "score", "reason", "has_abstract", "link"]

    def _as_row(p):
        return [
            _clean_doi(p),
            p.get("title", ""),
            p.get("year", ""),
            p.get("source", ""),
            pick_category(p),
            # ★ 只有标题的没打过分，用标记代替数字，别让 0 被误读成"很不相关"
            ("仅标题" if p.get("_title_only") else p.get("total_score", 0)),
            p.get("reason", ""),
            "无" if has_no_abstract(p) else "有",
            p.get("link", ""),
        ]

    def _as_md(p, i):
        _sc = "**仅标题**" if p.get("_title_only") else p.get("total_score", 0)
        return (f"| {i} | {_sc} | {pick_category(p)} | "
                f"{_md(p.get('title',''))} | `{_clean_doi(p)}` | "
                f"{'**无**' if has_no_abstract(p) else '有'} | "
                f"[链接]({p.get('link','')}) |")

    import csv
    # ★ 原子写：与 processed / save_history / harvest 一致
    tmp = csv_path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        rows_md = []
        i = 0
        for k in merged_keys:
            if k in fresh:
                p = fresh[k]
                w.writerow(_as_row(p))
                i += 1
                rows_md.append(_as_md(p, i))
            else:
                # 沿用旧 CSV 那一行（字段顺序按 FIELDS 对齐）
                row = old_by_key[k]
                w.writerow([row.get(c, "") for c in FIELDS])
                i += 1
                rows_md.append(
                    f"| {i} | {_md(row.get('score',''))} | {_md(row.get('category',''))} | "
                    f"{_md(row.get('title',''))} | `{_md(row.get('doi',''))}` | "
                    f"{_md(row.get('has_abstract',''))} | "
                    f"[链接]({row.get('link','')}) |")
    os.replace(tmp, csv_path)

    L = [f"# 待下载清单 · {date_str}\n",
         f"共 **{len(merged_keys)}** 篇没能自动拿到 PDF（闭源，或出版商拦截）。",
         f"手动下载后丢进 `{MANUAL_DROP_DIR}\\`，再跑 `python manual_ingest.py` 即可自动入库。\n",
         "| # | 分 | 主题 | 文献 | DOI | 摘要 | 链接 |",
         "|---|----|------|------|-----|------|------|"]
    L.extend(rows_md)
    _tmp_md = md_path + ".tmp"
    with io.open(_tmp_md, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    os.replace(_tmp_md, md_path)
    return csv_path, len(merged_keys)


def ensure_dirs():
    for d in (PDF_INBOX_DIR, LIBRARY_DIR, DIGEST_DIR, DOWNLOAD_LIST_DIR):
        os.makedirs(d, exist_ok=True)
