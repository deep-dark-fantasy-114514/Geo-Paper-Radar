#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scoring.py —— 文献打分：默认本地 Qwen，失败回退 DeepSeek

从 paper_radar.py 拆出（2026-10-01，逐字搬运，未改逻辑）。
本地打分本体在 local_scorer.py。
"""
import json
import time

from config import *
from research_profile import QUALITATIVE_FIELDS
import local_scorer          # 打分提示词/归一化都在这里，两边共用



def build_deepseek_prompt(title, abstract):
    system_prompt = (
        "你是一位资深地学审稿专家，专攻地质灾害与水文地质领域。\n\n"
        "请根据以下四个维度对文献进行独立评分（每维度 0-10 分，整数）：\n"
        "1. 斜坡稳定性（Slope Stability）：是否涉及边坡失稳机理、稳定性分析方法、加固技术等\n"
        "2. 降雨入渗（Rainfall Infiltration）：是否涉及雨水入渗过程、渗流场分析、入渗模型等\n"
        "3. 优先流（Preferential Flow）：是否涉及大孔隙流、根土间隙流、裂隙流等非达西流\n"
        "4. 方法创新（Method Innovation）：方法/模型/实验设计的新颖性和突破性\n\n"
        "评分完毕后，计算 total_score = 四维分数之和（0-40 分）。\n"
        "根据总分给出推荐等级：\n"
        "  - 若 total_score >= 30 → recommendation = \"strong\"（强烈推荐）\n"
        "  - 若 total_score >= 24 → recommendation = \"normal\"（值得关注）\n"
        "  - 否则               → recommendation = \"weak\"（参考阅读）\n\n"
        "请严格输出以下 JSON 格式，不要输出任何其他内容：\n"
        '{"slope_stability": <0-10整数>, "rainfall_infiltration": <0-10整数>, '
        '"preferential_flow": <0-10整数>, "method_innovation": <0-10整数>, '
        '"total_score": <0-40整数>, '
        # ★ 2026-10-01：补上三个【定性】字段。原来只在本地打分器的 prompt 里
        #   有 —— 一旦把 SCORER 切到 deepseek，简报的「作用/尺度/方法」三列
        #   会全部退化成 "—"（下游 library_manager.build_digest 读的就是这三个键），
        #   而且没有任何提示。这里与 local_scorer 用同一份 QUALITATIVE_FIELDS。
        '"dual_role":"<adverse|beneficial|both|none>",'
        '"scale":"<pore|slope|catchment|regional|na>",'
        '"approach":"<numerical|experimental|theoretical|review|data-driven|na>",'
        '"reason": "<20字以内的中文推荐理由>", '
        '"tldr": "<一句话中文总结该文创新点>"}'
    )
    user_prompt = f"题目：{title}\n\n摘要：{abstract[:2000]}"
    return system_prompt, user_prompt


_client = None


def _get_client():
    """★ 2026-10-01：客户端改为模块级单例。

    原来每次打分都 `OpenAI(...)` 一次 —— 底层无法复用 HTTP 连接池，
    每篇都要重做 TCP/TLS 握手，白白增加延迟。
    """
    global _client
    if _client is None:
        _client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    return _client


def score_paper_with_deepseek(title, abstract, retries=2):
    system_prompt, user_prompt = build_deepseek_prompt(title, abstract)
    try:
        client = _get_client()
        response = None
        for _attempt in range(retries + 1):
            try:
                response = client.chat.completions.create(
                    model=DEEPSEEK_MODEL,
                    messages=[{"role": "system", "content": system_prompt},
                              {"role": "user", "content": user_prompt}],
                    temperature=0.3,
                    # ★ max_tokens 由 300 提到 700：reason/tldr 写长一点时，
                    #   300 会把结尾的 } 截掉，json.loads 直接抛错丢文献。
                    max_tokens=700,
                    # ★ 强制 JSON 模式，比事后再找 { } 截取可靠得多
                    response_format={"type": "json_object"},
                )
                break
            except Exception as _e:
                if _attempt >= retries:
                    raise
                print(f"  [重试] 第 {_attempt+1} 次失败（{type(_e).__name__}），稍后再试")
                time.sleep(1.5 * (_attempt + 1))
        content = response.choices[0].message.content.strip()
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1:
            content = content[start:end + 1]
        result = json.loads(content)
        required = ["slope_stability", "rainfall_infiltration",
                     "preferential_flow", "method_innovation",
                     "total_score", "reason", "tldr"]
        for field in required:
            if field not in result:
                raise ValueError(f"缺少字段: {field}")
        for dim in ["slope_stability", "rainfall_infiltration", "preferential_flow", "method_innovation"]:
            result[dim] = max(0, min(10, int(result[dim])))
        # ★ 2026-10-01：不再采信模型给的 total_score —— 大模型做多步精确加法
        #   极易出错（四项 8+7+6+8 可能被写成 26 或 31），而双轨制筛选
        #   （总分≥30 / 单项≥9）就靠这个数裁决。改为代码自己求和。
        #   （local_scorer 一直是这么做的，这里是补齐一致性。）
        result["total_score"] = sum(result[d] for d in
            ["slope_stability", "rainfall_infiltration",
             "preferential_flow", "method_innovation"])
        ts = result["total_score"]
        result["recommendation"] = "strong" if ts >= TOTAL_SCORE_PASS else ("normal" if ts >= BROWSING_THRESHOLD else "weak")
        result["score"] = round(result["total_score"] / 40 * 100)
        result["tldr"] = result.get("tldr", "")
        # ★ 2026-10-01：定性字段归一化 —— 与 local_scorer.normalize 完全同规则。
        #   取值不在允许集合里就退回 none/na，避免模型自由发挥污染下游筛选。
        for _f, _spec in QUALITATIVE_FIELDS.items():
            _v = str(result.get(_f, "")).strip().lower()
            result[_f] = _v if _v in _spec["values"] else (
                "none" if _f == "dual_role" else "na")
        return result
    except json.JSONDecodeError as e:
        print(f"  [Error] JSON 解析失败: {e}")
        if 'content' in locals():
            print(f"  [Debug] 原始返回: {content[:200]}")
    except Exception as e:
        print(f"  [Error] API 调用失败: {e}")
    return None


def score_titleonly_with_deepseek(title, retries=2):
    """只有标题的论文：走【相关性二分类】，不套四维打分。

    ★ 复用 `local_scorer` 的提示词与归一化函数，保证两条打分器行为**完全一致**
      —— 否则换打分器后 `_title_only` 标签与简报分区就会失效。
    """
    prompt = local_scorer.build_prompt(title, "No abstract available")
    try:
        client = _get_client()
        resp = None
        for _attempt in range(retries + 1):
            try:
                resp = client.chat.completions.create(
                    model=DEEPSEEK_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1, max_tokens=300,
                    response_format={"type": "json_object"})
                break
            except Exception:
                if _attempt >= retries:
                    raise
                time.sleep(1.5 * (_attempt + 1))
        raw = resp.choices[0].message.content.strip()
        return local_scorer.normalize_titleonly(local_scorer.parse_json(raw))
    except Exception as e:
        print(f"  [Error] 相关性判断失败: {e}")
        return None


def score_all_papers(papers, phase_label="DeepSeek"):
    """对文献列表打分。

    ★ 2026-10-01：默认走【本地 Qwen3.5-9B】（免费、0.7~1.1 s/篇），
    规模从 40 放开到 MAX_CANDIDATES=500。本地不可用时自动回退 DeepSeek。
    """
    if SCORER == "local" and papers:
        print("\n" + "=" * 60)
        print(f"【第二阶段】本地 Qwen3.5-9B 多维度打分（免费，共 {len(papers)} 篇）")
        print("=" * 60)
        # ★ 2026-10-01：local_scorer 已在文件顶部静态导入（原来是运行时
        #   改 sys.path 再自导入的补丁式写法）。
        try:
            scored = local_scorer.score_all(papers)
        except Exception as e:
            print(f"  [Error] 本地打分模块异常：{e}")
            scored = None
        if scored:
            for p in scored:                       # 补齐与 DeepSeek 版一致的派生字段
                ts = p.get("total_score", 0)
                p["recommendation"] = ("strong" if ts >= TOTAL_SCORE_PASS
                                       else ("normal" if ts >= BROWSING_THRESHOLD
                                             else "weak"))
                p["score"] = round(ts / 40 * 100)
            return scored
        print("  [回退] 本地打分不可用，改用 DeepSeek")
        # ★★★ 2026-10-01【安全熔断】★★★
        #   main() 里的 _cap 是按【配置的 SCORER】算的：配成 local 时
        #   MAX_CANDIDATES=None ⇒ 走"不截断"分支。可本地一旦挂掉就回退到这里，
        #   收到的是【全部】几百上千篇 ⇒ MAX_DEEPSEEK_INPUT=40 的堤坝被完全绕过，
        #   会直接产生高额 API 账单并阻塞数小时。
        #   所以熔断必须做在这里 —— 判据是"实际要跑哪个打分器"，不是配置。
        if len(papers) > MAX_DEEPSEEK_INPUT:
            _n0 = len(papers)
            try:
                from filters import limit_for_deepseek
                papers = limit_for_deepseek(papers, MAX_DEEPSEEK_INPUT)
            except Exception:
                papers = papers[:MAX_DEEPSEEK_INPUT]
            print(f"  [安全熔断] 本地打分失效，回退 DeepSeek 前强制截断 "
                  f"{_n0} → {len(papers)} 篇，防止费用失控")

    # ─────────────── 以下为原 DeepSeek 路径（保留作后备） ───────────────
    print("\n" + "=" * 60)
    print(f"【第二阶段】DeepSeek AI 多维度打分 ({phase_label})")
    print("=" * 60)

    scored = []
    total = len(papers)
    for idx, paper in enumerate(papers, 1):
        title = paper["title"]
        abstract = paper["summary"]
        source = paper.get("source", "?")
        print(f"\n[进度] ({idx}/{total}) 正在打分 [{source}]: {title[:60]}...")

        # ★ 2026-10-01：只有标题的走【相关性二分类】，不硬套四维打分。
        #   原来 DeepSeek 路径对所有文献一律四维评分 —— 让模型凭十来个单词的
        #   英文标题去判"优先流机制/方法创新性"，必然幻觉；而且【不打 _title_only
        #   标签】，主程序预设的"只标题文献走独立简报与待下载清单"就彻底失效，
        #   导致换打分器后业务行为不一致。
        if no_abstract(abstract):      # ★ 统一判空（带 strip）
            result = score_titleonly_with_deepseek(title)
            if result is None:
                print(f"  [跳过] 该篇相关性判断失败，已跳过")
                continue
            paper.update(result)
            scored.append(paper)
            print(f"  [只标题] {'相关' if result.get('relevant') else '不相关'}"
                  f" | {result.get('reason', '')}")
            time.sleep(0.3)
            continue

        result = score_paper_with_deepseek(title, abstract)
        if result is None:
            print(f"  [跳过] 该篇打分失败，已跳过")
            continue

        paper.update(result)
        scored.append(paper)
        dims = (f"S:{result['slope_stability']} R:{result['rainfall_infiltration']} "
                f"P:{result['preferential_flow']} M:{result['method_innovation']}")
        print(f"  [得分] {result['total_score']}/40 {dims} | {result['reason']}")
        time.sleep(0.5)

    print(f"\n[汇总] 成功打分 {len(scored)}/{total} 篇")
    return scored


# ══════════════════════════════════════════════
# ══════════════════════════════════════════════
