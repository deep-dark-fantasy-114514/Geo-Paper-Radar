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
        # ★ 2026-10-02：删掉原来那段硬编码的推荐等级说明。
        #   它写死 30 / 24，而程序实际用的是 TOTAL_SCORE_PASS /
        #   BROWSING_THRESHOLD —— 你一旦在 config 里改阈值，**模型脑子里
        #   那套标准还是旧的**，它写 reason/tldr 时的取舍就已经跑偏了
        #   （虽然最终 recommendation 字段由代码覆盖，但理由文字不会回滚）。
        #   而且模型本来也不需要输出 recommendation —— 那是确定性计算，
        #   交给代码就够（跟 total_score 一个道理）。
        "评分完毕后不要自己算总分，也不要输出推荐等级。\n\n"
        + SCORE_RUBRIC + "\n"
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
        # ★ 2026-10-02：补显式 timeout + 关掉 SDK 自带的隐式重试。
        #   原来两者都没设 —— 而这是个【无人值守】任务，某次连接卡住就会
        #   把整轮拖死（对比 sources.py 里每个 requests 都写了 timeout=45）。
        #   `max_retries=0` 是为了让重试策略完全由本文件控制（见 _is_retryable），
        #   否则 SDK 会在底下偷偷重试，次数和我们的算不到一起。
        _client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL,
                         timeout=DEEPSEEK_TIMEOUT, max_retries=0)
    return _client


DIMS_KEYS = ("slope_stability", "rainfall_infiltration",
             "preferential_flow", "method_innovation")

# 这些错误重试没意义（参数错 / 鉴权错 / 模型不存在 …），直接放弃
_FATAL_MARKERS = ("400", "401", "403", "404", "422",
                  "invalid_request", "authentication", "model_not_found",
                  "context_length")


def _is_retryable(exc):
    """区分「等会儿再来」和「再来也没用」。

    ★ 2026-10-02：原来 `except Exception` 一律重试 —— 连
      `400 Bad Request`（参数非法）、`401`（key 无效）都会白等 1.5s + 3s
      再打两次。而这两种错误重试一万次结果都一样。
    """
    s = f"{type(exc).__name__}: {exc}".lower()
    if any(m in s for m in _FATAL_MARKERS):
        return False
    return True


def _parse_deepseek_result(result):
    """严格校验模型返回的结构，不合法就抛 ValueError。

    ★ 2026-10-02（审查第 5/17 条）：原来只做"字段在不在 + int() 能不能转"。
      而 `int(float(v))` 会把 `8.9` 悄悄变成 `8`、`True` 变成 `1` ——
      **把模型的错误输出"修正"成了看起来合法的分数**。
      更糟的是定性字段取值越界时被静默填空值，整条结果照样算"成功"。
      ⇒ 现在：维度必须是 0-10 的**整数**（bool 不算），定性字段必须落在
        允许集合内（越界即判失败，不再用默认值掩盖），reason/tldr 强制截断
        到与 local_scorer 相同的长度（两边口径一致）。
    """
    for dim in DIMS_KEYS:
        v = result.get(dim)
        if isinstance(v, bool) or not isinstance(v, int):
            # 允许 "8" 这种纯数字串，但不接受 8.9 / True / None
            try:
                if isinstance(v, str) and v.strip().lstrip("-").isdigit():
                    v = int(v.strip())
                else:
                    raise ValueError(f"{dim} 不是整数：{v!r}")
            except (TypeError, ValueError):
                raise ValueError(f"{dim} 不是合法整数：{v!r}")
        if not 0 <= v <= 10:
            raise ValueError(f"{dim} 越界：{v}")
        result[dim] = v

    for _f, _spec in QUALITATIVE_FIELDS.items():
        _v = str(result.get(_f, "")).strip().lower()
        if _v not in _spec["values"]:
            # ★ 不再静默填默认值：那是"把模型答非所问伪装成成功"。
            raise ValueError(f"{_f} 取值非法：{result.get(_f)!r}")
        result[_f] = _v

    # ★ 与 local_scorer.normalize 相同的长度口径（原来 DeepSeek 这边不截断，
    #   两条打分器产出的 reason/tldr 长度行为不一致）
    result["reason"] = str(result.get("reason", ""))[:60]
    result["tldr"] = str(result.get("tldr", ""))[:200]
    # 总分仍由代码求和（不信模型的加法）
    result["total_score"] = sum(result[d] for d in DIMS_KEYS)
    ts = result["total_score"]
    result["recommendation"] = ("strong" if ts >= TOTAL_SCORE_PASS
                                else ("normal" if ts >= BROWSING_THRESHOLD
                                      else "weak"))
    result["score"] = round(ts / 40 * 100)
    return result


def score_paper_with_deepseek(title, abstract, retries=2):
    system_prompt, user_prompt = build_deepseek_prompt(title, abstract)
    try:
        client = _get_client()
        response = None
        _max_tok = 700
        for _attempt in range(retries + 1):
            try:
                response = client.chat.completions.create(
                    model=DEEPSEEK_MODEL,
                    messages=[{"role": "system", "content": system_prompt},
                              {"role": "user", "content": user_prompt}],
                    temperature=0.3,
                    # ★ max_tokens 由 300 提到 700：reason/tldr 写长一点时，
                    #   300 会把结尾的 } 截掉，json.loads 直接抛错丢文献。
                    max_tokens=_max_tok,
                    # ★ 强制 JSON 模式，比事后再找 { } 截取可靠得多
                    response_format={"type": "json_object"},
                )
                # ★★ 2026-10-02【必须看 finish_reason】★★
                #   JSON 模式只保证"语法是合法 JSON"，**不保证没被截断**。
                #   官方明确：finish_reason="length" 时输出可能被砍。
                #   原来直接取 content 去 json.loads，一旦截断就
                #   JSONDecodeError → return None → 这篇从当天消失。
                _fin = getattr(response.choices[0], "finish_reason", None)
                if _fin == "length":
                    if _attempt < retries:
                        _max_tok = 1200          # 加长再试一次
                        print(f"  [重试] 输出被截断（finish_reason=length），"
                              f"max_tokens → {_max_tok}")
                        continue
                    raise ValueError("输出持续被截断（finish_reason=length）")
                if _fin not in (None, "stop"):
                    raise ValueError(f"finish_reason={_fin}")
                break
            except Exception as _e:
                if not _is_retryable(_e):
                    print(f"  [放弃] 不可重试的错误（{type(_e).__name__}），重试无意义")
                    raise
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
        return _parse_deepseek_result(result)
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


# ★ 2026-10-01：记录【实际】跑了哪个打分器。原来日志是按配置变量 SCORER
#   打印的，而本地 Qwen 挂掉时会静默回退 DeepSeek —— 于是日志显示"免费"，
#   实际在按量计费。成本判断不能靠配置猜，要记事实。
LAST_RUN = {"scorer": None, "count": 0, "fallback": False}


def score_all_papers(papers, phase_label="DeepSeek"):
    """对文献列表打分。

    ★ 2026-10-01：默认走【本地 Qwen3.5-9B】（免费、0.7~1.1 s/篇），
    规模从 40 放开到 MAX_CANDIDATES=500。本地不可用时自动回退 DeepSeek。
    实际用了哪个记在 `LAST_RUN`（调用方可据此报成本）。
    """
    LAST_RUN.update({"scorer": None, "count": 0, "fallback": False})
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
            # ★★ 2026-10-02【部分失败不能整批算成功】★★
            #   原来只要 `scored` 非空就直接 return —— 于是"本地 492/500 成功、
            #   8 篇失败"会被判成"本地打分成功"，那 8 篇【连 DeepSeek 兜底都
            #   轮不到】，当天既不进简报也不进邮件。
            #   （它们没被标 seen，次日会重新入池 —— 所以不是永久消失，
            #     但"回退"的设计意图是"本地不行的交给 DeepSeek"，不是"丢掉"。）
            #   ⇒ 只把【本地没打上的那批】送 DeepSeek 补，而不是整批重来。
            _done = {id(p) for p in scored}
            _missed = [p for p in papers if id(p) not in _done]
            if _missed:
                print(f"\n  [补送] 本地有 {len(_missed)}/{len(papers)} 篇没打上，"
                      f"转 DeepSeek 补打（上限 {MAX_DEEPSEEK_INPUT}）")
                LAST_RUN["fallback"] = True
                _extra = _deepseek_batch(_missed[:MAX_DEEPSEEK_INPUT],
                                         phase_label + "·本地补漏")
                scored = scored + _extra
                if len(_missed) > MAX_DEEPSEEK_INPUT:
                    print(f"  [限额] 另有 {len(_missed) - MAX_DEEPSEEK_INPUT} 篇"
                          f"本轮未补，次日会重新入池")
            LAST_RUN.update({"scorer": "本地 Qwen3.5-9B",
                             "count": len(scored),
                             "fallback": bool(LAST_RUN.get("fallback"))})
            return scored
        print("  [回退] 本地打分不可用，改用 DeepSeek")
        LAST_RUN["fallback"] = True
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

    # ─────────────── DeepSeek 路径（后备 + 本地补漏）───────────────
    scored = _deepseek_batch(papers, phase_label)
    LAST_RUN.update({"scorer": "DeepSeek",
                     "count": len(scored),
                     "fallback": bool(LAST_RUN.get("fallback"))})
    return scored


def _deepseek_batch(papers, phase_label="DeepSeek"):
    """对 papers 逐篇打分（原地 update），返回成功的那批。

    ★ 2026-10-02：从 `score_all_papers` 里抽出来 —— 因为它现在有两个调用方：
        · 本地打分整体不可用 → 整批回退
        · 本地只失败了几篇   → **只补那几篇**（见上面的 [补送] 分支）
      原来这段是内联在函数体里的，没法只对子集调用。
    """
    if not papers:
        return []
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
