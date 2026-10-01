#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""harvest.py —— 云端「只攒候选」模式 + 本地回捞

从 paper_radar.py 拆出（2026-10-01，逐字搬运，未改逻辑）。

云端每天只抓取，把候选写成 harvest/YYYY-MM-DD.json 提交回仓库；
本机开机后用 raw HTTP 回捞（**刻意不用 git pull**：本地在 v3.0-dev 分支
且有未提交改动，pull 必冲突；仓库公开所以无需 token）。
"""
import json
import os
import sys
from datetime import datetime, timedelta

import requests

from config import *
from sources import (fetch_papers_from_rss, OpenAlexFetcher,
                     fetch_crossref, fetch_crossref_journals,
                     fetch_openalex_dissertations, fetch_openalex_journals,
                     dedupe_by_title)
from filters import local_regex_coarse_filter, blacklist_filter


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

    # ★ 学位论文 + 中文核心刊（云端 harvest 也要，否则笔记本睡着的那些天收不到）
    if USE_DISSERTATIONS:
        try:
            import sources as _src
            all_papers.extend(_src.fetch_openalex_dissertations(
                DISSERTATION_QUERIES, days=DISSERTATION_DAYS,
                per_query=DISSERTATIONS_PER_QUERY))
        except Exception as e:
            print(f"  [Warning] 学位论文抓取失败：{e}")
    if USE_CN_JOURNALS:
        try:
            import sources as _src
            all_papers.extend(_src.fetch_openalex_journals(days=CN_JOURNAL_DAYS))
        except Exception as e:
            print(f"  [Warning] 中文核心刊抓取失败：{e}")

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
    kept, _bl = blacklist_filter(kept)      # ★ 拉黑主题先刷掉，省存储
    if _bl:
        print(f'  [拉黑] 刷掉 {len(_bl)} 篇')
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
