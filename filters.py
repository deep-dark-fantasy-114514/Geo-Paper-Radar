#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""filters.py —— 两阶段过滤：Regex 粗筛 → 限流 → 双轨制筛选

从 paper_radar.py 拆出（2026-10-01，逐字搬运，未改逻辑）。
"""
import json
import time

from config import *


def _no_abstract(paper):
    """见 config.no_abstract（全仓唯一实现）。薄封装，本文件内的调用点保持不变。"""
    return no_abstract(paper)



# 期刊事务性内容（这类东西没有摘要、标题也不含研究关键词，混进来纯属浪费）
_MATTER_RE = re.compile(
    r"(erratum|corrigendum|retraction|expression of concern|"
    r"editorial board|editorial\b|call for papers|table of contents|"
    r"front matter|back matter|issue information|author index|subject index|"
    r"volume index|annual index|preface|list of reviewers|"
    r"勘误|更正|撤稿|编委|征稿|启事|目录|索引|会议通知)", re.I)


def _looks_like_matter(title):
    """判断标题是不是期刊事务性内容（勘误/编委会/目录/征稿…）。"""
    return bool(_MATTER_RE.search(title or ""))


def local_regex_coarse_filter(papers, min_hits=None):
    """
    第一层：本地 Regex 粗筛

    ★ 2026-10-01 更改：粗筛阈值可调，且 SCORER=local 时自动降到 1。
      原因：粗筛原本的唯一目的是【省 DeepSeek 的 API 费】。改用本地打分后
      这个目的不复存在，而硬卡"≥2 命中"实测只剩 79/1033 篇——**丢掉 93%**，
      正是"漏掉优质文献"的真凶。放宽到 ≥1 后再由本地模型做精细判断。
    """
    # min_hits=None ⇒ 走新的【加权分】规则（见下方）；显式传值则退回计数规则
    print("\n" + "=" * 60)
    print("【第一阶段】本地 Regex 粗筛")
    print("=" * 60)

    # 编译所有关键词为正则（忽略大小写）
    all_keywords = KEYWORD_LIST_EN + KEYWORD_LIST_CN
    # 按长度降序排列以确保长词优先匹配
    all_keywords_sorted = sorted(all_keywords, key=len, reverse=True)

    passed, n_bypass, n_matter = [], 0, 0
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
            # ★ 2026-10-01：加一道轻量闸 —— 挡掉"期刊事务性内容"。
            #   原来是【标题也不看，摘要为空就全放行】。实测当前样本里
            #   TOC/勘误/编委会是 0 例，但这类东西一旦混进来，
            #   每一篇都要白花 0.7 秒让模型判"不相关"。
            if _looks_like_matter(title):
                paper["regex_hits"] = ["<事务性内容·丢弃>"]
                n_matter += 1
                continue
            paper["regex_hits"] = ["<无摘要·全收>"]
            passed.append(paper)
            n_bypass += 1
            continue

        # ★ 2026-10-01：从"命中 ≥N 个词"改成【加权计分】——
        #   锚定词×2 + 泛化词×1（泛化词表见 config.GENERIC_KEYWORDS）。
        #   理由见 config.py 里那段注释：只凭一个泛词通过的都是噪音。
        #   min_hits 仍然保留：显式传了就退回老的"计数"语义，方便对照。
        # ★ 2026-10-01 修：关键词表里有【包含关系】（"landslide" ⊂ "shallow landslide"、
        #   "降雨入渗" ⊃ "入渗"，实测 10 组），逐个匹配会让同一处文字被计两次：
        #   一篇写 "shallow landslide" 的论文拿到 4 分而不是 2 分。
        #   这不只是分数虚高 —— 【会改变判定】：只命中"稳定性分析"（泛化词）的论文，
        #   重复计分 = 2 分通过，去重后 = 1 分本该被砍。
        #   改法：all_keywords_sorted 是长→短排的，命中后若已被更长的命中词包含，
        #   就不再单独计分。
        matched = []
        for kw in all_keywords_sorted:            # 长 → 短
            kl = kw.lower()
            if kl not in text:
                continue
            if any(kl in prev.lower() for prev in matched):
                continue                          # 已被更长的命中词覆盖
            matched.append(kw)
        anchor_hits = [k for k in matched if k not in GENERIC_KEYWORDS]
        generic_hits = [k for k in matched if k in GENERIC_KEYWORDS]

        if min_hits is not None:
            ok = (len(anchor_hits) + len(generic_hits)) >= min_hits
        else:
            ok = (2 * len(anchor_hits) + len(generic_hits)) >= COARSE_MIN_SCORE

        if ok:
            paper["regex_hits"] = (anchor_hits + generic_hits)[:5]
            paper["_regex_score"] = 2 * len(anchor_hits) + len(generic_hits)
            passed.append(paper)

    print(f"  [输入] {len(papers)} 篇 → 粗筛后 {len(passed)} 篇"
          f"（其中 {n_bypass} 篇是无摘要直接放行"
          + (f"，另丢弃 {n_matter} 篇期刊事务性内容" if n_matter else "") + "）")
    _rule = (f"命中 ≥{min_hits} 个关键词" if min_hits is not None
             else f"加权分 ≥{COARSE_MIN_SCORE}（锚定词×2 + 泛化词×1）")
    print(f"  [规则] 有摘要者：{_rule}；无摘要者全收（交本地模型判）")
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
    print(f"\n  [限流] 粗筛后 {len(papers)} 篇超出阈值 {max_count}，"
          f"按来源轮询采样中...")   # ★ 原来是"随机采样"，但实现是确定性轮询
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
# 6. 双轨制筛选（总分 ≥ TOTAL_SCORE_PASS 或 创新分 ≥ 阈值）
#    ⚠️ 原题「模块四：DeepSeek 多维度智能打分（V2.0 复用）」是拆分残留 ——
#       打分早已搬到 scoring.py，这里只剩筛选。
# ══════════════════════════════════════════════


def dual_track_filter(papers):
    print("\n" + "=" * 60)
    print("【双轨制筛选】(V3.0)")
    print("=" * 60)

    papers.sort(key=lambda x: x.get("total_score", 0), reverse=True)
    # ★ 2026-10-01：原来这里读 history 去重 —— 但本函数在【打分之后】才被调用，
    #   已推送过的文献还是先花了一遍算力/费用。history 检查已前置到 main() 的
    #   score_all_papers 之前（那里才是该拦截的地方）。这里不再重复。
    # history 去重已在 main() 打分之前完成，这里不再重复（原来会有 NameError 残留）
    deduped = papers

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
    # ★ 2026-10-01：原来这里是 MAX_EMAIL_RESULTS（=10），
    #   但泛读列表现在要进简报与待下载清单，截到 10 篇会把当天
    #   评出的其余优质泛读文献静默丢掉。改用独立配额。
    if MAX_BROWSING_RESULTS:
        browsing_list = browsing_list[:MAX_BROWSING_RESULTS]

    print(f"  [轨道A] 总分≥{TOTAL_SCORE_PASS}/40: {sum(1 for p in pass_list if 'A' in p.get('pass_track',''))} 篇")
    print(f"  [轨道B] 创新分≥{INNOVATION_PASS}/10: {sum(1 for p in pass_list if 'B' in p.get('pass_track',''))} 篇")
    print(f"  [通关] {len(pass_list)} 篇 → 推送邮件 + .ris")
    print(f"  [备选] {len(browsing_list)} 篇 → 终端 + .ris（进简报与待下载清单）")
    return pass_list, browsing_list


# ══════════════════════════════════════════════
# ★ 2026-10-01：主题黑名单预筛
# ══════════════════════════════════════════════
def blacklist_filter(papers, terms=None):
    """把「用不上」的主题直接刷掉，**放在打分之前** ⇒ 省下这些篇的打分时间。

    黑名单在 research_profile.BLACKLIST_TOPICS，用户原话：
      「地震滑坡、滑坡动力学、古滑坡等，我完全用不上，遇到可以直接刷掉」

    匹配范围：标题 + （有摘要时）摘要。**无摘要的只匹配标题**——
    闭源论文只有标题可比，硬匹配摘要字段会把 "No abstract available" 也算进去。

    返回 (保留, 被刷掉) 两个列表。被刷掉的一律【不进入打分池】。
    """
    terms = BLACKLIST_TOPICS if terms is None else terms
    lows = [t.lower() for t in terms]
    kept, killed = [], []
    for p in papers:
        text = (p.get("title") or "")
        if not BLACKLIST_TITLE_ONLY and not _no_abstract(p):
            text += " " + (p.get("summary") or "")
        text = text.lower()
        hit = next((t for t in lows if t in text), None)
        if not hit:
            kept.append(p)
            continue
        p["_blacklist_hit"] = hit
        # ★ 2026-10-01：删掉原来的 drop=False 分支。
        #   那个分支"保留但强制 0 分"，但这些文献会被 main() 原封不动送进
        #   score_all_papers，大模型重新打分并【覆盖】那个 0 分 ——
        #   黑名单形同虚设。现在一律丢弃。
        killed.append(p)
    return kept, killed
