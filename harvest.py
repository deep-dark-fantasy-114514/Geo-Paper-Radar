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
import time
from datetime import datetime, timedelta, timezone

import requests

from config import *
from sources import (fetch_papers_from_rss, OpenAlexFetcher, fetch_arxiv,
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
            all_papers.extend(fetch_crossref(
                CROSSREF_QUERIES, days=OPENALEX_DAYS_LOOKBACK, rows=CROSSREF_ROWS))
            all_papers.extend(fetch_crossref_journals(
                days=OPENALEX_DAYS_LOOKBACK))
        except Exception as e:
            print(f"  [Warning] Crossref 抓取失败：{e}")

    # ★ 2026-10-01（审查第八条）：arXiv 这一支原来【漏了】——
    #   本函数是从 paper_radar.py 逐字拷贝的，那边有 USE_ARXIV 分支、这边没有。
    #   今天 USE_ARXIV=False 所以零影响，但一旦打开，云端就会比本地少一个源。
    if USE_ARXIV:
        try:
            all_papers.extend(fetch_arxiv(ARXIV_QUERIES, ARXIV_PER_QUERY))
        except Exception as e:
            print(f"  [Warning] arXiv 抓取失败：{e}")

    # ★ 学位论文 + 中文核心刊（云端 harvest 也要，否则笔记本睡着的那些天收不到）
    if USE_DISSERTATIONS:
        try:
            all_papers.extend(fetch_openalex_dissertations(
                DISSERTATION_QUERIES, days=DISSERTATION_DAYS,
                per_query=DISSERTATIONS_PER_QUERY))
        except Exception as e:
            print(f"  [Warning] 学位论文抓取失败：{e}")
    if USE_CN_JOURNALS:
        try:
            all_papers.extend(fetch_openalex_journals(days=CN_JOURNAL_DAYS))
        except Exception as e:
            print(f"  [Warning] 中文核心刊抓取失败：{e}")

    try:
        before = len(all_papers)
        out = dedupe_by_title(all_papers)
        # ★ 2026-10-01：按来源统计，云端日志里也看得见各源贡献
        #   （原来 main() 只统计 RSS/OpenAlex，其余源在日志里隐身）
        from collections import Counter
        _c = Counter(x.get("data_source", "?") for x in out)
        print(f"  [跨源去重] {before} → {len(out)} 篇，按来源：")
        for _s2, _n2 in _c.most_common():
            print(f"     {_s2:16s} {_n2:5d} 篇")
        return out
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
    kept = local_regex_coarse_filter(cand)   # 走加权分规则
    kept, _bl = blacklist_filter(kept)      # ★ 拉黑主题先刷掉，省存储
    if _bl:
        print(f'  [拉黑] 刷掉 {len(_bl)} 篇')
    # ★ 2026-10-01：截断从 `kept[:HARVEST_MAX]` 改为【按来源保底 + 按序补足】。
    #   原来是纯按列表顺序切，而学术论文源（学位论文 / 中文核心刊）恒在列表末尾
    #   ⇒ 实测每天被【整源砍光】（48 篇学位论文 + 20 篇中文核心一篇都进不了存档）。
    picked = _stratified_pick(kept, HARVEST_MAX, HARVEST_MIN_PER_SOURCE)
    slim = []
    for p in picked:
        rec = {k: p.get(k) for k in HARVEST_FIELDS}
        rec["summary"] = (p.get("summary") or "")[:HARVEST_ABSTRACT_CHARS]
        slim.append(rec)
    os.makedirs(HARVEST_DIR, exist_ok=True)
    # ★ 用 UTC 命名：与 GitHub runner（UTC）保持一致，回捞端也按 UTC 拼日期。
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(HARVEST_DIR, f"{day}.json")
    _atomic_write_json(path, {"date": day, "count": len(slim), "papers": slim})
    print(f"\n[完成] 已写 {path}（{len(slim)} 篇，"
          f"{os.path.getsize(path)/1024:.0f} KB）")
    return 0


def _stratified_pick(papers, budget, floor_per_source):
    """按来源【保底 + 补足】地截断，避免某个源被整源砍掉。

    背景：原实现是 `papers[:HARVEST_MAX]` —— 纯按列表顺序切。而
    `fetch_all_candidates()` 的追加顺序是 RSS → OpenAlex → Crossref →
    arXiv → 学位论文 → 中文核心刊，后两者恒在末尾 ⇒ 只要候选总数超预算
    （实测 898 > 600），学位论文与中文核心刊【每天都在被整源丢弃】。

    规则：
      1) 每个来源先各取 floor_per_source 篇（不足则全取）
      2) 剩余额度按【原始顺序】补足 —— 顺序即优先级，行为可预期
    """
    if not budget or len(papers) <= budget:
        return list(papers)
    by_src = {}
    for i, p in enumerate(papers):
        by_src.setdefault(p.get("data_source", "?"), []).append(i)
    # 来源极多时按均分下调保底，防止保底本身就把预算撑爆
    if len(by_src) * floor_per_source > budget:
        floor_per_source = max(1, budget // len(by_src))

    keep = set()
    for ids in by_src.values():
        keep.update(ids[:floor_per_source])
    for i in range(len(papers)):
        if len(keep) >= budget:
            break
        keep.add(i)
    dropped = len(papers) - len(keep)
    if dropped:
        print(f"  [存档截断] {len(papers)} → {len(keep)} 篇"
              f"（每源保底 {floor_per_source} 篇，其余按序补足）")
    return [papers[i] for i in sorted(keep)][:budget]


def _atomic_write_json(path, obj):
    """原子写 JSON：先写 .tmp 再 os.replace。

    ★ 2026-10-01：原来直接用 open(path, 'w')。云端被 CI 强杀、或本地断在
    写盘瞬间，会留下【半截文件】，次日 json.load 直接抛异常，整天的候选全废。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_consumed():
    try:
        with open(HARVEST_CONSUMED, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_consumed(s):
    try:
        _atomic_write_json(HARVEST_CONSUMED, sorted(s))
    except Exception as e:
        # ★ 2026-10-01：原来 `except Exception: pass` —— 连"没存上"都看不见。
        #   写失败意味着次日会把同一批候选【重复回捞】一遍。
        print(f"  [云端候选] 警告：消费记录写入失败（{type(e).__name__}: {e}）")


def _utc_day(i):
    """回捞用的日期串。

    ★ 必须与【生产端】一致：GitHub runner 在 UTC 用 datetime.now() 命名文件
      （harvest.py 现在也显式用 timezone.utc）。本地若按东八区算，在北京
      00:00–08:00 这段会先撞上"云端今天的文件还没生成"。
    """
    return (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")


def _fetch_harvest_json(day, bases):
    """按镜像链取某天的云端候选。

    返回 (状态, 数据, 挂掉的镜像列表)：
        ("ok",      dict, [...])  取到
        ("missing", None, [...])  404 —— 那天云端没跑（正常，与镜像无关）
        ("neterr",  ex,   [...])  所有镜像都没成功

    挂掉的镜像由调用方从候选列表里剔除，后续日子不再白等它。
    """
    dead, last_err = [], None
    for base in bases:
        # ★ cache-buster：本地"当天不锁"（见 merge_harvest），同一小时内
        #   重跑会再取一次当天的文件；边缘缓存会把刚补跑完的旧版给你。
        url = base + day + ".json?t=" + str(int(time.time()))
        try:
            r = requests.get(url, timeout=HARVEST_HTTP_TIMEOUT)
        except requests.RequestException as e:
            dead.append(base)
            last_err = e
            host = base.split("/")[2] if "//" in base else base
            print(f"  [云端候选] 镜像不通 {host}：{type(e).__name__}: {e}")
            continue                       # 这个镜像不通，试下一个
        if r.status_code == 404:
            return "missing", None, dead   # 文件确实不存在，换镜像也一样
        if r.status_code != 200:
            last_err = RuntimeError(f"HTTP {r.status_code}")
            continue                       # 可能是镜像自身问题，换一个试
        try:
            return "ok", r.json(), dead
        except ValueError:
            last_err = ValueError("返回的不是合法 JSON（可能写盘被截断）")
            continue
    return "neterr", last_err, dead


def merge_harvest(cand):
    """把云端攒的候选（最近 N 天、且本地尚未消费过的）合并进本地候选池。

    ★ 刻意【不用 git pull】：本地在 v3.0-dev 分支且有未提交改动，
      pull 会冲突。仓库是公开的，直接走 raw HTTP 取，无副作用。
    """
    consumed = _load_consumed()
    before = len(cand)
    per_day, net_fail = [], 0
    bases = list(REPO_RAW_MIRRORS)

    for i in range(HARVEST_KEEP_DAYS):
        day = _utc_day(i)
        if day in consumed:
            continue
        status, payload, dead = _fetch_harvest_json(day, bases)

        # 把本轮挂掉的镜像摘掉 —— 否则后面每天都先白等它一次
        for b in dead:
            if b in bases:
                bases.remove(b)
                print(f"  [云端候选] 镜像不可达，本轮剔除：{b}")
        if not bases:
            print("  [云端候选] 所有镜像都不可达，停止回捞（不影响本地流程）")
            break

        if status == "neterr":
            # ★ 2026-10-01：原来"网络不通"和"那天没跑"混在同一个
            #   `except: continue` 里 ⇒ 通道不通时要【逐日死等 14 × timeout】。
            net_fail += 1
            print(f"  [云端候选] {day} 取回失败："
                  f"{type(payload).__name__}: {payload}")
            if net_fail >= HARVEST_NET_FAIL_TOLERANCE:
                print(f"  [云端候选] 连续 {net_fail} 天取不回，"
                      f"停止回捞（不影响本地流程）")
                break
            continue
        net_fail = 0

        if status == "missing":
            continue                      # 那天云端没跑，跳过即可

        n = 0
        for p in payload.get("papers", []):
            p["from_harvest"] = day
            cand.append(p)
            n += 1
        # ★ 2026-10-01：【当天】不写进 consumed。云端 workflow 带
        #   workflow_dispatch，你白天手动补跑一次；若当天已被标成"已消费"，
        #   那份更新就永远取不回来（14 天后滑出窗口）。
        if i > 0:
            consumed.add(day)
        per_day.append((day, n))

    # ★ 2026-10-01：去重改用 sources.dedupe_by_title —— 与主流程完全一致
    #   （优先规范化 DOI，其次 _norm_title_key 清洗过的标题）。
    #   原来这里是 `title.strip().lower()`，与主流程是两套标准。
    #   顺序上本地候选在前 ⇒ 同篇冲突时保留本地那份，符合预期。
    if len(cand) > before:
        cand = dedupe_by_title(cand)

    if per_day:
        _save_consumed(consumed)
        detail = ", ".join("%s(+%d)" % (d, n) for d, n in per_day)
        print(f"  [云端候选] 回捞 {detail}，跨源去重后净增 {len(cand) - before} 篇")
    else:
        print("  [云端候选] 无新的可回捞")
    return cand
