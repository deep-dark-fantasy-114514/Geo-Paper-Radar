#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
processed.py —— 全局去重登记表（2026-10-01 新增）

解决两个真实存在的重复问题
--------------------------
1. **跨天重复**：OpenAlex 回看 7 天（`OPENALEX_DAYS_LOOKBACK=7`）⇒ 同一篇论文
   会连续 7 天出现在候选池。而下载文件名带日期 ⇒ 每天都是新名字 ⇒
   `Library\优先流\xxx.pdf`、`xxx (1).pdf`、`xxx (2).pdf`…… 一路堆。
2. **跨源重复**：云端 harvest 回捞的 + 本地 OpenAlex + Crossref（待接入）
   + RSS，同一篇可能从四个地方进来。

做法
----
一张表记下"见过的文献"，键与 `library_manager.key_of()` 完全一致：
    · 优先 DOI（规范化：去掉 https://doi.org/ 前缀、转小写）
    · 没有 DOI 就退回规范化标题（去标点、压空白、转小写）

三种状态：
    seen   —— 过了粗筛、打过分（不管分高分低），下次不再重复打分
    listed —— 进了「待下载清单」，等你手动下
    filed  —— 已下载并归档进 Library\

★ **原子写入**：先写 .tmp 再 os.replace。这是从 COMSOL 那次
  `safe_save` 用 `.part` 当临时扩展名、白跑 11.8 小时的教训学来的
  —— 中途崩了也不能留下半截文件，否则整张表报废。

命令行
------
    python processed.py --stats     # 看统计
    python processed.py --recent 7  # 看最近 7 天处理的
    python processed.py --forget <doi或标题片段>   # 放行某篇（想重跑时用）
"""
import io
import json
import os
import re
import shutil
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from config import norm_title          # noqa: E402  全仓唯一的标题清洗实现

FILE = os.path.join(BASE_DIR, "processed.json")

# --forget 的防护（见 CLI 里的说明）
FORGET_MIN_LEN = 5      # 匹配串最短长度：挡住 " "、"doi:"、"2026" 这类
FORGET_MAX_SAFE = 5     # 单次删除超过这么多条就要加 --yes 二次确认

_cache = None


# --------------------------------------------------------------- 键
def key_of(paper):
    """全生命周期唯一标识：优先规范化 DOI，其次规范化标题（截断 120 字）。

    ★ 2026-10-01：标题清洗改为调用 `config.norm_title` —— 全仓唯一实现。
      原来 `sources._norm_title_key` / `manual_ingest._norm_title` 各写一份，
      已经漂了（manual_ingest 那份漏了"剥标签"）。三份算法算出三把键，
      跨清单匹配（download_list ↔ processed ↔ 销账）就可能对不上。
    """
    doi = re.sub(r"^https?://(dx\.)?doi\.org/",
                 "", (paper.get("doi") or "").strip(), flags=re.I).strip().lower()
    if doi:
        return "doi:" + doi
    return "title:" + norm_title(paper.get("title"))[:120]


# --------------------------------------------------------------- 读写
def load(force=False):
    global _cache
    if _cache is not None and not force:
        return _cache
    try:
        with io.open(FILE, encoding="utf-8") as f:
            d = json.load(f)
        _cache = d if isinstance(d, dict) else {}
    except Exception:
        _cache = {}
    return _cache


def _save():
    """★ 原子写入：写 .tmp 再 replace。崩了也不会毁掉整张表。"""
    data = load()
    tmp = FILE + ".tmp"
    try:
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, FILE)
    except Exception as e:
        print("  [警告] 去重表写入失败：%s" % e, file=sys.stderr)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


# --------------------------------------------------------------- 查询/标记
def is_done(paper):
    return key_of(paper) in load()


def status_of(paper):
    v = load().get(key_of(paper))
    return v[1] if isinstance(v, list) and len(v) > 1 else (v if v else None)


def mark(paper, status="seen", score=None):
    """记下这篇。已存在则【升级状态】（failed < seen < listed < filed），不回退。

    ★ 2026-10-01：新增 "failed" —— 归档失败、等下次重试。
      见 filter_new 的说明。
    """
    k = key_of(paper)
    if not k or k == "title:":
        return
    d = load()
    today = time.strftime("%Y-%m-%d")
    old = d.get(k)
    # ⚠️ failed / deferred 必须排在 seen 之后：mark() 是"只升不降"的，如果它们
    #    排在 seen 前面，"打分后被标 seen" 就会把它们顶掉 ⇒ 永远记不上 ⇒
    #    次日不重试（这正是要修的那个 bug）。而重试成功写 filed(4) 仍能覆盖。
    # ★ 2026-10-01 新增 "deferred"：本轮因 PDF_MAX_PER_RUN 限额【没轮到下载】。
    #   见 filter_new 的说明 —— 它和 failed 一样必须可重试。
    rank = {"seen": 1, "failed": 2, "deferred": 2, "listed": 3, "filed": 4}
    old_status = old[1] if (isinstance(old, list) and len(old) > 1) else None
    if old_status and rank.get(old_status, 0) > rank.get(status, 0):
        status = old_status              # 保留更"靠后"的状态

    # ★ 2026-10-01【日期只在状态真的推进时才刷新】。
    #   原来 `rec = [today, status]` 无条件写今天 ⇒ 一条几个月前的记录只要被
    #   "触碰"一下就冒充成最近处理过的：`--recent 7` 会把历史文献混进来，统计失真。
    #   现在状态没变就沿用原日期（= 首次录入日）。
    #   可达路径：failed 的文献天天重试（mark_many 每轮都标一次 seen，被 rank 挡住
    #   状态不变）；或手动重跑 manual_ingest 对同一篇再次 mark("filed")。
    if old and old_status == status and old[0]:
        day = old[0]
    else:
        day = today
    rec = [day, status]
    if score is not None:
        rec.append(int(score))
    elif old and len(old) > 2:
        rec.append(old[2])
    d[k] = rec


def mark_many(papers, status="seen"):
    for p in papers:
        mark(p, status, p.get("total_score"))
    _save()


RETRYABLE = ("failed", "deferred")   # 这两种状态要放行重试，见下


def filter_new(papers):
    """返回【没处理过】的，外加【该重试】的。放在打分之前调用。

    ★ 2026-10-01：状态为 "failed" / "deferred" 的【放行】。
      原来只要键在表里就一律拦掉，而这会踩死两条路：

      · **failed**（归档失败）：文件下下来了但归档时被 EndNote 锁住 ⇒
        中文重命名与 Library 归档永久失效。

      · **deferred**（本轮 PDF_MAX_PER_RUN 限额没轮到）：这是更隐蔽的一条。
        `paper_radar` 在【下载之前】就把所有打过分的一律标成 seen，
        然后下载时才发现"第 21 篇之后的下不了"，把它记进 deferred_keys。
        可它早就被标成 seen 了 ⇒ 次日 filter_new 直接拦掉 ⇒
        **既不会被自动补下，又被 `_settled` 排除在待下载清单之外 —— 彻底掉进黑洞**。
        （原注释写的"下轮自动补下"从来没有对应的机制。）

      放行时打 `_retry_archive` 标记，让 paper_radar 把这篇【无条件】塞进
      下载/归档批次，并【排在最前面】—— 否则它会排在队尾再被限额切掉一次。
    """
    d = load()
    out, skipped, retry = [], 0, 0
    for p in papers:
        k = key_of(p)
        rec = d.get(k) if (k and k != "title:") else None
        st = None
        if rec is not None:
            st = rec[1] if isinstance(rec, list) and len(rec) > 1 else "seen"
        if rec is not None and st not in RETRYABLE:
            skipped += 1
            continue
        if st in RETRYABLE:
            retry += 1
            p["_retry_archive"] = True
        out.append(p)
    if skipped:
        print("  [去重] 已处理过 %d 篇，跳过；本轮新文献 %d 篇" % (skipped, len(out)))
    if retry:
        print("  [重试] 其中 %d 篇上次归档失败/被限额推迟，本轮优先重走下载归档"
              % retry)
    return out


def stats():
    d = load()
    c = {}
    for v in d.values():
        s = v[1] if isinstance(v, list) and len(v) > 1 else "?"
        c[s] = c.get(s, 0) + 1
    return len(d), c


# --------------------------------------------------------------- CLI
def main():
    if "--stats" in sys.argv:
        n, c = stats()
        print("去重表：%s" % FILE)
        print("  总计 %d 条" % n)
        # ★ 2026-10-01：原来硬编码 ("seen","listed","filed") 三项 —— 加了
        #   "failed" 之后它被直接吞掉，用户看不到有多少篇卡在重试里。
        #   改成动态遍历，将来再加状态也不用改这里。
        known = ["seen", "listed", "filed", "failed"]
        for k in known:
            if c.get(k):
                _note = "   ← 归档失败，等下次重试" if k == "failed" else ""
                print("    %-8s %d%s" % (k, c[k], _note))
        for k in sorted(set(c) - set(known)):
            print("    %-8s %d   ← 未知状态" % (k, c[k]))
        if os.path.exists(FILE):
            print("  文件大小 %.1f MB" % (os.path.getsize(FILE) / 1e6))
        return 0

    if "--recent" in sys.argv:
        i = sys.argv.index("--recent")
        days = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) else 7
        cut = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        rows = [(k, v) for k, v in load().items()
                if isinstance(v, list) and v and v[0] >= cut]
        rows.sort(key=lambda x: x[1][0], reverse=True)
        print("最近 %d 天处理过 %d 篇（截止 %s）：" % (days, len(rows), cut))
        for k, v in rows[:40]:
            print("  %s  %-8s %s" % (v[0], v[1], k[:78]))
        return 0

    if "--forget" in sys.argv:
        i = sys.argv.index("--forget")
        pat = (sys.argv[i + 1] if i + 1 < len(sys.argv) else "").strip()

        # ★ 2026-10-01【加防护】。原来 `pat and pat.lower() in k.lower()` 就删，
        #   既没有长度下限也没有确认，也没有备份 —— 手滑一下就不可逆。
        #   实际能踩的坑：
        #     --forget " "   → 所有 title: 键（中文/英文标题都带空格）全没
        #     --forget doi:  → 所有 doi: 键全没
        #   而删光去重表的后果是【次日全量历史文献重新抓取/重复打分/重复推送】。
        if len(pat) < FORGET_MIN_LEN:
            print("[拒绝] 匹配串太短（%d 个字符），下限是 %d。"
                  % (len(pat), FORGET_MIN_LEN))
            print("       像 'doi:' 或一个空格会把整类记录一次删光。")
            print("       请给一个更具体的片段，例如 doi:10.1016/j.enggeo.2026")
            return 2

        d = load()
        hit = [k for k in d if pat.lower() in k.lower()]
        if not hit:
            print("没有匹配到任何条目，未改动。")
            return 0

        if len(hit) > FORGET_MAX_SAFE and "--yes" not in sys.argv:
            print("[拒绝] 命中 %d 条，超过安全阈值 %d。" % (len(hit), FORGET_MAX_SAFE))
            print("       核对下面这些确实是你要删的，再加 --yes 重跑：")
            for k in hit[:15]:
                print("         " + k[:88])
            if len(hit) > 15:
                print("         …… 另外 %d 条" % (len(hit) - 15))
            return 2

        # 删前先备份：--forget 是唯一会【减少】记录的入口，误删不可逆
        if os.path.exists(FILE):
            bak = FILE + ".bak_forget_" + time.strftime("%Y%m%d_%H%M%S")
            try:
                shutil.copy2(FILE, bak)
                print("已备份：%s" % bak)
            except Exception as e:
                print("[警告] 备份失败（继续执行）：%s" % e)

        for k in hit:
            del d[k]
        _save()
        print("已放行 %d 条（下次会重新处理）" % len(hit))
        for k in hit[:10]:
            print("  " + k[:90])
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
