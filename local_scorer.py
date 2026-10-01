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

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ASK_IMAGE = r"C:\Users\zihao\.claude\skills\local-vision\scripts\ask_image.py"

# 与 paper_radar.py 保持一致的四维度定义
DIMS = [
    ("slope_stability",      "斜坡稳定性：边坡失稳机理、稳定性分析方法、加固技术"),
    ("rainfall_infiltration", "降雨入渗：雨水入渗过程、渗流场分析、入渗模型"),
    ("preferential_flow",    "优先流：大孔隙流、根土间隙流、裂隙流等非结构达西流"),
    ("method_innovation",    "方法创新：方法/模型/实验设计的新颖性与突破性"),
]

SYSTEM = ("你是一位资深地学审稿专家，专攻地质灾害与水文地质。"
          "你的任务是对文献做初筛打分，判断它与【碎石土边坡优先流入渗】"
          "这一研究方向的相关程度。评分要果断、有区分度，不要都给中间分。")


def build_prompt(title, abstract):
    dims_txt = "\n".join(
        f"{i}. {k}（{d}）0-10 分" for i, (k, d) in enumerate(DIMS, 1))
    return (
        f"{SYSTEM}\n\n"
        f"请按四个维度给下面这篇论文打分（每维 0-10 整数）：\n{dims_txt}\n\n"
        f"打分时注意：\n"
        f"- 泛泛相关（只在引言里提一句）给 2-4 分；真正以该主题为研究对象给 7-10 分\n"
        f"- 方法创新看的是【有没有新模型/新数据/新实验手段】，纯应用案例给低分\n"
        f"- 与本方向完全无关的（如纯遥感反演、纯岩石力学、纯管网水力学）都给 0-2 分\n\n"
        f"【论文标题】{title}\n"
        f"【摘要】{abstract[:2000]}\n\n"
        f"只输出 JSON，不要解释、不要 markdown 代码块：\n"
        '{"slope_stability":<int>,"rainfall_infiltration":<int>,'
        '"preferential_flow":<int>,"method_innovation":<int>,'
        '"reason":"<20字以内中文推荐理由>","tldr":"<一句话中文总结其创新点>"}'
        "\n不要输出 total_score，我会自己加。"
    )


def parse_json(text):
    """模型偶尔会包代码块或加废话，容错提取。"""
    t = (text or "").strip()
    t = t.replace("```json", " ").replace("```", " ").strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:
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
    for k, _ in DIMS:
        v = raw.get(k)
        if v is None:
            return None
        try:
            out[k] = max(0, min(10, int(float(v))))
        except (TypeError, ValueError):
            return None
    out["total_score"] = sum(out[k] for k, _ in DIMS)   # ★ 自己加，不信模型
    out["reason"] = str(raw.get("reason", ""))[:60]
    out["tldr"] = str(raw.get("tldr", ""))[:200]
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
    if not ai.health():
        if ai.mineru_running():
            print("  [Error] MinerU 正在运行，与本地模型抢显存；本轮改用 DeepSeek")
            return None
        print("  正在启动本地 Qwen3.5-9B …")
        if not ai.start_server():
            print("  [Error] 本地模型启动失败；本轮改用 DeepSeek")
            return None

    total = len(papers)
    t0 = time.time()
    ok = fail = 0
    try:
        for idx, p in enumerate(papers, 1):
            title = p.get("title", "")
            abstract = p.get("summary", "") or "No abstract available"
            res = None
            for attempt in range(max_retries):
                try:
                    raw = _call_text(ai, build_prompt(title, abstract))
                    res = normalize(parse_json(raw))
                    if res:
                        break
                except Exception:
                    time.sleep(0.4)
            if res:
                p.update(res)
                ok += 1
            else:
                fail += 1
            if verbose and (idx % log_every == 0 or idx == total):
                el = time.time() - t0
                eta = el / idx * (total - idx)
                print(f"  [本地打分] {idx}/{total}  成功 {ok} 失败 {fail}  "
                      f"已用 {el:.0f}s  预计剩余 {eta:.0f}s")
    finally:
        ai.stop_server()

    el = time.time() - t0
    print(f"  [汇总] 本地打分完成：成功 {ok}/{total} 篇，耗时 {el:.0f}s "
          f"（{el/max(total,1):.2f} s/篇）")
    return [p for p in papers if "total_score" in p]


# --------------------------------------------------------------- 自测
def _test():
    import requests
    q = "preferential flow macropore slope rainfall infiltration"
    url = ("https://api.openalex.org/works?search=" + requests.utils.quote(q) +
           "&per_page=6&mailto=test@example.com")
    works = requests.get(url.replace("per_page", "per-page"), timeout=30).json().get("results", [])
    papers = []
    for w in works:
        inv = w.get("abstract_inverted_index")
        if not inv:
            continue
        pos = {}
        for word, idxs in inv.items():
            for i in idxs:
                pos[i] = word
        papers.append({"title": w.get("title") or "",
                       "summary": " ".join(pos[k] for k in sorted(pos))[:1500],
                       "source": "test"})
    print(f"自测：{len(papers)} 篇")
    out = score_all(papers, log_every=1)
    if out is None:
        print("失败"); return 1
    for p in out:
        print(f"  {p['total_score']:2d}/40  {p['title'][:52]}")
        print(f"        {p['reason']}")
    return 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(_test())
    print(__doc__)
