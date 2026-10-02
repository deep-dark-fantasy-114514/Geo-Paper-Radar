#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_ris_from_mail.py —— 把 QQ 邮箱里【历史上所有】文献雷达邮件中的 .ris 附件
                          一次性全捞下来，省掉一封封手点。

背景
----
云端工作流以前每天发一封「地学前沿推送」邮件，每封挂着当天的 .ris 附件。
几个月下来攒了一堆，手点要很久。但 QQ 邮箱支持 IMAP，用你 .env 里
已有的【授权码】（就是 SMTP_PASSWORD 那个，SMTP/IMAP 通用）就能批量取。

用法
----
    python fetch_ris_from_mail.py --scan              # 先侦察：有多少封、共几个附件
    python fetch_ris_from_mail.py                     # 下载到 EndNote_Watch\\
    python fetch_ris_from_mail.py --since 2026-06-01  # 只取某日期之后的
    python fetch_ris_from_mail.py --out <目录>         # 换个输出目录
    python fetch_ris_from_mail.py --dry-run           # 列出会存什么，不落盘

下完之后
--------
不用一封封导入 EndNote！用 EndNote 的【批量导入文件夹】一次搞定：
    File → Import → Folder …
    Import Folder:  EndNote_Watch
    Import Option:  Reference Manager (RIS)
    Duplicates:     Discard Duplicates
    → Import
（EndNote 自己会去重，重复导入无害。）

安全
----
· 只读邮件，不删不发不改；用 BODY.PEEK 取信，不标记已读
· 不打印密码；证书校验开启
"""
import argparse
import email
import email.header
import email.utils
import hashlib
import imaplib
import io
import os
import re
import sys
import time
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(BASE_DIR, ".env")
DEFAULT_OUT = os.path.join(BASE_DIR, "EndNote_Watch")
DEFAULT_SUBJECT = "地学前沿推送"     # 与 paper_radar.py 里的邮件主题一致

IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
IMAP_TIMEOUT = 60          # 单次 IMAP 读写的 socket 超时（秒）


def load_env():
    """读 .env。

    ★ 2026-10-02：改用 `dotenv.dotenv_values` —— 本项目其它地方（config.py）
      用的就是 python-dotenv，这里原来自己手写了一个解析器，等于
      **同一个 .env 有两套语义**（`export K=V`、行尾注释、转义引号的处理都不同）。
      dotenv 本来就是已装依赖，没有理由再维护第二份。
    """
    try:
        from dotenv import dotenv_values
        return dict(dotenv_values(ENV))
    except Exception:
        return {}


def decode_hdr(s):
    """邮件头可能是 =?utf-8?B?...?= 编码的，解出来。

    ★ Date 之类是纯 ASCII，不能走 make_header（会把日期吃成空串）。
    """
    if not s:
        return ""
    try:
        parts = email.header.decode_header(s)
        if all(p[1] is None for p in parts):     # 纯 ASCII ⇒ 原样返回
            return s
        return str(email.header.make_header(parts))
    except Exception:
        return str(s)


def safe_name(s, maxlen=110):
    """附件名清洗。

    ★ 2026-10-02：删掉本地这份实现，改用 config.safe_filename（全仓唯一）。
      原来这份只把非法字符换成 `_`，**没有 Windows 保留设备名（CON/AUX/NUL…）
      的处理** —— 而配置层早就为这件事写过代码了。
      （而且把 `A/B.ris`、`A:B.ris` 都换成 `A_B.ris` 会制造同名冲突，
        见下面 save_attachment 里的 hash 去冲突。）
    """
    try:
        from config import safe_filename
        n = safe_filename(os.path.basename(str(s or "")), max_len=maxlen)
    except Exception:
        n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", os.path.basename(str(s or "")))
    return n or "unnamed.ris"


def _looks_like_ris(payload):
    """内容级校验：光看扩展名不算数。"""
    if not payload:
        return False
    try:
        t = payload.decode("utf-8-sig", "replace")
    except Exception:
        return False
    return ("TY  - " in t) and ("ER  -" in t)


def _atomic_write(path, payload):
    """原子写：写临时文件 → fsync → os.replace。

    ★ 2026-10-02：原来直接 `open(path,"wb")`。中途被杀会留下**残缺的 .ris**，
      而下一次的幂等判断是 `os.path.exists(path)` ⇒ 残缺文件被当成"已下载"，
      **永远不会重试**。全仓其它地方（processed / save_history / .ris 生成 /
      PDF 缓存）早就是原子写，这里补齐。
    """
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _subject_hit(subj, match):
    """主题是否命中。

    ★ 2026-10-02：原来判据是 `DEFAULT_SUBJECT in subj or args.match in subj`。
      两个副作用：
        · `--match X` 并不"只匹配 X"，默认主题照样会命中 —— 与用户预期不符；
        · `--match ""` 时 `"" in subj` 恒为真 ⇒ **整个 INBOX 全部命中**，
          接着对每封拉 BODY.PEEK[]，数据量可能失控。
      ⇒ 现在只按一个关键词判，且空关键词回退到默认（见 main()）。
    """
    m = (match or "").strip()
    if not m:
        return False
    return m in (subj or "")


def _msg_date(hdr):
    """从邮件头里取一个可比较的时间。

    ★ 2026-10-02：**QQ 的 IMAP 不返回 `Date` 头**。实测
      `BODY.PEEK[HEADER]` 拿回完整头部，字段只有
      From / To / Subject / Message-ID / Received / Content-Type / MIME-Version
      —— 没有 Date。所以原来那句
          hit.append((mid, subj, decode_hdr(hdr.get("Date"))))
      **一直都是空字符串**，排序其实退化成了"按输入顺序"，
      日志里的"最早／最新"也一直是空的。
      ⇒ 依次尝试 Date → Received（Received 末尾分号后就是时间）。
    """
    for field in ("Date", "Received"):
        v = hdr.get(field)
        if not v:
            continue
        # Received 形如 "from a by b with ESMTP id x; Thu, 1 Oct 2026 10:30:00 +0800"
        if field == "Received" and ";" in v:
            v = v.rsplit(";", 1)[-1]
        dt = _parse_date(v)
        if dt:
            return dt
    return None


def _parse_date(s):
    """把邮件 Date 头解析成可比较的 datetime（解析不了返回 None）。

    ★ 2026-10-02：原来直接拿 Date 的**原始字符串**排序。而邮件日期写法五花八门
      （`1 Oct` / `01 Oct`、`GMT` / `+0800` / `-0700`），字符串序 ≠ 时间序。
    """
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def scan_or_fetch(args, cfg):
    user = cfg.get("SMTP_SENDER") or cfg.get("SMTP_RECEIVER")
    pwd = cfg.get("SMTP_PASSWORD")
    if not user or not pwd:
        print("[错误] .env 里缺 SMTP_SENDER / SMTP_PASSWORD", file=sys.stderr)
        print("       （IMAP 与 SMTP 用的是同一个授权码）", file=sys.stderr)
        return 2

    print("连接 %s:%d …（账号 %s）" % (IMAP_HOST, IMAP_PORT, user))
    try:
        # ★ 2026-10-02：加 socket 超时。原来裸连 —— 网络半死不活时
        #   会一直挂着；这是无人值守的批量任务，必须有超时。
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
    except Exception as e:
        print("[错误] 连不上 IMAP：%s" % e, file=sys.stderr)
        return 2
    try:
        M.login(user, pwd)
    except imaplib.IMAP4.error as e:
        print("[错误] 登录失败：%s" % e, file=sys.stderr)
        print("       多半是授权码不对。QQ 邮箱 → 设置 → 账户 → "
              "POP3/IMAP/SMTP服务 → 生成授权码", file=sys.stderr)
        M.logout()
        return 2

    try:
        M.select("INBOX", readonly=True)      # ★ 只读，不改动邮箱状态
        crit = ["SINCE", args.since] if args.since else ["ALL"]
        # ★★ 2026-10-02【改用 UID】★★
        #   原来用 `M.search` + 序号。序号会随别的客户端（手机 QQ、网页版）
        #   删除/移动邮件而**整体前移** —— 一个跨越几百封、要跑几分钟的批量
        #   任务，期间序号一变就会取错邮件或取不到。
        #   UID 在会话内是稳定的，还顺带为"断点续传"留了钩子。
        typ, data = M.uid("search", None, *crit)
        if typ != "OK":
            print("[错误] 搜索失败：%s" % data, file=sys.stderr)
            return 1
        uids = data[0].split()
        print("  日期条件：%s ⇒ 命中 %d 封邮件，开始筛主题…"
              % (" ".join(crit), len(uids)))

        hit = []
        for i, uid in enumerate(uids, 1):
            # ★ 只取头，且用 PEEK ⇒ 不标记已读、不拉正文
            # ★ 字段表里必须带上 RECEIVED —— QQ 不给 Date，这是唯一的时间来源
            typ, d = M.uid("fetch", uid,
                           "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE RECEIVED)])")
            if typ != "OK" or not d or not d[0]:
                continue
            e = d[0]
            raw = e[1] if isinstance(e, tuple) else e
            hdr = email.message_from_bytes(raw if isinstance(raw, bytes)
                                           else raw.encode())
            subj = decode_hdr(hdr.get("Subject"))
            if _subject_hit(subj, args.match):
                hit.append((uid, subj, decode_hdr(hdr.get("Date")),
                            _msg_date(hdr)))
            if i % 200 == 0:
                print("    …已扫 %d/%d" % (i, len(uids)))

        print("  邮件主题命中：%d 封" % len(hit))
        if not hit:
            return 0
        # ★ 按解析出的 datetime 排序；取不到时间的用 UID 兜底 —— UID 是按
        #   到达顺序单调递增的，本身就是一条可靠的时间线。
        _MINDT = datetime.min.replace(tzinfo=timezone.utc)
        hit.sort(key=lambda x: (x[3] or _MINDT, int(x[0])))

        def _dstr(x):
            return x[3].strftime("%Y-%m-%d %H:%M") if x[3] else (x[2][:25] or "?")

        print("  最早 %s ／ 最新 %s" % (_dstr(hit[0]), _dstr(hit[-1])))

        if args.scan:
            print("\n  （--scan 模式：只侦察不下载。去掉 --scan 即开始下载）")
            for x in hit[:8]:
                print("    · %s  %s" % (_dstr(x), x[1]))
            if len(hit) > 8:
                print("    …还有 %d 封" % (len(hit) - 8))
            return 0

        if not args.dry_run:
            os.makedirs(args.out, exist_ok=True)
        saved = skipped = failed = 0
        failed_detail = []
        t0 = time.time()

        for k, (uid, subj, dt, _pdt) in enumerate(hit, 1):
            typ, d = M.uid("fetch", uid, "(BODY.PEEK[])")   # 整封，但不标已读
            if typ != "OK" or not d or not d[0]:
                failed += 1
                failed_detail.append((uid.decode(), subj, "fetch 失败"))
                continue
            raw = d[0][1] if isinstance(d[0], tuple) else b""
            msg = email.message_from_bytes(raw)
            for part in msg.walk():
                fn = decode_hdr(part.get_filename())
                if not (fn or "").lower().endswith(".ris"):
                    continue
                try:
                    payload = part.get_payload(decode=True) or b""
                except Exception as e:
                    failed += 1
                    failed_detail.append((uid.decode(), subj, "附件解码失败 " + str(e)[:40]))
                    continue
                # ★ 内容级校验：扩展名叫 .ris 不等于内容是 RIS
                if not _looks_like_ris(payload):
                    failed += 1
                    failed_detail.append((uid.decode(), subj,
                                          "内容不是 RIS（%d 字节）" % len(payload)))
                    print("    [跳过] %s 内容不是 RIS，未落盘" % (fn or "?"))
                    continue
                if args.dry_run:
                    print("    [预览] %s（%d 字节）"
                          % (safe_name(fn), len(payload)))
                    saved += 1
                    continue
                # ★ 同名不同内容 ⇒ 加内容 hash 后缀，不再"存在即跳过"。
                #   原来 `exists → skip` 会让【同名但不同论文】的附件永久丢失。
                base = safe_name(fn)
                path = os.path.join(args.out, base)
                if os.path.exists(path):
                    try:
                        with open(path, "rb") as f:
                            if f.read() == payload:
                                skipped += 1
                                continue
                    except Exception:
                        pass
                    stem, ext = os.path.splitext(base)
                    path = os.path.join(
                        args.out, "%s__%s%s"
                        % (stem, hashlib.sha256(payload).hexdigest()[:6], ext))
                    if os.path.exists(path):
                        skipped += 1
                        continue
                try:
                    _atomic_write(path, payload)
                    saved += 1
                except Exception as e:
                    failed += 1
                    failed_detail.append((uid.decode(), subj, "写盘失败 " + str(e)[:40]))
            if k % 20 == 0:
                print("    …已处理 %d/%d 封，存下 %d 个" % (k, len(hit), saved))

        print("\n" + "=" * 60)
        print("邮件 %d 封 ｜ 新存 %d ｜ 已存在跳过 %d ｜ 失败 %d"
              % (len(hit), saved, skipped, failed))
        if failed_detail:
            fp = os.path.join(BASE_DIR, "fetch_ris_failures.csv")
            try:
                with io.open(fp, "w", encoding="utf-8-sig", newline="") as f:
                    f.write("uid,subject,reason\n")
                    for u, s, r in failed_detail:
                        f.write('"%s","%s","%s"\n'
                                % (u, s.replace('"', "'"), r.replace('"', "'")))
                print("  失败明细：%s（含 UID，可据此单独重跑）" % fp)
            except Exception:
                for u, s, r in failed_detail[:5]:
                    print("    ✗ uid=%s %s：%s" % (u, s[:40], r))
        if not args.dry_run:
            print("输出目录：%s" % args.out)
            print("耗时 %.0f 秒" % (time.time() - t0))
            print("\n下一步：不用一封封导入！用 EndNote 批量导入文件夹：")
            print("  File → Import → Folder …")
            print("    Import Folder: %s" % args.out)
            print("    Import Option: Reference Manager (RIS)")
            print("    Duplicates:    Discard Duplicates")
        return 0 if failed == 0 else 1
    finally:
        try:
            M.logout()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="把邮箱里历史 .ris 附件批量捞下来")
    ap.add_argument("--scan", action="store_true",
                    help="只侦察：命中多少封邮件（不下载）")
    ap.add_argument("--dry-run", action="store_true",
                    help="预览会存哪些 .ris（仍会下载整封邮件，但不落盘）")
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="只取该日期【当天及以后】的邮件（IMAP SINCE，按发信日期）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    ap.add_argument("--match", default=DEFAULT_SUBJECT,
                    help="主题关键词（显式指定后【只】按它筛，覆盖默认主题）")
    args = ap.parse_args()
    # ★ 2026-10-02：--match 显式给了就【只】按它筛（原来是与默认主题取 OR，
    #   用户以为在收窄，实际没有）。空/纯空白回退到默认主题，避免
    #    命中整个邮箱。
    args.match = (args.match or "").strip() or DEFAULT_SUBJECT
    return scan_or_fetch(args, load_env())


if __name__ == "__main__":
    sys.exit(main())
