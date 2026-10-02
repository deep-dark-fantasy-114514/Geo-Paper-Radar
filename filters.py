#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""filters.py —— 两阶段过滤：Regex 粗筛 → 限流 → 双轨制筛选

从 paper_radar.py 拆出（2026-10-01），此后已多次独立修正。

★ 2026-10-02：本模块的核心原则改成 **FILTER ≠ DELETE**。
  每一层筛选都只负责"把候选分配到不同的队列"，只有真正的
  REJECT / BLACKLIST 才算丢弃。理由见 `dual_track_filter()` 的注释：
  原来"邮件容量上限"顺手把论文从整个系统里删掉了。
"""
import re                       # ★ 显式导入：原来靠 config 的 import * 偷渡
from collections import defaultdict

from config import *   # noqa: F401,F403


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


# ★ 2026-10-02【标点归一化】。地学标题里 hyphen / en-dash / 斜杠 连接复合词
#   极常见：`slope-stability`、`preferential–flow`、`rainfall/infiltration`。
#   而关键词表里存的是空格形式（`slope stability`）⇒ **子串匹配一律落空**。
#   实测这三种写法当前 0 命中，等于把一整类论文静默漏掉。
#   做法：把各种连接符统一压成空格，文本与关键词**同规则**处理。
_DASH_RE = re.compile(r"[‐-―−­\-‘’/\\&＋+]+")


def _norm_for_match(s):
    """匹配专用归一化：连接符→空格、压空白、转小写。文本与关键词走同一条路。"""
    s = _DASH_RE.sub(" ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip().lower()


_KEYWORDS_NORM = None
_GENERIC_NORM = None


def _keyword_tables():
    """预计算归一化后的关键词表（长→短）与泛化词集合。进程内只算一次。"""
    global _KEYWORDS_NORM, _GENERIC_NORM
    if _KEYWORDS_NORM is None:
        pairs = [(kw, _norm_for_match(kw)) for kw in (KEYWORD_LIST_EN + KEYWORD_LIST_CN)]
        pairs = [(kw, kn) for kw, kn in pairs if kn]
        # 按【归一化后】长度降序，保证"长词覆盖短词"的判定在同一把尺子上
        pairs.sort(key=lambda x: len(x[1]), reverse=True)
        _KEYWORDS_NORM = pairs
        # ★ 泛化词也归一化后再比对 —— 原来依赖"配置文件里两份写法逐字相同"，
        #   改一个大小写/连字符就会把泛化词误判成锚定词（×2 分）。
        _GENERIC_NORM = {_norm_for_match(g) for g in GENERIC_KEYWORDS}
        _GENERIC_NORM.discard("")
    return _KEYWORDS_NORM, _GENERIC_NORM


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

    _kws, _generic_norm = _keyword_tables()

    passed, n_bypass, n_matter = [], 0, 0
    for idx, paper in enumerate(papers, 1):
        title = paper.get("title", "")
        summary = paper.get("summary", "")
        # ★ 与关键词走同一套标点归一化（见 _norm_for_match）
        text = _norm_for_match(title + " " + summary)

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
        matched, matched_norm = [], []
        for kw, kn in _kws:                       # 归一化后 长 → 短
            if kn not in text:
                continue
            if any(kn in prev for prev in matched_norm):
                continue                          # 已被更长的命中词覆盖
            matched.append(kw)
            matched_norm.append(kn)
        # ★ 分类也用归一化后的键比对（原来靠两份配置逐字相同）
        anchor_hits = [k for k, kn in zip(matched, matched_norm)
                       if kn not in _generic_norm]
        generic_hits = [k for k, kn in zip(matched, matched_norm)
                        if kn in _generic_norm]

        if min_hits is not None:
            ok = (len(anchor_hits) + len(generic_hits)) >= min_hits
        else:
            ok = (2 * len(anchor_hits) + len(generic_hits)) >= COARSE_MIN_SCORE

        if ok:
            # ★ 2026-10-02：证据不再只留前 5 个 —— 真实分是
            #   `2*锚定 + 泛化`，只看前 5 个会让日志与判定依据对不上号
            #   （12 个命中显示成 5 个，排查时无法复原为什么这篇过了）。
            paper["regex_hits"] = (anchor_hits + generic_hits)[:12]
            paper["regex_anchor_n"] = len(anchor_hits)
            paper["regex_generic_n"] = len(generic_hits)
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
    """把候选裁到 max_count 篇。

    ★★ 2026-10-02 两处修正 ★★

    ① **分层键原来是错的**。原代码用 `p.get("source")` 分组，注释写
       "尽量保证各来源都有代表"。但在本系统里：
           `data_source` = 数据库（OpenAlex / Crossref / RSS / arXiv / S2 …）
           `source`      = **期刊名**（Engineering Geology / Landslides …）
       ⇒ 实际做的是【按期刊轮询】，不是【按数据源轮询】。
       一个期刊多的库（OpenAlex 一下几十个刊）自然拿到更多席位，
       这跟"让各数据源都有代表"完全是两回事。现在改成 `data_source`。

    ② **组内不做任何价值排序**。原实现 `lst.pop(0)` 取的是输入顺序，
       而输入顺序 = 数据源 append 顺序 ⇒ 于是"40 个席位"给到的
       只是"每个来源最先被抓到的几篇"，而不是"最像相关文献的 40 篇"。
       粗筛阶段已经算出了 `_regex_score`（锚定词×2 + 泛化词×1），
       白白不用太浪费。现在组内按 `_regex_score` 降序。

    （这是"费用堤坝"，不是"价值排序器"—— 但它至少不该把
      最能说明相关性的一列信号丢掉。）
    """
    if len(papers) <= max_count:
        return papers
    print(f"\n  [限流] 粗筛后 {len(papers)} 篇超出阈值 {max_count}，"
          f"按【数据源】轮询 + 组内按 regex 相关度取前几篇")

    by_src = defaultdict(list)
    for p in papers:
        by_src[p.get("data_source", "Unknown")].append(p)
    # 组内排序：regex 分高的优先（同分保持原相对顺序，sorted 是稳定的）
    for _k, lst in by_src.items():
        lst.sort(key=lambda x: x.get("_regex_score", 0) or 0, reverse=True)

    # 轮询各数据源，保证小源不被大源挤光
    sampled, cursors = [], {k: 0 for k in by_src}
    while len(sampled) < max_count:
        added = 0
        for src, lst in by_src.items():
            i = cursors[src]
            if i < len(lst):
                sampled.append(lst[i])
                cursors[src] = i + 1
                added += 1
                if len(sampled) >= max_count:
                    break
        if added == 0:
            break

    _by = defaultdict(int)
    for p in sampled:
        _by[p.get("data_source", "?")] += 1
    print(f"  [结果] 最终送入打分: {len(sampled)} 篇，按数据源："
          + "、".join(f"{k} {v}" for k, v in sorted(_by.items(),
                                                   key=lambda x: -x[1])))
    return sampled


# ══════════════════════════════════════════════
# 6. 双轨制筛选（总分 ≥ TOTAL_SCORE_PASS 或 创新分 ≥ 阈值）
#    ⚠️ 原题「模块四：DeepSeek 多维度智能打分（V2.0 复用）」是拆分残留 ——
#       打分早已搬到 scoring.py，这里只剩筛选。
# ══════════════════════════════════════════════


def _rank_key(p):
    """通关文献的排序键（完全可解释，不留"并列时看输入顺序"的暗门）。

    ★ 2026-10-02：原来只按 `total_score` 排。同分时 Python 保持输入顺序，
      而输入顺序 = 数据源 append 顺序（RSS→OpenAlex→Crossref→…）
      ⇒ **同分论文谁进邮件，实际由"哪个源先抓到"决定**。现在补两级 tie-breaker。
    """
    return (p.get("total_score", 0) or 0,
            p.get("method_innovation", 0) or 0,
            p.get("_regex_score", 0) or 0)


def dual_track_filter(papers):
    """双轨制筛选。返回 `(email_list, browsing_list, overflow_list)`。

    ★★ 2026-10-02【P0 修复：通关论文不再被邮件上限吃掉】★★
      原来最后一步是 `pass_list = pass_list[:MAX_EMAIL_RESULTS]` ——
      **被切掉的那些既没进邮件、也没进 browsing**（它们在 `if track_a or track_b`
      分支里就已经被归入 pass 了，压根到不了 elif）。
      而 paper_radar 随后会把所有 `scored_papers` 标成 `seen`
      ⇒ 它们不是"下次还有机会"，而是**永久消失**。
      实测复现（25 篇全部 35 分通关）：通行 15 篇，**10 篇蒸发**。

    ★★ 同样修掉【轨道B 被总分榜挤光】★★
      原来是"A 轨 + B 轨合并 → 按总分排序 → 一刀切 N 篇"。
      B 轨的意义本是"创新性突出也应有独立晋级机会"，但合并排序后
      它只是"贴了个 B 标签、再去和所有 A 争同一个总分榜"。
      实测：15 篇 A(35分) + 5 篇 B(27分/创新10) ⇒ **5 篇 B 一条都没进邮件**。
      ⇒ 现在给轨道B 预留 `EMAIL_RESERVE_TRACK_B` 个席位（A 不够时席位还给 B）。

    剩下的通关论文进 `overflow_list`：**会进简报、待下载清单与 .ris，只是不发邮件**。
    """
    print("\n" + "=" * 60)
    print("【双轨制筛选】(V3.0)")
    print("=" * 60)

    # ★ 用 sorted() 而不是 papers.sort()：一个名叫 filter 的函数不该
    #   悄悄改掉调用方传入列表的顺序。
    deduped = sorted(papers, key=_rank_key, reverse=True)

    pass_all, browsing_all = [], []
    for p in deduped:
        ts = p.get("total_score", 0)
        mi = p.get("method_innovation", 0)
        track_a = ts >= TOTAL_SCORE_PASS
        track_b = mi >= INNOVATION_PASS
        if track_a or track_b:
            p["pass_track"] = ("A+B" if (track_a and track_b)
                               else ("A" if track_a else "B"))
            pass_all.append(p)
        elif ts >= BROWSING_THRESHOLD:
            browsing_all.append(p)

    # ── 邮件席位分配：A 轨按总分，B 轨按创新分，B 有保底席位 ──
    a_pool = [p for p in pass_all if "A" in p["pass_track"]]
    b_pool = [p for p in pass_all if p["pass_track"] == "B"]
    b_pool.sort(key=lambda p: (p.get("method_innovation", 0) or 0,
                               p.get("total_score", 0) or 0,
                               p.get("_regex_score", 0) or 0), reverse=True)

    b_quota = min(EMAIL_RESERVE_TRACK_B, len(b_pool), MAX_EMAIL_RESULTS)
    email_b = b_pool[:b_quota]
    n_a = max(0, MAX_EMAIL_RESULTS - len(email_b))
    email_a = a_pool[:n_a]
    # A 轨不够时，空出来的席位还给 B 轨（别浪费邮件额度）
    if len(email_a) < n_a:
        email_b += b_pool[b_quota:b_quota + (n_a - len(email_a))]

    email_list = sorted(email_a + email_b, key=_rank_key, reverse=True)
    _chosen = {id(p) for p in email_list}
    overflow_list = [p for p in pass_all if id(p) not in _chosen]

    # ★ 泛读的上限只管【真泛读】；overflow 是"已通关"，不该被泛读配额再切一次
    browsing_list = (browsing_all[:MAX_BROWSING_RESULTS]
                     if MAX_BROWSING_RESULTS else browsing_all)

    print(f"  [轨道A] 总分≥{TOTAL_SCORE_PASS}/40: {len(a_pool)} 篇"
          f"（进邮件 {len(email_a)}）")
    print(f"  [轨道B] 创新分≥{INNOVATION_PASS}/10 且未达总分线: {len(b_pool)} 篇"
          f"（进邮件 {len(email_b)}，保底席位 {EMAIL_RESERVE_TRACK_B}）")
    print(f"  [通关] 共 {len(pass_all)} 篇 → 邮件 {len(email_list)} + "
          f"溢出 {len(overflow_list)}")
    if overflow_list:
        print(f"  [溢出] {len(overflow_list)} 篇已通关但超出邮件上限 "
              f"({MAX_EMAIL_RESULTS}) ⇒ 进简报/待下载清单，**不丢**")
    print(f"  [备选] {len(browsing_list)} 篇 → 终端 + .ris（进简报与待下载清单）")
    return email_list, browsing_list, overflow_list


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
    # ★ 2026-10-02：匹配也用同一套标点归一化，否则 "earthquake-induced landslide"
    #   这类带连字符的黑名单词永远命不中标题里的连字符写法。
    lows = [_norm_for_match(t) for t in terms]
    lows = [t for t in lows if t]
    kept, killed, soft = [], [], 0
    for p in papers:
        t_norm = _norm_for_match(p.get("title"))
        hit = next((t for t in lows if t in t_norm), None)
        if hit:
            # 【标题命中 ⇒ 硬杀】。这是用户的明确意图（"遇到可以直接刷掉"），
            #   而且标题就是论文的研究对象本身 —— 判得准。
            p["_blacklist_hit"] = hit
            # ★ 2026-10-01：删掉原来的 drop=False 分支。
            #   那个分支"保留但强制 0 分"，但这些文献会被 main() 原封不动送进
            #   score_all_papers，大模型重新打分并【覆盖】那个 0 分 ——
            #   黑名单形同虚设。现在一律丢弃。
            killed.append(p)
            continue

        # ★★ 2026-10-02【摘要命中 ⇒ 只降权，不硬杀】★★
        #   原来标题和摘要是**同一个 substring 判据**。而学术摘要里
        #   "Previous studies on earthquake-induced landslides have…"
        #   这种**背景引用**极其常见 —— 一篇标题完全对口的论文，
        #   只因为摘要里提了一句拉黑主题，就被**永久淘汰**（不可逆：
        #   不进打分、不进简报、不进下载清单）。
        #   现在摘要命中只记一个标记，交给自己去打分的模型（它的 SYSTEM
        #   prompt 里本来就写了"遇到这些主题直接给 0 分"）去判断轻重。
        if not BLACKLIST_TITLE_ONLY and not _no_abstract(p):
            s_norm = _norm_for_match(p.get("summary"))
            s_hit = next((t for t in lows if t in s_norm), None)
            if s_hit:
                p["_blacklist_soft"] = s_hit
                soft += 1
        kept.append(p)
    if soft:
        print(f"  [拉黑·软] {soft} 篇摘要把拉黑主题当背景提及 —— "
              f"保留（交打分模型按主体判断），未硬杀")
    return kept, killed
