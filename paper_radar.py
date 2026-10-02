#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paper_radar.py —— 地学文献雷达【主程序 / 入口】

2026-10-01 拆分为 主程序 + 分模块（逐字搬运，逻辑未改）：
    config.py           常量、工具函数、history 读写
    sources.py          各检索源（RSS / OpenAlex / Crossref / 学位论文 / 中文核心刊 / S2 / arXiv）
    filters.py          两阶段过滤（Regex 粗筛 → 限流 → 双轨制筛选）
    scoring.py          打分调度（本地 Qwen 优先，失败回退 DeepSeek）
    download.py         OA PDF 下载
    output.py           .ris 引文 + 邮件
    harvest.py          云端「只攒候选」+ 本地回捞
    library_manager.py  归类 / 重命名 / 简报 / 待下载清单
    manual_ingest.py    手动下载的 PDF 自动入库
    processed.py        全局去重登记表
    local_scorer.py     本地 Qwen 打分本体

定时任务与 GitHub Actions 都调本文件，所以这里必须保持可执行。
"""
import json
import os
import sys
import time

# ★ 2026-10-01【入口先定 BASE_DIR】：必须在导入本项目模块【之前】，
#   否则一旦工作目录不对，程序会在看到任何 sys.path 修补之前就 ImportError。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config import *                                    # noqa: E402,F401,F403

# ══════════════════════════════════════════════
# ★ 云端模式的【懒加载】
# ══════════════════════════════════════════════
# 原来所有模块（sources/filters/scoring/download/output/library_manager…）
# 都在文件顶部无条件导入，然后才 `if HARVEST_MODE: return run_harvest()`。
# 那只做到"运行时不使用"这些模块，没做到"云端环境不需要它们"——
# 任何一个模块在导入期引入重依赖（GPU 库、PDF 库、本地模型桥…），
# 云端 GitHub Actions 就会在读到 HARVEST_MODE 之前先崩。
# 云端只需要：抓取 + 粗筛 + 写 JSON。所以那里只导入这几个。
if HARVEST_MODE:
    from harvest import fetch_all_candidates, run_harvest     # noqa: E402
else:
    from sources import (fetch_papers_from_rss, OpenAlexFetcher,   # noqa: E402,F401
                         fetch_crossref, fetch_crossref_journals,
                         fetch_openalex_dissertations, fetch_openalex_journals,
                         dedupe_by_title)
    from filters import (local_regex_coarse_filter,          # noqa: E402,F401
                         limit_for_deepseek, dual_track_filter, blacklist_filter)
    from scoring import score_all_papers                     # noqa: E402,F401
    from download import download_all_oa_pdfs                 # noqa: E402,F401
    from output import (generate_ris_file, build_html_email_v3,  # noqa: E402,F401
                        send_email)
    from harvest import merge_harvest                        # noqa: E402,F401


# ══════════════════════════════════════════════
# ★ 未送达邮件的暂存（2026-10-01 新增）
# ══════════════════════════════════════════════
# 为什么需要：`processed.mark_many(...,"seen")` 在【发邮件之前】就提交了，
# 而 filter_new 拦的是"键在不在表里"（不管什么状态）。于是 SMTP 抖一下，
# 当天的通关文献就【永久不再推送】—— 邮件既没送到，系统也认为已处理完。
# 这是状态机提交点错误：'seen' 同时被当成了"已评分""已处理""已交付"。
# 现在把没送出去的记下来，下一轮开工时并进邮件补发。
PENDING_EMAIL = os.path.join(BASE_DIR, "pending_email.json")
_PENDING_FIELDS = ("title", "link", "doi", "source", "data_source", "year",
                   "authors", "total_score", "reason", "tldr",
                   "slope_stability", "rainfall_infiltration",
                   "preferential_flow", "method_innovation",
                   "dual_role", "scale", "approach", "summary")


def _save_pending_email(papers):
    """原子写：把本轮没送达的那些暂存下来。"""
    try:
        keep = [{k: p.get(k) for k in _PENDING_FIELDS} for p in papers]
        tmp = PENDING_EMAIL + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(keep, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PENDING_EMAIL)
        print(f"  📮 已暂存 {len(keep)} 篇，下次运行会自动补发")
    except Exception as e:
        print(f"  [警告] 暂存未送达文献失败：{type(e).__name__}: {e}")


def _load_pending_email():
    try:
        with open(PENDING_EMAIL, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _clear_pending_email():
    try:
        if os.path.exists(PENDING_EMAIL):
            os.remove(PENDING_EMAIL)
    except Exception:
        pass


# ══════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════

def main():
    # ★ 云端模式：只攒候选就退出（不打分、不发邮件、不下载）
    if HARVEST_MODE:
        return run_harvest()

    # ★ 2026-10-02：启动就自检配置。这些参数直接决定业务状态
    #   （发几篇邮件、下几篇 PDF、花不花钱），但原来只有定义、没有校验。
    _cfg_problems = validate_config()
    if _cfg_problems:
        print("\n[中止] 配置有问题，先修好再跑（上面列了具体哪几条）。")
        return 2

    print("\n" + "🌟" * 30)
    print("  Geo_Paper_Radar V3.0 — 地学文献雷达启动")
    print("  数据源: RSS + OpenAlex  |  过滤: 两阶段  |  双轨制筛选")
    print("🌟" * 30 + "\n")

    start_time = time.time()
    # ★ 2026-10-01：集中记录【不可降级的失败】。末尾据此决定退出码。
    #   判据不是"有没有出警告"，而是"业务语义有没有被悄悄改变"：
    #     · 网络源挂掉  → 可降级（少一个源），不记
    #     · 去重表/黑名单/历史挂掉 → **fail-open 方向反了**，必须记
    #     · 邮件发不出去 → 用户拿不到当天的推送，必须记
    problems = []

    def _note(msg):
        problems.append(msg)
        print(f"  [!! 严重] {msg}")

    all_papers = []

    # ==========================
    # 阶段 A: 双数据源抓取
    # ==========================

    # A1: RSS 抓取
    # ★ 2026-10-01 修：原来裸奔调用。RSS 源常遇 DNS 失败 / SSL 过期 / 503，
    #   一抛异常就【崩在启动阶段】，连后面的 merge_harvest（把云端攒的候选捞回来）
    #   都执行不到 —— 而那正是笔记本睡过觉之后的唯一补救途径。
    #   所以每个源都要兜住，坏一个不影响其余。
    try:
        rss_papers = fetch_papers_from_rss()
        if rss_papers:
            all_papers.extend(rss_papers)
    except Exception as e:
        print(f"  [警告] RSS 抓取失败（继续跑其余源）：{type(e).__name__}: {e}")

    # A2: OpenAlex 抓取
    try:
        oa_fetcher = OpenAlexFetcher(mailto=SMTP_SENDER)
        oa_papers = oa_fetcher.fetch_papers()
        if oa_papers:
            all_papers.extend(oa_papers)
    except Exception as e:
        print(f"  [警告] OpenAlex 抓取失败（继续跑其余源）：{type(e).__name__}: {e}")

    # A3: ★ Crossref 抓取（2026-10-01 新增）—— 实测 91% 是 OpenAlex 没有的
    if USE_CROSSREF:
        try:
            import sys as _sys
            if BASE_DIR not in _sys.path:
                _sys.path.insert(0, BASE_DIR)
            import sources as _src
            print("\n" + "=" * 60)
            print("【Crossref 源】主题检索 + 期刊目录（替代已死的 RSS）")
            print("=" * 60)
            cr = _src.fetch_crossref(CROSSREF_QUERIES,
                                     days=OPENALEX_DAYS_LOOKBACK,
                                     rows=CROSSREF_ROWS)
            cr += _src.fetch_crossref_journals(days=OPENALEX_DAYS_LOOKBACK)
            all_papers.extend(cr)
            print(f"  [Crossref] 合计 {len(cr)} 篇")
        except Exception as e:
            print(f"  [警告] Crossref 抓取失败：{type(e).__name__}: {e}")
    if USE_ARXIV:
        try:
            import sources as _src
            # ★ 2026-10-01：查询词移入 config.ARXIV_QUERIES ——
            #   harvest.py 的同名函数原来漏了 arXiv 这一支，两处共用同一份配置
            #   才能保证云端与本地抓到的是同一批。
            all_papers.extend(_src.fetch_arxiv(ARXIV_QUERIES, ARXIV_PER_QUERY))
        except Exception as e:
            print(f"  [警告] arXiv 抓取失败：{e}")

    # A4: ★ 学位论文（2026-10-01 新增）
    if USE_DISSERTATIONS:
        try:
            import sources as _src
            print("\n" + "=" * 60)
            print("【学位论文源】OpenAlex type:dissertation")
            print("=" * 60)
            ds = _src.fetch_openalex_dissertations(
                DISSERTATION_QUERIES, days=DISSERTATION_DAYS,
                per_query=DISSERTATIONS_PER_QUERY)
            all_papers.extend(ds)
            print(f"  [学位论文] 合计 {len(ds)} 篇")
        except Exception as e:
            print(f"  [警告] 学位论文抓取失败：{type(e).__name__}: {e}")

    # A5: ★ 中文核心刊（2026-10-01 新增，只能到题目级别）
    if USE_CN_JOURNALS:
        try:
            import sources as _src
            print("\n" + "=" * 60)
            print("【中文核心刊源】OpenAlex 按 ISSN（Crossref 几乎没有）")
            print("=" * 60)
            cj = _src.fetch_openalex_journals(days=CN_JOURNAL_DAYS)
            all_papers.extend(cj)
            print(f"  [中文核心刊] 合计 {len(cj)} 篇")
        except Exception as e:
            print(f"  [警告] 中文核心刊抓取失败：{type(e).__name__}: {e}")

    # ★ 2026-10-01：把云端（GitHub Actions）在笔记本睡着时攒下的候选合并进来。
    #   放在"无数据就退出"之前——万一本地网络抽风抓不到，云端攒的还是能兜住。
    # ★ 2026-10-01：merge_harvest 是"笔记本睡过觉之后唯一的补救途径"，
    #   原来却【没有异常边界】—— 它一抛，后面的去重/打分/邮件全不执行。
    #   它内部对取文件已经逐日兜住了，但这里再包一层，防的是它自己写文件/解析出错。
    try:
        all_papers = merge_harvest(all_papers)
    except Exception as e:
        print(f"  [警告] 云端候选回捞失败（继续用本地抓到的）："
              f"{type(e).__name__}: {e}")

    if not all_papers:
        print("\n[结果] 所有数据源均无新文献，任务结束")
        return 0

    # 全局去重
    # ★ 2026-10-01 修：原来只做 title.strip().lower()，跨源的细微差异
    #   （尾部句点 / HTML 实体 &amp; / <i> 标签 / 连续空白）全都识别不出。
    #   改用 sources.dedupe_by_title()：**优先按规范化 DOI，其次按清洗后的标题**。
    #   （这个函数早就写好了、也 import 了，却一直没被调用 —— 顺手修正。）
    before = len(all_papers)
    try:
        all_papers = dedupe_by_title(all_papers)
    except Exception as e:
        # 去重挂了不能"当没重复"继续跑 —— 那会给用户推重复文献。
        _note(f"跨源去重失败，本轮中止以免重复推送：{type(e).__name__}: {e}")
        return 1
    deduped_global = all_papers
    if before != len(deduped_global):
        print(f"  [跨源去重] {before} → {len(deduped_global)} 篇")

    total = len(deduped_global)
    # ★ 2026-10-01 修：原来硬编码只统计 RSS / OpenAlex，
    #   Crossref / OpenAlex-Diss / OpenAlex-CN 在日志里【完全隐身】，
    #   显示"总共 150 篇 = RSS 10 + OpenAlex 20"这种对不上的数。
    from collections import Counter
    _by_src = Counter(p.get("data_source", "?") for p in deduped_global)
    print(f"\n{'=' * 60}")
    print(f"📊 全局合并共 {total} 篇，按来源：")
    for _s, _n in _by_src.most_common():
        print(f"     {_s:16s} {_n:5d} 篇")
    print(f"{'=' * 60}")

    if not deduped_global:
        print("\n[结果] 合并后无有效文献，任务结束")
        return 0

    # ==========================
    # 阶段 B: 两阶段过滤
    # ==========================

    # B1: 第一层 — 本地 Regex 粗筛
    # ★ 2026-10-01：原来无保护。粗筛一挂，后面全不执行；而且"跳过粗筛直接往下"
    #   也不对 —— 那会把全部 ~1900 篇送进打分（本地约 45 分钟）。
    #   这是【不可降级】的失败：它意味着整个过滤契约坏了，宁可中止并报错。
    try:
        coarse_papers = local_regex_coarse_filter(deduped_global)
    except Exception as e:
        _note(f"粗筛失败，本轮中止（不降级为「不过滤」——那会白烧 45 分钟 GPU）："
              f"{type(e).__name__}: {e}")
        return 1
    if not coarse_papers:
        print("\n[结果] 粗筛后无文献通过，任务结束")
        return 0

    # B2: ★ 全局去重（2026-10-01 新增）
    # 为什么必须在打分之前：OpenAlex 回看 7 天 ⇒ 同一篇会连续 7 天进候选池。
    # 以前每天重下重命名 ⇒ Library 里 xxx.pdf / xxx (1).pdf / xxx (2).pdf 一路堆。
    # 放在这里还能顺手省掉重复打分的时间。
    try:
        import sys as _sys
        if BASE_DIR not in _sys.path:
            _sys.path.insert(0, BASE_DIR)
        import processed
        coarse_papers = processed.filter_new(coarse_papers)
    except Exception as e:
        # ★ 2026-10-01：这是【fail-open 方向反了】的一处。去重表坏掉还继续跑，
        #   等于把过去所有已处理文献当新文献重打一遍分并重新推送 —— 用户看到的是
        #   一堆早就看过的文献。降级走"不去重"比中止更糟，所以要记进 problems。
        _note(f"去重表不可用，本轮会重复处理历史文献：{type(e).__name__}: {e}")

    # B2b: ★ 主题黑名单预筛（2026-10-01）
    # 用户原话：「地震滑坡、滑坡动力学、古滑坡等，我完全用不上，遇到可以直接刷掉」
    # 放在打分之前 ⇒ 这些篇根本不用花那 1.4 秒。
    try:
        before = len(coarse_papers)
        coarse_papers, _killed = blacklist_filter(coarse_papers)
        if _killed:
            print(f"  [拉黑] 刷掉 {len(_killed)} 篇（{before} → {len(coarse_papers)}）")
            for _p in _killed[:5]:
                print(f"     ✗ [{_p.get('_blacklist_hit')}] {_p.get('title','')[:56]}")
            if len(_killed) > 5:
                print(f"     …另有 {len(_killed)-5} 篇")
    except Exception as e:
        # ★ 2026-10-01：也是 fail-open 方向反了 —— 你明确说了"这些主题我不要"，
        #   过滤器坏掉却退化成"全都要"。必须记进 problems。
        _note(f"黑名单预筛失败，本轮会把拉黑主题也推给你：{type(e).__name__}: {e}")

    # B3: 限流
    # ★ 2026-10-01：本地打分免费 ⇒ 默认【不设上限】，粗筛通过的全送。
    #   DeepSeek 路径仍按 MAX_DEEPSEEK_INPUT=40 卡住，防止 API 费用暴涨。
    _cap = (MAX_CANDIDATES if SCORER == "local" else MAX_DEEPSEEK_INPUT)
    if _cap:
        deepseek_input = limit_for_deepseek(coarse_papers, _cap)
    else:
        # 不限：不采样、不打散，粗筛通过的全送（顺序即来源优先级）
        deepseek_input = list(coarse_papers)
        print(f"  [不限流] 上限已取消，粗筛通过的 {len(deepseek_input)} 篇全部送打分"
              f"（预计 {len(deepseek_input)*1.1/60:.0f} 分钟）")

    # B4: ★ 摘要补全（2026-10-01 新增）
    # 只对【将要打分的那些】做，避免为低产出耗时间。
    # 顺序：先 Crossref（便宜、快），再网页（慢、且只在 Copernicus 类平台有效）。
    if ABSTRACT_ENRICH_CROSSREF or ABSTRACT_ENRICH_WEB:
        try:
            import sources as _src
            n0 = sum(1 for p in deepseek_input if no_abstract(p))
            print(f"\n  [摘要补全] 待打分 {len(deepseek_input)} 篇，"
                  f"其中无摘要 {n0} 篇")
            if n0:
                if ABSTRACT_ENRICH_CROSSREF:
                    _src.enrich_abstracts(deepseek_input,
                                          max_lookups=ABSTRACT_ENRICH_CROSSREF)
                if ABSTRACT_ENRICH_WEB:
                    _src.enrich_abstracts_web(deepseek_input,
                                              max_lookups=ABSTRACT_ENRICH_WEB)
                n1 = sum(1 for p in deepseek_input if no_abstract(p))
                print(f"  [摘要补全] 无摘要 {n0} → {n1} 篇")
        except Exception as e:
            print(f"  [警告] 摘要补全失败：{type(e).__name__}: {e}")

    # ==========================
    # 阶段 C: DeepSeek 打分 + 双轨制
    # ==========================

    # C0: ★ 已推送过的不再重复打分（2026-10-01）
    #   原来这道检查在 dual_track_filter 里，而那是在【打分之后】才跑的 ——
    #   已推送过的文献还是先花了一遍算力/费用。挪到打分之前才叫拦截。
    try:
        _hist = load_history()
        _b0 = len(deepseek_input)
        deepseek_input = [p for p in deepseek_input
                          if make_link_key(p) not in _hist]
        if _b0 != len(deepseek_input):
            print(f"  [历史去重] 已推送过 {_b0 - len(deepseek_input)} 篇，不再重复打分")
    except Exception as e:
        print(f"  [警告] 历史去重失败（本轮不拦）：{e}")

    # C1: 细筛（默认走本地 Qwen，失败才回退 DeepSeek）
    scored_papers = score_all_papers(deepseek_input, phase_label="细筛")
    if not scored_papers:
        # ★ 2026-10-01 修：原来写死 "DeepSeek 打分全部失败"，
        #   但默认配置是本地 Qwen —— 日志会把排查方向指错。
        _scorer_name = ("本地 Qwen3.5-9B" if SCORER == "local" else "DeepSeek")
        print(f"\n[结果] {_scorer_name} 打分全部失败，任务结束")
        if SCORER == "local":
            print("   ↳ 本地模型起不来？检查显卡占用（MinerU 会抢显存）、"
                  "或临时用 PAPER_RADAR_SCORER=deepseek 跑一轮")
        return 1      # ★ 打分全失败 = 业务失败，退出码必须非零

    # C1.5: ★ 把「只有标题·未打分」的挑出来（用户 2026-10-01 定）
    #   标题信息量不足以支撑四维细粒度评分，硬打分只会误导筛选。
    #   这些篇只做了【相关/不相关】二分类，不进双轨制，单独走简报 + 待下载清单。
    titleonly_list = [p for p in scored_papers
                      if p.get("_title_only") and p.get("relevant")]
    scored_only = [p for p in scored_papers if not p.get("_title_only")]
    if titleonly_list:
        print(f"\n  [只标题·未打分] {len(titleonly_list)} 篇判为相关 —— "
              f"不进双轨制，改走简报与待下载清单，供你人工判断")

    # C2: 双轨制筛选（只对【打过四维分】的做）
    # ★ 2026-10-01：加异常边界。原来一抛就整个任务结束，连邮件都没有。
    #   降级方向取【保守】—— 筛不出来就当作"没有通关文献"，不发邮件，
    #   而不是乱发。同时记进 problems 让退出码非零。
    try:
        # ★ 2026-10-02：第三个返回值是【通关但超出邮件上限】的。
        #   它们照常进简报 / 待下载清单 / .ris，只是不发邮件 ——
        #   以前这一步是被 `pass_list[:MAX_EMAIL_RESULTS]` 直接抹掉的。
        pass_list, browsing_list, overflow_list = dual_track_filter(scored_only)
    except Exception as e:
        _note(f"双轨制筛选失败，本轮不发邮件（避免误推）：{type(e).__name__}: {e}")
        pass_list, browsing_list, overflow_list = [], [], []
    # 打通关+溢出+泛读 = 所有"值得进简报与待下载清单"的
    digest_browse = overflow_list + browsing_list

    # ★ 2026-10-01：把打过分的一律登记（不管分高分低），下次不再重复打分
    try:
        import processed as _proc
        _proc.mark_many(scored_papers, "seen")
    except Exception as e:
        print(f"  [警告] 去重表登记失败：{e}")

    # ==========================
    # 阶段 D: .ris 生成
    # ==========================

    ris_generated = 0
    ris_files = []  # 收集所有 .ris 文件路径，用于邮件附件
    if pass_list:
        print(f"\n{'=' * 60}")
        print("【EndNote 联动】生成 .ris 引文文件")
        print("=" * 60)
        for p in pass_list:
            fp = generate_ris_file(p)
            if fp:
                ris_generated += 1
                ris_files.append(fp)
                ds = p.get("data_source", "?")
                print(f"  ✅ [{ds}] {os.path.basename(fp)}")

    if digest_browse:
        print(f"\n{'=' * 60}")
        print(f"【备选/溢出列表】共 {len(digest_browse)} 篇"
              f"（其中通关但超邮件上限的 {len(overflow_list)} 篇）"
              f"（正文只在终端列，但 .ris 附件【一并发送】）")
        print("=" * 60)
        for idx, p in enumerate(digest_browse, 1):
            ts = p.get("total_score", 0)
            mi = p.get("method_innovation", 0)
            ds = p.get("data_source", "?")
            title = p.get("title", "")[:70]
            print(f"  {idx}. [{ds}][{ts}/40] {title} (创新:{mi}/10)")
            fp = generate_ris_file(p)
            if fp:
                ris_generated += 1
                ris_files.append(fp)
                print(f"     📄 {os.path.basename(fp)}")

    print(f"\n  [汇总] 共生成 {ris_generated} 个 .ris 文件")

    # ==========================
    # 阶段 D2: ★ OA PDF 自动下载（2026-10-01 新增）
    # ==========================
    # 下到 PDF_Inbox ⇒ EndNote 的「PDF 自动导入文件夹」会自动入库，
    # 从而免去"去 QQ 邮箱手动下载 .ris 再导入"这一步。
    # ★ 2026-10-01：上轮归档失败 / 被限额推迟的那些（processed 状态为
    #   failed / deferred）会带 `_retry_archive` 标记。它们【无条件】进这一批，
    #   并且必须【排在最前面】：
    #     · 无条件 —— 否则要重新挤过 pass/browse 阈值才轮得到重试，而重打分
    #       有微小漂移，一旦低了 1 分这篇就永远出不来，重试机制形同虚设。
    #     · 最前面 —— download_all_oa_pdfs 是 `oa_papers[:PDF_MAX_PER_RUN]` 取批，
    #       排在队尾的话会被【再一次】限额切掉，等于永远轮不到（这是我上一轮
    #       加 _retry 时留下的漏洞：当时是 `+ _retry` 拼在末尾）。
    #   （不会重复下载：download_oa_pdf 在暂存区里命中原文件就幂等返回。）
    import processed as _proc_pk
    _seen_keys, _retry = set(), []
    for p in scored_papers:
        if not p.get("_retry_archive"):
            continue
        _k = _proc_pk.key_of(p)
        if _k in _seen_keys:          # 同一个键只放一次，防重复请求/重复日志
            continue
        _seen_keys.add(_k)
        _retry.append(p)
    if _retry:
        print(f"\n  [归档重试] {len(_retry)} 篇上轮归档失败/被限额推迟，"
              f"排在本轮批次【最前面】")
    # ★ 2026-10-01：下载是【可降级】环节 —— 它挂了不该连累简报和邮件。
    #   原来裸调用，一旦抛异常，后面 F2 待下载清单 / F 简报 / E 邮件【全都不执行】，
    #   用户当天什么都收不到。下载函数内部已经逐篇兜住，这里防的是它自己出问题。
    _batch = _retry + pass_list + digest_browse
    try:
        pdf_saved, filed_keys, deferred_keys = download_all_oa_pdfs(_batch)
    except Exception as e:
        print(f"  [警告] OA PDF 下载环节异常，跳过（简报与邮件照常）："
              f"{type(e).__name__}: {e}")
        pdf_saved, filed_keys, deferred_keys = [], set(), set()
    # deferred_keys = 本轮因 PDF_MAX_PER_RUN 限额仍【没轮到】的（已在
    # download.py 里登记成可重试的 "deferred" 状态）⇒ 不进待下载清单，
    # 次日会被 filter_new 放行并再次排到最前面。
    _settled = filed_keys | deferred_keys

    # ==========================
    # 阶段 F2: ★ 待下载清单（2026-10-01 新增）
    # ==========================
    # 高分但【没能自动拿到 PDF】的（闭源，或 Wiley/MDPI 的 403 拦截）
    # ⇒ 出一份含 DOI 的清单；你手动下载后丢进 E:\论文\手动下载\，
    #   跑一次 manual_ingest.py 就自动重命名+归类+进 EndNote。
    dl_csv, dl_n = None, 0
    try:
        import sys as _sys
        if BASE_DIR not in _sys.path:
            _sys.path.insert(0, BASE_DIR)
        import library_manager as _lm2
        _lm2.ensure_dirs()
        dl_csv, dl_n = _lm2.build_download_list(
            pass_list + digest_browse + titleonly_list, _settled)
        if dl_csv:
            # ★ 登记为 listed，避免同一篇明天又被列一次
            try:
                import processed as _proc
                for _p in (pass_list + digest_browse):
                    if _proc.key_of(_p) not in _settled:
                        _proc.mark(_p, "listed")
                _proc._save()
            except Exception as e:
                # ★ 2026-10-01：原来 `except: pass` —— 状态没写进去却一声不吭，
                #   次日同一篇会【又出现在待下载清单里】，用户还不知道为什么。
                print(f"  [警告] listed 状态写入失败（次日可能重复列出）："
                      f"{type(e).__name__}: {e}")
            print(f"\n📥 待下载清单（{dl_n} 篇）：{dl_csv}")
            print(f"   手动下载后丢进 {_lm2.MANUAL_DROP_DIR}，"
                  f"再跑 python manual_ingest.py")
    except Exception as e:
        print(f"\n[警告] 待下载清单生成失败：{type(e).__name__}: {e}")

    # ==========================
    # 阶段 F: ★ 文献简报（2026-10-01 新增）
    # ==========================
    # 告诉你"什么文献可能需要精读"——按总分排序，附四维分与中文理由
    digest_path = None
    try:
        import sys as _sys
        if BASE_DIR not in _sys.path:
            _sys.path.insert(0, BASE_DIR)
        import library_manager as _lm
        _lm.ensure_dirs()
        # ★ 2026-10-01：把【实际】用的打分器传进去 —— 本地挂掉静默回退
        #   DeepSeek 时，简报原来照样写"本地模型打分"。
        _sc_name = None
        try:
            import scoring as _scm
            _sc_name = (getattr(_scm, "LAST_RUN", {}) or {}).get("scorer")
        except Exception:
            pass
        digest_path = _lm.build_digest(pass_list, digest_browse,
                                       len(scored_papers),
                                       time.time() - start_time,
                                       titleonly=titleonly_list,
                                       scorer=_sc_name,
                                       overflow_n=len(overflow_list))
        print(f"\n📋 文献简报：{digest_path}")
    except Exception as e:
        print(f"\n[警告] 简报生成失败：{type(e).__name__}: {e}")

    # ==========================
    # 阶段 E: 空转保护 + 邮件
    # ==========================

    if not pass_list:
        print("\n" + "=" * 60)
        print("【结果】今日无通关文献")
        if digest_browse:
            print(f"📖 有 {len(digest_browse)} 篇备选/溢出已存 EndNote_Watch")
        print("📭 未发送邮件。")
        print("=" * 60)
    else:
        print(f"\n[进入] 准备推送 {len(pass_list)} 篇通关文献...")
        # ★ 2026-10-01：把【上轮没发出去的】先并进来一起发。
        #   见下面邮件失败分支里的说明。
        _carry = _load_pending_email()
        if _carry:
            _have = {make_link_key(p) for p in pass_list}
            _add = [p for p in _carry if make_link_key(p) not in _have]
            if _add:
                print(f"  [补发] 并入上轮未送达的 {len(_add)} 篇")
                pass_list = pass_list + _add
        html_content = build_html_email_v3(pass_list)
        success = send_email(html_content, attachments=ris_files)
        if success:
            links = [make_link_key(p) for p in pass_list]
            save_history(links)
            print("✅ 历史记录已更新")
            _clear_pending_email()
        else:
            # ★★★ 2026-10-01 修【状态提交点错误】★★★
            #   原来这里什么都不做 —— 而 pass_list 里的每一篇【早就】在
            #   `processed.mark_many(scored_papers, "seen")` 里被登记过了。
            #   filter_new 认的是"键在不在表里"（任何状态都拦）⇒
            #   次日它们不会再进打分池 ⇒ 不会再进 pass_list ⇒ **永远不会再发**。
            #   即：SMTP 抖一下，当天那十几篇就永久消失（数据其实还在
            #   EndNote_Watch 的 .ris 里，但用户根本不会知道）。
            #   ⇒ 把没送达的暂存下来，下一轮开工时并进邮件一起补发。
            _note(f"邮件发送失败，{len(pass_list)} 篇已暂存待下次补发")
            _save_pending_email(pass_list)

    # ==========================
    # 结束
    # ==========================

    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"🏁 V3.0 任务完成！总耗时: {elapsed:.1f} 秒")
    print(f"📊 抓取 {total} 篇（{len(_by_src)} 个源）"
          f" → 粗筛 {len(coarse_papers)}"
          f" → 打分 {len(scored_only)}"
          f" → 通关 {len(pass_list)} + 溢出 {len(overflow_list)} + 备选 {len(browsing_list)}"
          + (f" + 只标题 {len(titleonly_list)}" if titleonly_list else ""))
    print(f"   （打分器：{'本地 Qwen3.5-9B' if SCORER == 'local' else 'DeepSeek'}；"
          f"{'免 token 成本' if SCORER == 'local' else '按量计费'}）")
    print(f"📬 邮箱: {SMTP_RECEIVER}  |  📁 .ris: {ENDNOTE_WATCH_DIR}")
    print(f"📥 OA PDF: {PDF_INBOX_DIR}（已存 {len(pdf_saved)} 篇）")
    if pdf_saved:
        print("   ↳ 若 EndNote 已配置「PDF 自动导入文件夹」指向该目录，将自动入库")
    if digest_path:
        print(f"📋 文献简报: {digest_path}")
    if dl_csv:
        print(f"📥 待下载清单: {dl_csv}（{dl_n} 篇）")
    print(f"🧠 打分器: {SCORER}（local = 本地 Qwen3.5-9B，免费）")
    # ★ 2026-10-01：上面那几行报的是【配置里写的】打分器，而实际跑的是哪个
    #   score_all_papers 说了算 —— 本地挂了会静默回退 DeepSeek。
    #   拿配置当运行事实会让人误判成本（以为免费，实际烧了 API）。
    try:
        import scoring as _sc
        _r = getattr(_sc, "LAST_RUN", None)
        if _r:
            print(f"   实际使用: {_r['scorer']}"
                  + ("（★ 已回退 DeepSeek，本批【按量计费】）"
                     if _r.get("fallback") else "（无回退）"))
    except Exception:
        pass
    print(f"{'=' * 60}")

    if problems:
        print(f"\n⚠️  本轮有 {len(problems)} 项【严重问题】（业务语义已被改变）：")
        for _p in problems:
            print(f"     · {_p}")
        return 1
    return 0



if __name__ == "__main__":
    # ★ 2026-10-01：必须把 main() 的返回值传给操作系统。
    #   原来裸 `main()` —— 返回值被丢掉，进程永远退出码 0。
    #   于是 harvest 失败时：GitHub Actions / 计划任务看到的是 **Success**，
    #   根本不会告警。无人值守跑的东西，"失败要能被看见"是最低要求。
    sys.exit(main() or 0)
