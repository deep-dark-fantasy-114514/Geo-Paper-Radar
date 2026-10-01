#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
manual_ingest.py —— 把【你手动下载的 PDF】自动重命名、归类、送进 EndNote

为什么需要它
------------
闭源期刊的 PDF 自动下不了（出版商硬拦，约 70% 的 OA 链接也拿不到）。
所以流程改成：
    每天的简报/待下载清单  →  你看一眼，手动把想要的 PDF 下下来
    →  丢进 E:\\论文\\手动下载\\  →  跑本脚本  →  自动入库

本脚本干四件事：
    1. 读标题页（本地视觉模型，会自己找是第 1 还是第 2/3 页）
    2. 从 PDF 里认 DOI / 标题，去【待下载清单】里查它属于哪个主题
       （查不到就问本地模型现场判一个）
    3. 重命名为中文名，归入 Library\\<主题>\\
    4. 复制一份到 PDF_Inbox\\  →  EndNote 自动导入文件夹会自动收走

用法
----
    python manual_ingest.py                 # 处理 E:\\论文\\手动下载\\
    python manual_ingest.py --dir <文件夹>   # 换个来源文件夹
    python manual_ingest.py --dry-run       # 只看会怎么改，不动文件
    python manual_ingest.py --pending       # 看还有哪些清单条目没入库
    python manual_ingest.py --template "{year}_{author}_{title_zh}"

建议：一周或一月跑一次；也可以把它也挂进 Windows 计划任务。
"""
import argparse
import difflib
import glob
import io
import json
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import library_manager as lm          # noqa: E402
import processed                      # noqa: E402   ★ 入库后要回报中央去重表
from config import norm_title         # noqa: E402   全仓唯一的标题清洗实现

DEFAULT_TEMPLATE = "{year}_{author}_{title_zh}"
INGESTED_MARK = os.path.join(lm.DOWNLOAD_LIST_DIR, "_已入库.json")

CLASSIFY_PROMPT = (
    "这是一篇地学论文的前几页。请只输出一个 JSON，不要解释：\n"
    '{"category": "<四选一>"}\n'
    "四选一：优先流 / 降雨入渗 / 边坡稳定 / 方法创新\n"
    "判断依据：\n"
    "- 优先流：大孔隙流、根土间隙流、优势流、裂隙优先流\n"
    "- 降雨入渗：雨水入渗过程、非饱和渗流、入渗模型、湿润锋\n"
    "- 边坡稳定：滑坡机理、稳定性分析、加固、灾害评价\n"
    "- 方法创新：以上都不是但方法本身有突破（新模型/新实验手段/新数据）\n"
    "都不沾就填 其他。"
)

# ★ 2026-10-01：模型输出常是"语义正确但字面微调"
#   （边坡稳定性 / 滑坡稳定性 / 边坡失稳…）。原来严格全等 ⇒ 一律打回"其他"，
#   分类退化。改成关键词命中。
CATEGORY_SYNONYMS = [
    ("优先流",     ("优先流", "优势流", "大孔隙", "裂隙流", "根土间隙", "preferential", "macropore")),
    ("降雨入渗",   ("降雨入渗", "入渗", "非饱和渗流", "湿润锋", "入渗模型", "infiltration")),
    ("边坡稳定",   ("边坡", "滑坡", "斜坡", "稳定", "加固", "slope", "landslide")),
    ("方法创新",   ("方法创新", "创新", "新模型", "新方法", "新实验")),
]


def normalize_category(raw):
    """把模型的自由发挥收敛到四个主题之一，收不住就"其他"。"""
    s = (raw or "").strip()
    if not s:
        return "其他"
    for cat, kws in CATEGORY_SYNONYMS:
        if any(kw in s.lower() or kw in s for kw in kws):
            return cat
    return "其他"


# --------------------------------------------------------------- 清单索引
def _norm_doi(s):
    """DOI 归一化：去协议头 + 转小写。

    ★ 2026-10-01：建索引和查索引必须用同一个函数。
      原来建索引只 `.strip().lower()`（不剥协议头），查询时却剥了
      ⇒ CSV 里若残留 `http://dx.doi.org/10.x`（旧版 library_manager
      只 replace 掉了 https://doi.org/ 一种），精确匹配直接落空。
    """
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", (s or "").strip(), flags=re.I)
    return d.strip().lower()


def _norm_title(s):
    """标题归一化。

    ★ 2026-10-01：改为调用 `config.norm_title`（全仓唯一实现）。
      本地这份原来漏了「剥离 XML 标签」这一步 —— 而 `processed.key_of`
      和 `sources._norm_title_key` 都有。含 `<i>` 之类标签的标题会算出
      不同的键，`load_pending_index` 建的索引和 `processed.key_of` 算的
      销账键就对不上。
    """
    return norm_title(s)


def load_pending_index():
    """把所有待下载清单读成 {key: 条目} 的索引。"""
    idx = {}
    for p in sorted(glob.glob(os.path.join(lm.DOWNLOAD_LIST_DIR, "*.csv"))):
        try:
            import csv
            with io.open(p, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    doi = _norm_doi(row.get("doi"))
                    title = _norm_title(row.get("title"))
                    if doi:
                        idx["doi:" + doi] = row
                    if title:
                        idx["title:" + title[:120]] = row
        except Exception as e:
            print("  [警告] 读清单失败 %s：%s" % (os.path.basename(p), e))
    return idx


def _similarity(a, b):
    """两段标题的相似度（0–1）。

    ★ 2026-10-01：选 `SequenceMatcher`（字符级）而不是词集 Jaccard ——
      实测同一篇论文只差一个词（OCR 常见）时：
              Jaccard 0.82   SeqMatcher 0.94
      而【不同篇但开头同质】时：
              Jaccard 0.80   SeqMatcher 0.89
      Jaccard 的真假分离只有 0.02，压根切不开；SeqMatcher 有 0.05 的余量。
      中文标题没有空格、词集法还要额外切词，字符级反而是通用解。
    """
    return difflib.SequenceMatcher(
        None, _norm_title(a), _norm_title(b)).ratio()


def _load_done():
    try:
        with io.open(INGESTED_MARK, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_done(s):
    os.makedirs(lm.DOWNLOAD_LIST_DIR, exist_ok=True)
    try:
        with io.open(INGESTED_MARK, "w", encoding="utf-8") as f:
            json.dump(sorted(s), f, ensure_ascii=False)
    except Exception:
        pass


# 模糊匹配阈值。实测（2026-10-01）：
#     同一篇、英文、差 1–2 个词 .......... 0.944–0.945
#     不同篇、英文、开头同质 ............ 0.769 / 0.835 / 0.894
#     同一篇、中文短标题、差 2 个字 ...... 0.857
#     不同篇、中文短标题、差 4 个字 ...... 0.828
# ⚠️ 真假的取值区间【有重叠】（真最低 0.857 < 假最高 0.894），没有任何阈值能全对。
#    取 0.92 = 可用的最大间隔：既不放进任何假匹配，也救回英文的 OCR 噪声。
#    代价是【中文短标题被 OCR 打错两个字时匹配不上】——
#    它只会退到"清单未命中，模型判定"另判一个主题，**不会张冠李戴**。
#    这个方向的失败是可接受的：错配的代价（套错分类 + 销错账）远高于漏配。
MATCH_MIN_SIM = 0.92


def match_entry(meta, idx):
    """用 DOI（主）或标题（辅）在清单里找出该篇。

    返回 (条目, 命中的索引键, 命中方式)。第三个是**索引里那把标准键** ——
    入库销账必须用它，不能用 OCR 出来的 meta 现算（见 main 里的说明）。
    """
    doi = _norm_doi(meta.get("doi"))
    k = "doi:" + doi
    if doi and k in idx:
        return idx[k], k, "DOI"

    t = _norm_title(meta.get("title"))
    k = "title:" + t[:120]
    if t and k in idx:
        return idx[k], k, "标题"

    # ★ 2026-10-01：退化匹配原来比【前 40 字全等】——
    #   地学标题开头高度同质，实测 5 组真实标题里 2 组前 40 字完全相同
    #   （"Numerical investigation of the effect of " 恰好 40 字符！），
    #   而且 `for k in idx.items()` 是【按字典序任取其一】⇒ 张冠李戴，
    #   套上错误的分类与记录。改为词集 Jaccard 相似度。
    if len(t) <= 25:
        return None, None, None
    best, best_key, best_sim = None, None, 0.0
    for k, v in idx.items():
        if not k.startswith("title:"):
            continue
        sim = _similarity(t, k[6:])
        if sim > best_sim:
            best, best_key, best_sim = v, k, sim
    if best is not None and best_sim >= MATCH_MIN_SIM:
        return best, best_key, "标题(相似 %.2f)" % best_sim
    return None, None, None


# --------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="手动下载的 PDF → 自动入库")
    ap.add_argument("--dir", default=lm.MANUAL_DROP_DIR, help="放手动下载 PDF 的文件夹")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE)
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pending", action="store_true", help="只看待下载清单状态")
    args = ap.parse_args()

    idx = load_pending_index()
    done = _load_done()

    if args.pending:
        # ★ 2026-10-01：原来只打两个总数（"条目约 N 条，已入库 M 条"）让用户自己减，
        #   而且 idx 的键和 done 的键来源不同（索引来自 CSV，done 原来是 OCR 现算的）
        #   ⇒ 那个减法本身也不可靠。现在两边同源，直接列出【还没入库的】。
        print("待下载清单目录：%s" % lm.DOWNLOAD_LIST_DIR)
        files = sorted(glob.glob(os.path.join(lm.DOWNLOAD_LIST_DIR, "*.csv")))
        if not files:
            print("  （还没有清单。跑一次 paper_radar.py 就会生成）")
            return 0
        # 一个条目可能同时有 doi: 和 title: 两个键 ⇒ 按"条目身份"归并，
        # 只要它任何一个键在 done 里就算已入库。
        keys_of, rows_by_id = {}, {}
        for k, row in idx.items():
            eid = id(row)
            keys_of.setdefault(eid, set()).add(k)
            rows_by_id[eid] = row
        pending = [rows_by_id[e] for e, ks in keys_of.items() if not (ks & done)]
        print("  清单文件 %d 份，条目 %d 条，已入库 %d 条，**待下载 %d 条**"
              % (len(files), len(keys_of), len(keys_of) - len(pending),
                 len(pending)))
        print("  清单：%s" % "、".join(os.path.basename(p) for p in files))
        if pending:
            print("\n  还没入库的：")
            for i, row in enumerate(pending, 1):
                print("   %3d. [%s] %s" % (
                    i, (row.get("category") or "?"),
                    (row.get("title") or "")[:70]))
                if row.get("doi"):
                    print("        DOI: %s" % row["doi"])
        else:
            print("\n  ✓ 清单里的都入库了")
        return 0

    if not os.path.isdir(args.dir):
        os.makedirs(args.dir, exist_ok=True)
        print("来源文件夹不存在，已创建：%s" % args.dir)
        print("把手动下载的 PDF 丢进去，再跑一次本脚本。")
        return 0

    pdfs = [p for p in glob.glob(os.path.join(args.dir, "*.pdf"))]
    if not pdfs:
        print("来源文件夹里没有 PDF：%s" % args.dir)
        return 0

    print("=" * 66)
    print("手动入库：找到 %d 个 PDF" % len(pdfs))
    print("=" * 66)

    ai = lm._load_module(lm.ASK_IMAGE, "ask_image")
    rn = lm._load_module(lm.RENAMER, "rename_pdfs_ai")
    started = False
    if not ai.health():
        print("正在启动本地视觉模型…")
        started = ai.start_server()
        if not started:
            print("本地模型起不来，无法识别 PDF", file=sys.stderr)
            return 1
    lm.ensure_dirs()

    ok = fail = 0
    try:
        for i, pdf in enumerate(pdfs, 1):
            base = os.path.basename(pdf)
            pngs = []
            try:
                pngs = rn.render_pages(pdf, args.pages, args.dpi)
                reply = ai.ask(pngs, rn.PROMPT.format(n=len(pngs)),
                               max_tokens=700, temperature=0.1)
                meta = rn.parse_json_reply(reply) or {}

                entry, hit_key, how = match_entry(meta, idx)
                if entry:
                    cat = entry.get("category") or "其他"
                    note = "清单命中(%s)" % how
                else:
                    # 清单里没有 → 现场问模型判个主题
                    # ★ 2026-10-01：原来只送 pngs[:1]。Elsevier/Springer/Wiley 的
                    #   PDF 首页常是出版社封面页（免责声明 + 期刊 Logo + 版权），
                    #   真正的标题在第 2 页 ⇒ 只喂第一页模型只能判"其他"。
                    #   送前 2 页，成本只多一张图。
                    rep2 = ai.ask(pngs[:2], CLASSIFY_PROMPT,
                                  max_tokens=80, temperature=0.1)
                    m = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", rep2 or "")
                    cat = "其他"
                    if m:
                        try:
                            cat = (json.loads(m.group(0)).get("category") or "其他").strip()
                        except Exception:
                            pass
                    cat = normalize_category(cat)
                    note = "清单未命中，模型判定"
                    if cat == "其他":
                        note += "（未识别出主题，等人工确认）"

                newname = rn.build_name(meta, args.template,
                                        os.path.splitext(base)[0])
                dst_dir = os.path.join(lm.LIBRARY_DIR, cat)
                dst = lm.unique_path(os.path.join(dst_dir, newname))

                if args.dry_run:
                    print("[%d/%d] %s" % (i, len(pdfs), base[:52]))
                    print("        → [%s] %s   （%s）" % (cat, newname, note))
                else:
                    import shutil
                    os.makedirs(dst_dir, exist_ok=True)
                    shutil.move(pdf, dst)
                    # ★ 原子拷贝进 EndNote 监听目录（原来是裸 shutil.copy2）
                    lm.atomic_copy_into(dst, lm.PDF_INBOX_DIR, newname)

                    # ★ 2026-10-01【销账用哪把键】：优先用 match_entry 在索引里
                    #   命中的那把【标准键】；meta 是 OCR 产物，连字符/长单词
                    #   常有微小偏差 ⇒ lm.key_of(meta) 算出来的键可能根本不在
                    #   索引里，销账销不掉。清单没命中（模型现判）时才退回 OCR 键。
                    k = hit_key or lm.key_of(meta)
                    if k:
                        done.add(k)
                    # ★ 2026-10-01：回报中央去重表。原来 manual_ingest 完全
                    #   活在"状态孤岛"里 —— paper_radar 把这批标成 "listed"，
                    #   用户手动入库后没有任何一处把它推进到 "filed"，
                    #   processed.py --stats 永远显示"待下载"。
                    try:
                        processed.mark(entry or meta, "filed")
                        processed._save()
                    except Exception as _e:
                        print("        [警告] 中央去重表登记失败：%s" % _e)
                    print("[%d/%d] ✓ [%s] %s   （%s）"
                          % (i, len(pdfs), cat, os.path.basename(dst)[:60], note))
                ok += 1
            except Exception as e:
                fail += 1
                print("[%d/%d] ✗ %s — %s: %s"
                      % (i, len(pdfs), base[:44], type(e).__name__, str(e)[:50]))
            finally:
                for p in pngs:
                    try:
                        if p and os.path.exists(p):
                            os.remove(p)
                    except Exception:
                        pass
    finally:
        if started:
            ai.stop_server()

    if not args.dry_run:
        _save_done(done)
    print("\n%s：成功 %d，失败 %d" % ("预览" if args.dry_run else "完成", ok, fail))
    if ok and not args.dry_run:
        print("⇒ PDF 已进 Library\\ 并复制到 PDF_Inbox\\；"
              "若 EndNote 配了「PDF 自动导入文件夹」，十分钟内自动入库。")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
