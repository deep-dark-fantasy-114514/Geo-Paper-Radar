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
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(BASE_DIR, "processed.json")

_cache = None


# --------------------------------------------------------------- 键
def key_of(paper):
    """与 library_manager.key_of 保持一致的键规则。"""
    doi = (paper.get("doi") or "").strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    if doi:
        return "doi:" + doi
    t = (paper.get("title") or "").strip().lower()
    t = re.sub(r"<[^>]+>", " ", t)                    # 去掉 XML 标签
    t = re.sub(r"[^0-9a-z一-鿿 ]", "", re.sub(r"\s+", " ", t))
    return "title:" + t[:120]


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
    """记下这篇。已存在则【升级状态】（seen → listed → filed），不回退。"""
    k = key_of(paper)
    if not k or k == "title:":
        return
    d = load()
    today = time.strftime("%Y-%m-%d")
    old = d.get(k)
    rank = {"seen": 0, "listed": 1, "filed": 2}
    if old and isinstance(old, list) and len(old) > 1:
        if rank.get(old[1], 0) > rank.get(status, 0):
            status = old[1]              # 保留更"靠后"的状态
    rec = [today, status]
    if score is not None:
        rec.append(int(score))
    elif old and len(old) > 2:
        rec.append(old[2])
    d[k] = rec


def mark_many(papers, status="seen"):
    for p in papers:
        mark(p, status, p.get("total_score"))
    _save()


def filter_new(papers):
    """返回【没处理过】的那些。放在打分之前调用，省下重复打分的时间。"""
    d = load()
    out, skipped = [], 0
    for p in papers:
        k = key_of(p)
        if k and k != "title:" and k in d:
            skipped += 1
            continue
        out.append(p)
    if skipped:
        print("  [去重] 已处理过 %d 篇，跳过；本轮新文献 %d 篇" % (skipped, len(out)))
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
        for k in ("seen", "listed", "filed"):
            print("    %-8s %d" % (k, c.get(k, 0)))
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
        pat = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        d = load()
        hit = [k for k in d if pat and pat.lower() in k.lower()]
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
