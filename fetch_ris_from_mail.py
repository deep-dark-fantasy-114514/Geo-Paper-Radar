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
import imaplib
import io
import os
import re
import sys
import time
from datetime import datetime

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


def load_env():
    """读 .env（不依赖 python-dotenv，避免多一个依赖）。"""
    cfg = {}
    if not os.path.exists(ENV):
        return cfg
    with io.open(ENV, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


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
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(s or ""))
    return s.strip(" ._")[:maxlen] or "unnamed.ris"


def scan_or_fetch(args, cfg):
    user = cfg.get("SMTP_SENDER") or cfg.get("SMTP_RECEIVER")
    pwd = cfg.get("SMTP_PASSWORD")
    if not user or not pwd:
        print("[错误] .env 里缺 SMTP_SENDER / SMTP_PASSWORD", file=sys.stderr)
        print("       （IMAP 与 SMTP 用的是同一个授权码）", file=sys.stderr)
        return 2

    print("连接 %s:%d …（账号 %s）" % (IMAP_HOST, IMAP_PORT, user))
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
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
        typ, data = M.search(None, *crit)
        if typ != "OK":
            print("[错误] 搜索失败：%s" % data, file=sys.stderr)
            return 1
        ids = data[0].split()
        print("  日期条件：%s ⇒ 命中 %d 封邮件，开始筛主题…"
              % (" ".join(crit), len(ids)))

        hit, natt = [], 0
        for i, mid in enumerate(ids, 1):
            # ★ 只取头，且用 PEEK ⇒ 不标记已读、不拉正文
            typ, d = M.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])")
            if typ != "OK" or not d or not d[0]:
                continue
            raw = d[0][1] if isinstance(d[0], tuple) else b""
            hdr = email.message_from_bytes(raw if isinstance(raw, bytes) else raw.encode())
            subj = decode_hdr(hdr.get("Subject"))
            if DEFAULT_SUBJECT in subj or args.match in subj:
                hit.append((mid, subj, decode_hdr(hdr.get("Date"))))
            if i % 200 == 0:
                print("    …已扫 %d/%d" % (i, len(ids)))

        print("  主题含「%s」的邮件：%d 封" % (args.match, len(hit)))
        if not hit:
            return 0
        hit.sort(key=lambda x: x[2])          # 按日期排序
        print("  最早 %s ／ 最新 %s" % (hit[0][2][:31], hit[-1][2][:31]))

        if args.scan:
            print("\n  （--scan 模式：只侦察不下载。去掉 --scan 即开始下载）")
            for mid, subj, dt in hit[:8]:
                print("    · %s  %s" % (dt[:25], subj))
            if len(hit) > 8:
                print("    …还有 %d 封" % (len(hit) - 8))
            return 0

        if not args.dry_run:
            os.makedirs(args.out, exist_ok=True)
        saved = skipped = failed = 0
        t0 = time.time()
        for k, (mid, subj, dt) in enumerate(hit, 1):
            typ, d = M.fetch(mid, "(BODY.PEEK[])")     # 整封，但不标已读
            if typ != "OK" or not d or not d[0]:
                failed += 1
                continue
            raw = d[0][1]
            msg = email.message_from_bytes(raw)
            for part in msg.walk():
                fn = decode_hdr(part.get_filename())
                if not (fn or "").lower().endswith(".ris"):
                    continue
                natt += 1
                fn = safe_name(fn)
                path = os.path.join(args.out, fn)
                if os.path.exists(path):               # 幂等
                    skipped += 1
                    continue
                if args.dry_run:
                    print("    [预览] %s" % fn)
                    continue
                try:
                    payload = part.get_payload(decode=True) or b""
                    with open(path, "wb") as f:
                        f.write(payload)
                    saved += 1
                except Exception:
                    failed += 1
            if k % 20 == 0:
                print("    …已处理 %d/%d 封，存下 %d 个" % (k, len(hit), saved))

        print("\n" + "=" * 60)
        print("邮件 %d 封 ｜ 附件 %d 个 ｜ 新存 %d ｜ 已存在跳过 %d ｜ 失败 %d"
              % (len(hit), natt, saved, skipped, failed))
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
    ap.add_argument("--scan", action="store_true", help="只侦察有多少封/多少附件")
    ap.add_argument("--dry-run", action="store_true", help="列出会存什么，不落盘")
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="只取该日期之后的邮件（IMAP SINCE，按发信日期）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    ap.add_argument("--match", default=DEFAULT_SUBJECT, help="主题关键词")
    args = ap.parse_args()
    return scan_or_fetch(args, load_env())


if __name__ == "__main__":
    sys.exit(main())
