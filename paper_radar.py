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
import time

from config import *                                    # noqa: F401,F403
from sources import (fetch_papers_from_rss, OpenAlexFetcher,   # noqa: F401
                     fetch_crossref, fetch_crossref_journals,
                     fetch_openalex_dissertations, fetch_openalex_journals,
                     dedupe_by_title)
from filters import (local_regex_coarse_filter,          # noqa: F401
                     limit_for_deepseek, dual_track_filter)
from scoring import score_all_papers                     # noqa: F401
from download import download_oa_pdf, download_all_oa_pdfs   # noqa: F401
from output import (generate_ris_file, build_html_email_v3,  # noqa: F401
                    send_email)
from harvest import (fetch_all_candidates, run_harvest,  # noqa: F401
                     merge_harvest)


# ══════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════

def main():
    # ★ 云端模式：只攒候选就退出（不打分、不发邮件、不下载）
    if HARVEST_MODE:
        return run_harvest()

    print("\n" + "🌟" * 30)
    print("  Geo_Paper_Radar V3.0 — 地学文献雷达启动")
    print("  数据源: RSS + OpenAlex  |  过滤: 两阶段  |  双轨制筛选")
    print("🌟" * 30 + "\n")

    start_time = time.time()
    all_papers = []

    # ==========================
    # 阶段 A: 双数据源抓取
    # ==========================

    # A1: RSS 抓取
    rss_papers = fetch_papers_from_rss()
    if rss_papers:
        all_papers.extend(rss_papers)

    # A2: OpenAlex 抓取
    oa_fetcher = OpenAlexFetcher(mailto=SMTP_SENDER)
    oa_papers = oa_fetcher.fetch_papers()
    if oa_papers:
        all_papers.extend(oa_papers)

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
            all_papers.extend(_src.fetch_arxiv(
                ['all:"preferential flow"', 'all:"slope stability"'], 60))
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
    all_papers = merge_harvest(all_papers)

    if not all_papers:
        print("\n[结果] 所有数据源均无新文献，任务结束")
        return

    # 全局去重
    seen = set()
    deduped_global = []
    for p in all_papers:
        t = p.get("title", "").strip().lower()
        if t and t not in seen:
            seen.add(t)
            deduped_global.append(p)

    total = len(deduped_global)
    rss_count = sum(1 for p in deduped_global if p.get("data_source") == "RSS")
    oa_count = sum(1 for p in deduped_global if p.get("data_source") == "OpenAlex")
    print(f"\n{'=' * 60}")
    print(f"📊 全局合并: RSS {rss_count} 篇 + OpenAlex {oa_count} 篇 = {total} 篇")
    print(f"{'=' * 60}")

    if not deduped_global:
        print("\n[结果] 合并后无有效文献，任务结束")
        return

    # ==========================
    # 阶段 B: 两阶段过滤
    # ==========================

    # B1: 第一层 — 本地 Regex 粗筛
    coarse_papers = local_regex_coarse_filter(deduped_global)
    if not coarse_papers:
        print("\n[结果] 粗筛后无文献通过，任务结束")
        return

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
        print(f"  [警告] 去重表不可用，本轮按不去重处理：{e}")

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
            n0 = sum(1 for p in deepseek_input
                     if (p.get("summary") or "").startswith("No abstract"))
            print(f"\n  [摘要补全] 待打分 {len(deepseek_input)} 篇，"
                  f"其中无摘要 {n0} 篇")
            if n0:
                if ABSTRACT_ENRICH_CROSSREF:
                    _src.enrich_abstracts(deepseek_input,
                                          max_lookups=ABSTRACT_ENRICH_CROSSREF)
                if ABSTRACT_ENRICH_WEB:
                    _src.enrich_abstracts_web(deepseek_input,
                                              max_lookups=ABSTRACT_ENRICH_WEB)
                n1 = sum(1 for p in deepseek_input
                         if (p.get("summary") or "").startswith("No abstract"))
                print(f"  [摘要补全] 无摘要 {n0} → {n1} 篇")
        except Exception as e:
            print(f"  [警告] 摘要补全失败：{type(e).__name__}: {e}")

    # ==========================
    # 阶段 C: DeepSeek 打分 + 双轨制
    # ==========================

    # C1: DeepSeek 细筛
    scored_papers = score_all_papers(deepseek_input, phase_label="细筛")
    if not scored_papers:
        print("\n[结果] DeepSeek 打分全部失败，任务结束")
        return

    # C2: 双轨制筛选
    pass_list, browsing_list = dual_track_filter(scored_papers)

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

    if browsing_list:
        print(f"\n{'=' * 60}")
        print("【备选泛读列表】（仅终端 + .ris，不发送邮件）")
        print("=" * 60)
        for idx, p in enumerate(browsing_list, 1):
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
    pdf_saved, filed_keys = download_all_oa_pdfs(pass_list + browsing_list)

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
        dl_csv, dl_n = _lm2.build_download_list(pass_list + browsing_list,
                                                filed_keys)
        if dl_csv:
            # ★ 登记为 listed，避免同一篇明天又被列一次
            try:
                import processed as _proc
                for _p in (pass_list + browsing_list):
                    if _proc.key_of(_p) not in filed_keys:
                        _proc.mark(_p, "listed")
                _proc._save()
            except Exception:
                pass
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
        digest_path = _lm.build_digest(pass_list, browsing_list,
                                       len(scored_papers),
                                       time.time() - start_time)
        print(f"\n📋 文献简报：{digest_path}")
    except Exception as e:
        print(f"\n[警告] 简报生成失败：{type(e).__name__}: {e}")

    # ==========================
    # 阶段 E: 空转保护 + 邮件
    # ==========================

    if not pass_list:
        print("\n" + "=" * 60)
        print("【结果】今日无通关文献")
        if browsing_list:
            print(f"📖 有 {len(browsing_list)} 篇备选泛读已存 EndNote_Watch")
        print("📭 未发送邮件。")
        print("=" * 60)
    else:
        print(f"\n[进入] 准备推送 {len(pass_list)} 篇通关文献...")
        html_content = build_html_email_v3(pass_list)
        success = send_email(html_content, attachments=ris_files)
        if success:
            links = [make_link_key(p) for p in pass_list]
            save_history(links)
            print("✅ 历史记录已更新")

    # ==========================
    # 结束
    # ==========================

    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"🏁 V3.0 任务完成！总耗时: {elapsed:.1f} 秒")
    print(f"📊 RSS {rss_count} + OpenAlex {oa_count} → 粗筛 {len(coarse_papers)} "
          f"→ DeepSeek {len(scored_papers)} → 通关 {len(pass_list)} → 备选 {len(browsing_list)}")
    print(f"📬 邮箱: {SMTP_RECEIVER}  |  📁 .ris: {ENDNOTE_WATCH_DIR}")
    print(f"📥 OA PDF: {PDF_INBOX_DIR}（已存 {len(pdf_saved)} 篇）")
    if pdf_saved:
        print("   ↳ 若 EndNote 已配置「PDF 自动导入文件夹」指向该目录，将自动入库")
    if digest_path:
        print(f"📋 文献简报: {digest_path}")
    if dl_csv:
        print(f"📥 待下载清单: {dl_csv}（{dl_n} 篇）")
    print(f"🧠 打分器: {SCORER}（local = 本地 Qwen3.5-9B，免费）")
    print(f"{'=' * 60}")



if __name__ == "__main__":
    main()
