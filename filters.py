#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""filters.py —— 两阶段过滤：Regex 粗筛 → 限流 → 双轨制筛选

从 paper_radar.py 拆出（2026-10-01，逐字搬运，未改逻辑）。
"""
import json
import time

from config import *


def _no_abstract(paper):
    """OpenAlex 对闭源论文不给摘要时，summary 会被填成 "No abstract available"。"""
    s = (paper.get("summary") or "").strip()
    return (not s) or s.startswith("No abstract")


def local_regex_coarse_filter(papers, min_hits=None):
    """
    第一层：本地 Regex 粗筛

    ★ 2026-10-01 更改：粗筛阈值可调，且 SCORER=local 时自动降到 1。
      原因：粗筛原本的唯一目的是【省 DeepSeek 的 API 费】。改用本地打分后
      这个目的不复存在，而硬卡"≥2 命中"实测只剩 79/1033 篇——**丢掉 93%**，
      正是"漏掉优质文献"的真凶。放宽到 ≥1 后再由本地模型做精细判断。
    """
    if min_hits is None:
        min_hits = COARSE_MIN_HITS if SCORER == "local" else 2
    print("\n" + "=" * 60)
    print("【第一阶段】本地 Regex 粗筛")
    print("=" * 60)

    # 编译所有关键词为正则（忽略大小写）
    all_keywords = KEYWORD_LIST_EN + KEYWORD_LIST_CN
    # 按长度降序排列以确保长词优先匹配
    all_keywords_sorted = sorted(all_keywords, key=len, reverse=True)

    passed, n_bypass = [], 0
    for idx, paper in enumerate(papers, 1):
        title = paper.get("title", "")
        summary = paper.get("summary", "")
        text = (title + " " + summary).lower()

        # ★ 2026-10-01：无摘要的（多是闭源）【不卡关键词】，全放行给本地模型判。
        #   原因：闭源论文只有标题可比，而关键词表是多词精确短语
        #   （如 "slope stability"），标题里换个说法（"rooted slope instability"）
        #   就命中不了 —— 实测 654 篇无摘要里有 440 篇（67%）因此被误杀，
        #   其中包括 Engineering Geology / Journal of Hydrology 的正经论文。
        #   反过来，放宽成"slope/flow/soil"这类宽泛词更糟：会把
        #   "Tafel slope"（电化学）、"soil respiration"（生态）也捞进来。
        #   本地打分免费，让模型看标题判断，比任何词表都准。
        if _no_abstract(paper) and COARSE_NO_ABSTRACT_BYPASS:
            paper["regex_hits"] = ["<无摘要·全收>"]
            passed.append(paper)
            n_bypass += 1
            continue

        # 统计命中关键词数
        hit_count = 0
        hit_words = []
        for kw in all_keywords_sorted:
            if kw.lower() in text:
                hit_count += 1
                hit_words.append(kw)
                if hit_count >= min_hits:
                    break

        if hit_count >= min_hits:
            paper["regex_hits"] = hit_words[:5]  # 记录前 5 个命中词
            passed.append(paper)

    print(f"  [输入] {len(papers)} 篇 → 粗筛后 {len(passed)} 篇"
          f"（其中 {n_bypass} 篇是无摘要直接放行）")
    print(f"  [规则] 有摘要者命中 ≥{min_hits} 个核心关键词；"
          f"无摘要者全收（交本地模型判）")
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
