# Name: Geo_Paper_Radar 地学文献雷达
# Description: 多源自动检索地学文献 → 本地 Qwen 打分 → 自动下载/重命名/归类 → 生成简报。**用户说「运行文献雷达 / 获取今日论文 / 推送地学文献」时加载。**

## ★ 先读这个

**完整交接文档：`交接_当前状态与下一步.md`**（同目录）——含架构、配置、踩过的坑、待办。
**详细技术记录**：`~/.claude/projects/d-------/memory/literature-toolchain.md`

## Level 1: 触发词

- 运行文献雷达 / 获取今日论文 / 推送地学文献
- 看今天的文献简报 / 待下载清单是什么
- 处理手动下载的 PDF

## Level 2: 运行方式

**不要手动跑完整流程**（15–20 分钟，会下载 PDF、发邮件）。它已由定时任务自动化：

| | 时间 | 做什么 |
|---|---|---|
| 云端 GitHub Actions | **每天 08:07** | 只抓取攒候选，不花钱 |
| 本机 Windows 计划任务 | **周一–周五 11:30** | 完整流程 |

**只在用户明确要求"现在就跑一次"时才手动执行** `python paper_radar.py`。

## Level 3: 常用动作

```bash
# 冒烟测试（云端路径，约 2 分钟，不下载不发信）—— 改完代码先跑这个
PAPER_RADAR_MODE=harvest python paper_radar.py

# 处理手动下载的 PDF（用户把 PDF 丢进 E:/论文/手动下载/ 之后）
python manual_ingest.py              # 重命名 + 归类 + 进 EndNote
python manual_ingest.py --pending    # 看待下载清单状态

# 从 QQ 邮箱批量取历史 .ris（幂等，可重跑）
python fetch_ris_from_mail.py --scan
python fetch_ris_from_mail.py
```

## Level 4: 关键事实

- **打分默认走本地 Qwen3.5-9B**（免费，1.4 s/篇）；本地挂了会自动**熔断到 40 篇**再回退 DeepSeek
- **只有标题的论文只判"相关/不相关"，不打四维分**（标题信息量不够）
- 输出分三类：`Library/<主题>/*.pdf`、`Library/简报/`、`Library/待下载清单/`
- 下载到 `PDF_Inbox/` 的会由 EndNote 的自动导入文件夹收走
- **改方向只需改 `research_profile.py`**（研究画像 + 主题黑名单）

## Level 5: 硬约束

- ⚠️ **底模 `.env` 里的密钥绝不能提交**——每次改完跑一次泄密自查（见交接文档第七节）
- ⚠️ 与 MinerU 抢显存，**不能同时开**
- ⚠️ `harvest/` 必须提交（云端靠它传候选）；`Library/ PDF_Inbox/ EndNote_Watch/ processed.json` 都已 gitignore
