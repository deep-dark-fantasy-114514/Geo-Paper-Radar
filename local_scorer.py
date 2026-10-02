#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local_scorer.py —— 用【本地 Qwen3.5-9B】给文献摘要打分，替代 DeepSeek

为什么
------
原流程 MAX_DEEPSEEK_INPUT=40 —— 每天只有 40 篇能进打分，其余全丢，
这就是"漏掉优质文献"的直接原因（DeepSeek 按量收费，不敢放开）。
本地模型 0.7 s/篇、成本为 0 ⇒ 规模可以放开到几百上千篇。

设计
----
* 输出字段与 paper_radar.score_paper_with_deepseek **完全一致**，
  所以可以在 paper_radar.py 里原样替换，也能随时切回 DeepSeek。
* total_score 由本模块【自己相加】，不信模型给的合计值（省得它算错）。
* 服务器【整批只起停一次】——逐个起停要 30 s/篇，那才是真慢。
* 任何异常都不向外抛：打分失败就跳过该篇，绝不让一次网络抖动毁掉整批。

实测（2026-10-01，8 篇真实 OpenAlex 摘要）：8/8 成功，0.7–0.8 s/篇，
分数有区分度（17–34），中文理由可用。

独立自测：
    python local_scorer.py --test
"""
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import *          # noqa: F401,F403  （RESEARCH_PROFILE 等）

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ★ 2026-10-01：删掉本文件里的硬编码路径。上一轮把它挪进了 config.ASK_IMAGE
#   （可 PAPER_RADAR_ASK_IMAGE 覆盖），却漏了这一处 —— 它在这里 **遮蔽** 了
#   config 的值，等于白改。
#   （教训：改路径这类事要 grep 全仓；同一常量有两处定义就一定会漂。）

# 与 paper_radar.py 保持一致的四维度定义
# ★ 2026-10-01 熔断阈值（见 score_all 里的说明）
FUSE_CONSEC_FAIL = 6      # 连续这么多篇失败 ⇒ 判定服务已死
FUSE_MIN_SAMPLE = 20      # 至少跑这么多篇才看失败率（防前几篇抖动误判）
FUSE_FAIL_RATE = 0.5      # 失败率超过这个 ⇒ 判定服务异常

# 与 paper_radar.py 保持一致的四维度定义
DIMS = [
    ("slope_stability",      "斜坡稳定性：边坡失稳机理、稳定性分析方法、加固技术"),
    ("rainfall_infiltration", "降雨入渗：雨水入渗过程、渗流场分析、入渗模型"),
    ("preferential_flow",    "优先流：大孔隙流、根土间隙流、裂隙流等非结构达西流"),
    ("method_innovation",    "方法创新：方法/模型/实验设计的新颖性与突破性"),
]

SYSTEM = (
    "你是一位资深地学审稿专家，专攻地质灾害与水文地质。"
    "你的任务是对文献做初筛打分，判断它对下面这位研究者的**研究主线**"
    "有多大帮助。评分要果断、有区分度，不要都给中间分。\n\n"
    + RESEARCH_PROFILE +
    # ★ 2026-10-01：黑名单改为从 research_profile.BLACKLIST_TOPICS 动态插值。
    #   原来在这里手抄了 3 个词（地震滑坡/滑坡动力学/古滑坡），而真正的名单有
    #   32 个 —— 你在 research_profile.py 里加一条，这里的提示词完全感知不到，
    #   打分规则和前置过滤规则就分家了。
    "\n■ 遇到下列主题【直接给 0 分】（它们与本研究完全无关）：\n"
    "   " + "、".join(BLACKLIST_TOPICS) + "\n"
)

# 定性字段的采集说明（不计分，只进简报供筛选）
_QUAL_SPEC = (
    '\n另外请【不计分地】判断三个属性，一并写进同一个 JSON：\n'
    '- "dual_role": 优先流在这篇里是加剧还是减轻边坡不稳定。'
    '取值 adverse(讲不利作用·本人重点) / beneficial(讲排水有利·对照文献) / '
    'both(两面都讲) / none(不涉及优先流的作用)\n'
    '- "scale": 研究尺度。pore / slope / catchment / regional / na\n'
    '- "approach": 主要方法。numerical / experimental / theoretical / review / '
    'data-driven / na\n'
)


def build_prompt(title, abstract):
    dims_txt = "\n".join(
        f"{i}. {k}（{d}）0-10 分" for i, (k, d) in enumerate(DIMS, 1))
    no_ab = no_abstract(abstract)      # ★ 统一判空（带 strip，见 config.no_abstract）
    if no_ab:
        # ★ 2026-10-01（用户定）：**只有标题的论文【不打分】**。
        #   标题的信息量不足以支撑"四维 0-10 分"这种细粒度评分，硬打出来的分数不可信。
        #   改为只做一个【相关 / 不相关】的二分类判断 —— 够用，且不误导后续筛选。
        return (
            f"{SYSTEM}\n\n"
            f"下面这篇论文**只有标题、没有摘要**（闭源期刊未公开摘要，属正常情况）。\n"
            f"⚠️ **不要给它打四维分数** —— 标题信息量不足以支撑细粒度评分。\n"
            f"只做一件事：**判断这个标题与上述研究主线是否相关**。\n\n"
            f"判定为【相关】的情形：\n"
            f"- 标题明确涉及 边坡稳定 / 滑坡 / 降雨入渗 / 优先流 / 非饱和土 / 渗流 / "
            f"根系与植被的水文力学作用 / 区域尺度滑坡易发性 / 库岸边坡\n"
            f"- 即使只是沾边（例如孔隙结构、土水特性），只要可能对本研究有参考价值就算相关\n\n"
            f"判定为【不相关】的情形：\n"
            f"- 标题与本方向看不出任何关系\n"
            f"- ⚠️ 特别注意**歧义词**：'slope' 可能是电化学的 Tafel slope，"
            f"'soil' 可能是土壤生态学/农学，'flow' 可能是流体力学/微流控，"
            f"'landslide' 可能是地质灾害科普或旅游——这些【不相关】\n"
            f"- 不确定时判**不相关**（宁可漏，不要用不确定的标题淹没你的清单）\n\n"
            f"【论文标题】{title}\n"
            f"【摘要】（无）\n\n"
            f"只输出 JSON，不要解释、不要 markdown 代码块：\n"
            '{"relevant":<true|false>,'
            '"reason":"<15字以内中文理由>",'
            '"dual_role":"<adverse|beneficial|both|none>",'
            '"scale":"<pore|slope|catchment|regional|na>",'
            '"approach":"<numerical|experimental|theoretical|review|data-driven|na>"}'
            + _QUAL_SPEC
            + "\n⚠️ 再强调一次：不要输出四个维度分数，也不要输出 total_score。"
        )
    return (
        f"{SYSTEM}\n\n"
        f"请按四个维度给下面这篇论文打分（每维 0-10 整数）：\n{dims_txt}\n\n"
        # ★ 2026-10-02：与 DeepSeek 用同一份分档锚点（research_profile.SCORE_RUBRIC）。
        #   原来两条打分器各说各话、都只写"是否涉及……"，没有 7 分和 8 分的界线
        #   ⇒ 分数不可比，也就没法说"本地挂掉回退 DeepSeek"在业务上等价。
        f"{SCORE_RUBRIC}\n"
        f"打分时注意：\n"
        f"- 泛泛相关（只在引言里提一句）给 2-4 分；真正以该主题为研究对象给 7-10 分\n"
        f"- 方法创新看的是【有没有新模型/新数据/新实验手段】，纯应用案例给低分\n"
        f"- 与本方向完全无关的（如纯遥感反演、纯岩石力学、纯管网水力学）都给 0-2 分\n\n"
        f"【论文标题】{title}\n"
        f"【摘要】{abstract[:2000]}\n\n"
        # ★★ 2026-10-02【实测根因】★★
        #   首次完整运行 74/895 篇失败，逐篇记录后发现 **100% 是
        #   "缺字段 method_innovation"**，且只在有摘要路径出现。
        #   抓模型原始输出才看清：**判定"完全无关"（前三维全 0）时，
        #   它会跳过第 4 个维度直接去写 reason**：
        #       {"slope_stability":0,"rainfall_infiltration":0,
        #        "preferential_flow":0,"reason":"完全无关…",...}
        #   而三次重试输出一模一样 ⇒ 确定性失败 ⇒ 次日重试仍然失败 ⇒
        #   **失败文献天天累积、每天的打分量越来越大**。
        #   ⇒ 一句显式要求把它堵在源头。
        f"⚠️ 无论论文相关与否，**都必须输出完整的四个维度**，一个都不能省略、不能跳过；\n"
        f"   完全不相关就把四个都写成 0（0,0,0,0），**不要只写前三个**。\n\n"
        f"只输出 JSON，不要解释、不要 markdown 代码块：\n"
        '{"slope_stability":<int>,"rainfall_infiltration":<int>,'
        '"preferential_flow":<int>,"method_innovation":<int>,'
        '"reason":"<20字以内中文推荐理由>","tldr":"<一句话中文总结其创新点>",'
        '"dual_role":"<adverse|beneficial|both|none>",'
        '"scale":"<pore|slope|catchment|regional|na>",'
        '"approach":"<numerical|experimental|theoretical|review|data-driven|na>"}'
        + _QUAL_SPEC
        + "\n不要输出 total_score，我会自己加。"
    )


_JSON_DEC = json.JSONDecoder()


def parse_json(text):
    """模型偶尔会包代码块或加废话，容错提取第一个**完整合法**的 JSON 对象。

    ★ 2026-10-01：原来是 `t[find("{") : rfind("}")+1]` —— 它假设文本里
      只有一对外层大括号。一旦模型在正式输出前先来一句带花括号的说明
      （例如 `示例格式：{"a":1}` 或 `{...}` 占位），find 会抓到前一段的左括号、
      rfind 抓到后一段的右括号，切出来的东西横跨两个 JSON 块 ⇒ json.loads 抛错
      ⇒ 这篇被判"打分失败"。
      ⇒ 改用 `JSONDecoder.raw_decode`：从每个 `{` 处试解析，
        它内部会正确地配对括号并跳过字符串里的花括号，第一个成功的就用。
        （括号匹配、转义、嵌套全部交给标准库，比手写栈更稳。）
    """
    t = (text or "").replace("```json", " ").replace("```", " ").strip()
    for i, ch in enumerate(t):
        if ch != "{":
            continue
        try:
            obj, _end = _JSON_DEC.raw_decode(t, i)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _load_ai():
    """加载本地视觉桥，复用它的服务器起停与显存冲突检测。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ask_image", ASK_IMAGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _call_text(ai, prompt, max_tokens=300, timeout=180):
    """纯文本调用（不带图），直接打本地 llama-server 的 OpenAI 兼容接口。"""
    import urllib.request
    payload = {
        "model": ai.MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},   # ★ 关闭思考模式
    }
    req = urllib.request.Request(
        ai.SERVER.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


def normalize(raw):
    """把模型返回规整成与 DeepSeek 版一致的字段（含自算 total_score）。"""
    if not isinstance(raw, dict):
        return None
    out = {}
    # ★ 2026-10-02 兜底：模型在"判定完全无关"时会漏掉最后一个维度（见 build_prompt
    #   里的根因说明）。若【已有的维度全为 0】而只缺个别维度，按 0 补 ——
    #   模型写出三个 0 就已经表明它的意图了。
    #   ⚠️ 只在这个条件下补：若已有维度里有非 0 值，仍按失败处理（宁可交上去补打，
    #      也不要凭空编一个分数）。
    _present, _missing = {}, []
    for k, _ in DIMS:
        v = raw.get(k)
        if v is None:
            _missing.append(k)
            continue
        try:
            _present[k] = max(0, min(10, int(float(v))))
        except (TypeError, ValueError):
            return None
    if _missing:
        if _present and all(x == 0 for x in _present.values()):
            for k in _missing:
                _present[k] = 0
        else:
            return None
    out = {k: _present[k] for k, _ in DIMS}
    out["total_score"] = sum(out[k] for k, _ in DIMS)   # ★ 自己加，不信模型
    out["reason"] = str(raw.get("reason", ""))[:60]
    out["tldr"] = str(raw.get("tldr", ""))[:200]

    # ★ 定性字段：**不计分**，只进简报供筛选。取值不在允许集合里就退回 na/none，
    #   避免模型自由发挥污染筛选。
    for f, spec in QUALITATIVE_FIELDS.items():
        v = str(raw.get(f, "")).strip().lower()
        # ★ 2026-10-01：默认值改为读字段自己的 spec["default"]。
        #   原来硬编码 `"none" if f == "dual_role" else "na"` —— 将来往
        #   QUALITATIVE_FIELDS 里加字段（比如 study_type）就会被迫吃 "na"，
        #   哪怕它在 spec 里声明了别的默认值。现在默认值属于数据，不属于代码。
        out[f] = v if v in spec["values"] else spec.get("default", "na")
    return out


def normalize_titleonly(raw):
    """★ 只有标题的论文：**只取「相关/不相关」，不打分**（用户 2026-10-01 定）。

    标题信息量不足以支撑四维 0-10 的细粒度评分，硬打分只会误导后续筛选。
    返回的记录里【没有 total_score】，但有 _title_only=True —— 下游据此分流。
    """
    if not isinstance(raw, dict) or "relevant" not in raw:
        return None
    v = raw.get("relevant")
    if isinstance(v, str):
        rel = v.strip().lower() in ("true", "yes", "y", "是", "1")
    else:
        rel = bool(v)
    out = {"relevant": rel, "_title_only": True,
           "reason": str(raw.get("reason", ""))[:60], "tldr": ""}
    for f, spec in QUALITATIVE_FIELDS.items():
        vv = str(raw.get(f, "")).strip().lower()
        out[f] = vv if vv in spec["values"] else spec.get("default", "na")
    return out


def score_all(papers, verbose=True, log_every=25, max_retries=2, quiet_below=0):
    """对 papers 批量打分（原地更新每篇的字段）。

    返回：
        成功则返回 papers（已 update 过），失败返回 None（调用方可回退 DeepSeek）
    """
    if not papers:
        return []
    try:
        ai = _load_ai()
    except Exception as e:
        print(f"  [Error] 无法加载本地视觉桥：{e}")
        return None
    # ★ 2026-10-01【服务生命周期】：只有"本函数亲手启动的"服务才由本函数收掉。
    #   原来 finally 里无条件 ai.stop_server() —— 如果这个 llama-server 是你
    #   自己先起来干别的用的（ask_image.health() 一进来就是 True，压根不会走到
    #   start_server），打分跑完照样被它杀掉。manual_ingest.py 一直是带
    #   `started` 标志的，这里对齐。
    #   ⚠️ start_server() 在**超时时会返回 False 却不杀进程**（见 download.py 的
    #      同款注释）⇒ 只要"尝试过启动"，退出时就必须收，否则留下一个占着
    #      几 GB 显存的孤儿。所以标志在调用前就置位。
    _we_started = False
    if not ai.health():
        if ai.mineru_running():
            print("  [Error] MinerU 正在运行，与本地模型抢显存；本轮改用 DeepSeek")
            return None
        print("  正在启动本地 Qwen3.5-9B …")
        _we_started = True
        if not ai.start_server():
            print("  [Error] 本地模型启动失败；本轮改用 DeepSeek")
            # ★ 这条 return 在下面的 try 之外 ⇒ finally 覆盖不到它。
            #   而 start_server() 在**超时时返回 False 却不杀进程** ⇒
            #   这里必须自己收一次，否则留下一个占几 GB 显存的孤儿，
            #   把后面所有需要显存的任务（含 MinerU）全部拖垮。
            try:
                ai.stop_server()
            except Exception:
                pass
            return None
    else:
        print("  [本地打分] 复用已在本机运行的服务（跑完不会关它）")

    total = len(papers)
    t0 = time.time()
    ok = fail = 0
    complete = []                 # ★ 真正跑完推理的那些（含判为不相关的只标题篇）
    fail_why = []                 # ★ 2026-10-02：逐篇记录失败原因（诊断用）
    consec_fail = 0
    fused = None
    try:
        n_title_only = 0
        for idx, p in enumerate(papers, 1):
            title = p.get("title", "")
            abstract = p.get("summary", "") or ""
            # ★ 只有标题的走【相关性判断】，不打分（用户 2026-10-01 定）
            title_only = no_abstract(abstract)
            if title_only:
                n_title_only += 1
            res = None
            why = ""
            for attempt in range(max_retries):
                try:
                    raw = _call_text(ai, build_prompt(title, abstract))
                    _pj = parse_json(raw)
                    if _pj is None:
                        # ★ 2026-10-02：区分"没吐出 JSON"和"JSON 结构不对"——
                        #   原来只有一句 `except Exception:` + 失败计数，
                        #   出了 74 篇失败根本不知道为什么。
                        why = f"不是合法 JSON（返回 {len(raw or '')} 字符）"
                        time.sleep(0.4)
                        continue
                    res = (normalize_titleonly(_pj) if title_only else normalize(_pj))
                    if res:
                        break
                    _need = ("relevant",) if title_only else tuple(k for k, _ in DIMS)
                    _miss = [k for k in _need if k not in _pj]
                    why = ("缺字段 " + ",".join(_miss)) if _miss else "字段值不合法"
                except Exception as e:
                    why = f"{type(e).__name__}: {str(e)[:70]}"
                    time.sleep(0.4)
            if res:
                p.update(res)
                complete.append(p)
                ok += 1
                consec_fail = 0
            else:
                fail += 1
                consec_fail += 1
                fail_why.append({
                    "title": title[:70], "title_only": title_only,
                    "why": why or "未知",
                    "abstract_len": len(abstract or ""),
                })

            # ★ 2026-10-01【熔断】。服务中途 OOM/崩溃时，后面每一篇都会
            #   "异常 → sleep 0.4 → 重试 → 再异常"，一路算失败。
            #   原来这种情况下函数照样返回一个【非空的】前半段结果，
            #   而 scoring.py 的判据是 `if scored:` ⇒ 上游认定"本地打分成功"，
            #   后面几百篇连 DeepSeek 兜底都轮不到，**静默消失**。
            #   现在：连续失败或失败率超线 ⇒ 返回 None，让上游走 DeepSeek。
            #   （没跑完的那些【不会】被标 seen，次日会重新入池。）
            if consec_fail >= FUSE_CONSEC_FAIL:
                fused = f"连续 {consec_fail} 篇失败（第 {idx} 篇）"
            elif idx >= FUSE_MIN_SAMPLE and fail / idx > FUSE_FAIL_RATE:
                fused = f"失败率 {fail}/{idx} = {fail/idx:.0%} 超过 {FUSE_FAIL_RATE:.0%}"
            if fused:
                print(f"\n  [熔断] {fused} —— 判定本地服务异常中断")
                print(f"         已成功 {ok} 篇（本批放弃，交给上游回退），"
                      f"其余 {total - idx} 篇未处理、次日会重新入池")
                return None

            if verbose and (idx % log_every == 0 or idx == total):
                el = time.time() - t0
                eta = el / idx * (total - idx)
                print(f"  [本地打分] {idx}/{total}  成功 {ok} 失败 {fail}  "
                      f"（其中只标题 {n_title_only} 篇，不打分）  "
                      f"已用 {el:.0f}s  预计剩余 {eta:.0f}s")
    finally:
        if _we_started:
            ai.stop_server()

    el = time.time() - t0
    n_scored = sum(1 for p in complete if "total_score" in p)
    n_rel = sum(1 for p in complete if p.get("_title_only") and p.get("relevant"))
    n_irrel = sum(1 for p in complete if p.get("_title_only")
                  and not p.get("relevant"))
    print(f"  [汇总] 本地打分完成：成功 {ok}/{total} 篇，耗时 {el:.0f}s "
          f"（{el/max(total,1):.2f} s/篇）")
    print(f"        其中【有摘要·打过四维分】{n_scored} 篇；"
          f"【只标题·不打分】{n_title_only} 篇（相关 {n_rel}，不相关 {n_irrel}）")
    if fail_why:
        from collections import Counter as _C
        _c = _C(w["why"] for w in fail_why)
        print(f"  [失败原因] {len(fail_why)} 篇未能打分，原因分布：")
        for _r, _n in _c.most_common(6):
            print(f"       {_n:4d}  篇  {_r}")
        _to = sum(1 for w in fail_why if w["title_only"])
        _ab = sum(1 for w in fail_why if w["abstract_len"] > 2000)
        print(f"       （其中只标题 {_to} 篇；摘要超过 2000 字符被截的 {_ab} 篇）")
        for _w in fail_why[:3]:
            print(f"       例：[{'只标题' if _w['title_only'] else '有摘要'}] "
                  f"{_w['title'][:52]} → {_w['why']}")

    # ★ 2026-10-01【返回值必须包含判为"不相关"的只标题文献】。
    #   原来这里只留 `有分 or (只标题 and relevant)` ⇒ relevant=False 的
    #   被直接从列表里剔除 ⇒ paper_radar 的 `mark_many(scored_papers,"seen")`
    #   永远登不到它们 ⇒ 次日 filter_new 认不出 ⇒ **再走一遍粗筛 + 再喂一次
    #   本地模型**，天天如此。而 COARSE_NO_ABSTRACT_BYPASS=True 正好保证
    #   它们每天都能过粗筛，这个循环是稳定的。实测这类约几百篇/天。
    #   现在返回【所有真正跑完推理的】；下游 titleonly_list 自己会用
    #   `and p.get("relevant")` 把它们挡在简报之外，不会漏给用户。
    #   （跑失败的那些不在 complete 里 —— 它们需要次日重试，不能登记。）
    return complete


# --------------------------------------------------------------- 自测
# ★ 2026-10-01【改为离线静态样本】。原来这个自测去敲
#   `api.openalex.org/works?search=...&mailto=test@example.com` ——
#   而 OpenAlex 自 2026-02-13 起**强制要求 API Key**，匿名 mailto 礼貌池已废弃，
#   不带头就全局 429。结果这个"独立自测"要么拿到空 results（等于什么都没测），
#   要么在非 JSON 的错误页上 `.json()` 直接抛异常崩掉。
#   自测的意义就是【不依赖外部服务】，所以样本直接内置。
_TEST_SAMPLES = [
    ("Preferential flow in macropore-dominated forest soils",
     "Macropore flow and preferential flow paths were studied in forest soils. "
     "Dye tracing showed that root channels and earthworm burrows dominate "
     "infiltration, bypassing the soil matrix. A dual-permeability model was "
     "fitted to the breakthrough curves.", "有摘要·优先流"),
    ("Numerical investigation of rainfall infiltration in unsaturated slopes",
     "A coupled rainfall infiltration and slope stability analysis is presented. "
     "The Richards equation is solved for variably saturated flow, and the "
     "factor of safety is computed with a limit equilibrium method. Results "
     "show that the wetting front depth controls the timing of failure.",
     "有摘要·降雨入渗"),
    ("Effects of root reinforcement on shallow landslide susceptibility",
     "Root tensile strength and root area ratio were measured for three species. "
     "A root cohesion model was coupled to an infinite slope stability model "
     "to assess regional landslide susceptibility.", "有摘要·边坡稳定"),
    ("A new machine-learning surrogate for the Richards equation",
     "We propose a physics-informed neural network surrogate for solving the "
     "Richards equation in heterogeneous media, achieving a 100x speedup over "
     "the finite element reference.", "有摘要·方法创新"),
    ("Deep-sea hydrothermal vent geochemistry",
     "Trace metal partitioning in hydrothermal plumes is examined using "
     "samples from the Mid-Atlantic Ridge.", "有摘要·不相关（应给低分）"),
    ("A numerical study of Tafel slope in electrocatalysis",
     "The Tafel slope of the oxygen evolution reaction was measured on "
     "nickel-iron oxide electrodes.", "有摘要·歧义 'slope'（应给低分）"),
    ("Landslide hazard mapping in the Three Gorges Reservoir area", "", "只标题·相关"),
    ("Seismic landslide dynamics and run-out distance of rock avalanches", "",
     "只标题·黑名单（应判不相关）"),
    ("Long-term survival of lichen communities on urban walls", "",
     "只标题·不相关"),
]


def _test():
    papers = [{"title": t, "summary": (s or "No abstract available"),
               "source": "self-test", "data_source": "self-test"}
              for t, s, _lab in _TEST_SAMPLES]
    print(f"自测（离线样本）：{len(papers)} 篇")
    for _t, _s, lab in _TEST_SAMPLES:
        print("   · %s" % lab)
    print()
    out = score_all(papers, log_every=25)
    if out is None:
        print("失败：本地打分不可用（服务起不来 / 中途熔断）")
        return 1
    n = 0
    for p in out:
        if p.get("_title_only"):
            print(f"   [只标题] {'相关' if p.get('relevant') else '不相关'}  "
                  f"{p['title'][:50]}")
            print(f"            {p.get('reason','')}")
        else:
            n += 1
            print(f"   {p['total_score']:2d}/40  {p['title'][:50]}")
            print(f"            {p.get('reason','')}")
    print(f"\n共 {len(out)} 篇返回（其中 {n} 篇有四维分）")
    return 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(_test())
    print(__doc__)
