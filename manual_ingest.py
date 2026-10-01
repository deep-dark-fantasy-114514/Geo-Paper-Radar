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

DEFAULT_TEMPLATE = "{year}_{author}_{title_zh}"
INGESTED_MARK = os.path.join(lm.DOWNLOAD_LIST_DIR, "_已入库.json")

CLASSIFY_PROMPT = (
    "这是一篇地学论文的标题页。请只输出一个 JSON，不要解释：\n"
    '{"category": "<四选一>"}\n'
    "四选一：优先流 / 降雨入渗 / 边坡稳定 / 方法创新\n"
    "判断依据：\n"
    "- 优先流：大孔隙流、根土间隙流、优势流、裂隙优先流\n"
    "- 降雨入渗：雨水入渗过程、非饱和渗流、入渗模型、湿润锋\n"
    "- 边坡稳定：滑坡机理、稳定性分析、加固、灾害评价\n"
    "- 方法创新：以上都不是但方法本身有突破（新模型/新实验手段/新数据）\n"
    "都不沾就填 其他。"
)


# --------------------------------------------------------------- 清单索引
def load_pending_index():
    """把所有待下载清单读成 {key: 条目} 的索引。"""
    idx = {}
    for p in sorted(glob.glob(os.path.join(lm.DOWNLOAD_LIST_DIR, "*.csv"))):
        try:
            import csv
            with io.open(p, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    doi = (row.get("doi") or "").strip().lower()
                    title = re.sub(r"\s+", " ", (row.get("title") or "").strip().lower())
                    title = re.sub(r"[^0-9a-z一-鿿 ]", "", title)
                    if doi:
                        idx["doi:" + doi] = row
                    if title:
                        idx["title:" + title[:120]] = row
        except Exception as e:
            print("  [警告] 读清单失败 %s：%s" % (os.path.basename(p), e))
    return idx


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


def match_entry(meta, idx):
    """用 DOI（主）或规范化标题（辅）在清单里找出该篇。返回 (条目, 命中方式)。"""
    doi = (meta.get("doi") or "").strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    if doi and ("doi:" + doi) in idx:
        return idx["doi:" + doi], "DOI"
    t = re.sub(r"\s+", " ", (meta.get("title") or "").strip().lower())
    t = re.sub(r"[^0-9a-z一-鿿 ]", "", t)
    if t and ("title:" + t[:120]) in idx:
        return idx["title:" + t[:120]], "标题"
    # 退化匹配：标题前 40 字命中即可（PDF 标题与 OpenAlex 常有细微出入）
    for k, v in idx.items():
        if k.startswith("title:") and len(t) > 25 and k[6:46] == t[:40]:
            return v, "标题(模糊)"
    return None, None


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
        print("待下载清单目录：%s" % lm.DOWNLOAD_LIST_DIR)
        files = sorted(glob.glob(os.path.join(lm.DOWNLOAD_LIST_DIR, "*.csv")))
        if not files:
            print("  （还没有清单。跑一次 paper_radar.py 就会生成）")
            return 0
        total = len(set(id(v) for v in idx.values()))
        print("  清单文件 %d 份，条目约 %d 条，已入库 %d 条"
              % (len(files), total, len(done)))
        for p in files:
            print("    %s" % os.path.basename(p))
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

                entry, how = match_entry(meta, idx)
                if entry:
                    cat = entry.get("category") or "其他"
                    note = "清单命中(%s)" % how
                else:
                    # 清单里没有 → 现场问模型判个主题
                    rep2 = ai.ask(pngs[:1], CLASSIFY_PROMPT,
                                  max_tokens=80, temperature=0.1)
                    m = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", rep2 or "")
                    cat = "其他"
                    if m:
                        try:
                            cat = (json.loads(m.group(0)).get("category") or "其他").strip()
                        except Exception:
                            pass
                    if cat not in ("优先流", "降雨入渗", "边坡稳定", "方法创新"):
                        cat = "其他"
                    note = "清单未命中，模型判定"

                newname = rn.build_name(meta, args.template,
                                        os.path.splitext(base)[0])
                dst_dir = os.path.join(lm.LIBRARY_DIR, cat)
                dst = lm._unique(os.path.join(dst_dir, newname))

                if args.dry_run:
                    print("[%d/%d] %s" % (i, len(pdfs), base[:52]))
                    print("        → [%s] %s   （%s）" % (cat, newname, note))
                else:
                    import shutil
                    os.makedirs(dst_dir, exist_ok=True)
                    shutil.move(pdf, dst)
                    os.makedirs(lm.PDF_INBOX_DIR, exist_ok=True)
                    shutil.copy2(dst, lm._unique(
                        os.path.join(lm.PDF_INBOX_DIR, newname)))
                    k = lm.key_of(meta)
                    if k:
                        done.add(k)
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
