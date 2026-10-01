#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scoring.py —— 文献打分：默认本地 Qwen，失败回退 DeepSeek

从 paper_radar.py 拆出（2026-10-01，逐字搬运，未改逻辑）。
本地打分本体在 local_scorer.py。
"""
import json
import time

from config import *


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
        '"reason": "<20字以内的中文推荐理由>", '
        '"tldr": "<一句话中文总结该文创新点>"}'
    )
    user_prompt = f"题目：{title}\n\n摘要：{abstract[:2000]}"
    return system_prompt, user_prompt


def score_paper_with_deepseek(title, abstract):
    system_prompt, user_prompt = build_deepseek_prompt(title, abstract)
    try:
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}],
            temperature=0.3,
            max_tokens=300,
        )
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
        result["total_score"] = max(0, min(40, int(result["total_score"])))
        ts = result["total_score"]
        result["recommendation"] = "strong" if ts >= TOTAL_SCORE_PASS else ("normal" if ts >= BROWSING_THRESHOLD else "weak")
        result["score"] = round(result["total_score"] / 40 * 100)
        result["tldr"] = result.get("tldr", "")
        return result
    except json.JSONDecodeError as e:
        print(f"  [Error] JSON 解析失败: {e}")
        if 'content' in locals():
            print(f"  [Debug] 原始返回: {content[:200]}")
    except Exception as e:
        print(f"  [Error] API 调用失败: {e}")
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
        try:
            import sys as _sys
            if BASE_DIR not in _sys.path:
                _sys.path.insert(0, BASE_DIR)
            import local_scorer
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
# 7. 模块五：双轨制筛选 + .ris + 邮件（V2.0 复用）
# ══════════════════════════════════════════════
