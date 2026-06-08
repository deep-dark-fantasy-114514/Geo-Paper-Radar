---
AgentSkillMetadata:
  name: "Geo_Paper_Radar"
  display_name: "🌍 地学文献雷达 V3.0"
  description: "全自动地学文献抓取（RSS + OpenAlex 双源）→ 两阶段 AI 过滤 → 双轨制筛选 → 邮件推送 + EndNote .ris 引文导出"
  version: "3.0.0"
  author: "Geo_Paper_Radar"
  tags: ["地学", "文献", "RSS", "OpenAlex", "DeepSeek", "邮件推送", "EndNote", "双轨制", "Windows计划任务"]
---

# 🌍 Geo_Paper_Radar V3.0 — 地学文献雷达

> 全自动抓取地学顶级期刊（RSS）+ 大规模学术数据库（OpenAlex）→ 中英双语两阶段过滤 → DeepSeek AI 四维度评分 → 双轨制筛选 → HTML 邮件推送 + EndNote .ris 引文导出 + Windows 定时任务

---

## Level 1 — 触发词

以下任意一个触发词均可运行本 Skill：

- `运行文献雷达`
- `获取今日论文`
- `推送地学文献`

---

## Level 2 — 前置条件

> ⚠️ **首次使用前，请务必完成以下配置：**

### 2.1 编辑 `.env` 文件

在 `Geo_Paper_Radar/.env` 中填写以下信息：

```ini
# DeepSeek API 密钥（必填）
DEEPSEEK_API_KEY=sk-your-deepseek-api-key-here

# QQ 邮箱 SMTP 配置（必填）
SMTP_SENDER=your_email@qq.com
SMTP_PASSWORD=your-auth-code-here
SMTP_RECEIVER=receiver@example.com
```

> **注意：** `SMTP_SENDER` 同时作为 OpenAlex API 的 `mailto` 参数使用，用于 API 身份标识，无需额外配置。

### 2.2 配置数据源（可选）

**RSS 源**：编辑 `paper_radar.py` 中的 `RSS_SOURCES` 列表。

**OpenAlex**：无需 API Key，调整 `OPENALEX_DAYS_LOOKBACK`（默认 7 天）和 `OPENALEX_MAX_PAGES`（默认 1 页 = 200 篇）即可。

**中文期刊 ISSN**：编辑 `CHINESE_JOURNALS_ISSN` 列表添加更多中文期刊。

### 2.3 一键部署每日自动推送

以**管理员身份**双击运行 `setup_windows_scheduler.bat`，即可注册 Windows 计划任务：

- 任务名称：`Geo_Paper_Radar_Daily`
- 执行时间：**每天早晨 8:00**
- 后台静默运行，自动抓取 → 筛选 → 推送

---

## Level 3 — 执行

### 手动运行

```bash
cd Geo_Paper_Radar
python paper_radar.py
```

### 手动触发计划任务

```bash
schtasks /run /tn Geo_Paper_Radar_Daily
```

### V3.0 运行流程

```
🌟 Geo_Paper_Radar V3.0 — 地学文献雷达启动 🌟
  数据源: RSS + OpenAlex  |  过滤: 两阶段  |  双轨制筛选

【RSS 源】文献抓取
  →  Engineering Geology ... 30篇
  →  Geomorphology ... 13篇

【OpenAlex 源】大规模文献检索（方案B：纯文本搜索）
  →  请求 OpenAlex /works ...
  →  获取 ~180 条结果

📊 全局合并: RSS 43 篇 + OpenAlex 180 篇 = 223 篇

【第一阶段】本地 Regex 粗筛
  →  输入 223 篇 → 粗筛后 28 篇（命中 ≥2 核心关键词）

【第二阶段】DeepSeek AI 多维度打分
  →  (1/28) 正在打分 ...
  →  [得分] 总分 33/40  S:8 R:9 P:8 M:8 | 创新突破

【双轨制筛选】
  →  轨道A 总分≥30: 5 篇  |  轨道B 创新分≥9: 3 篇
  →  通关 6 篇 → 邮件 + .ris  |  备选 4 篇 → 仅 .ris

【发送邮件】
  →  ✅ 邮件已发送

🏁 V3.0 任务完成！  223 → 28 → 6 通关 + 4 备选
```

### 执行结果

| 结果 | 说明 |
|------|------|
| ✅ 邮件推送 | 通关文献（双轨制）→ HTML 邮件 + `.ris` |
| 📖 备选泛读 | 未通关但 ≥60 分 → 终端显示 + `.ris` |
| ℹ️ 今日无相关 | 终端提示，不发送邮件 |

---

## V3.0 新特性

### 📡 双数据源

| 数据源 | 范围 | 数量级 |
|--------|------|--------|
| **RSS** | 顶级期刊定点跟踪 | ~30-50 篇/天 |
| **OpenAlex** | 大规模跨学科检索 | ~200 篇/天 |
| **中文 ISSN** | 定向追踪中文核心期刊 | 可按需扩展 |

### 🧠 两阶段过滤算法

```
OpenAlex 返回 200+ 篇
         │
         ▼
第一阶段：本地 Regex 粗筛
  ├── 中英双语 60+ 关键词
  ├── 命中 ≥2 个 → 保留
  └── 200 → 约 20-30 篇
         │
         ▼
第二阶段：DeepSeek AI 细筛
  ├── 四维度精准打分
  ├── 双轨制通关判定
  └── 最终 5-10 篇进入邮件
```

### 🎯 双轨制筛选

- **轨道A（总分达标）**：`total_score ≥ 30/40`
- **轨道B（创新突破）**：`method_innovation ≥ 9/10`
- **备选泛读**：`total_score ≥ 24/40`

### 🕐 自动化运维

- **一键安装**：`setup_windows_scheduler.bat` → 注册为 Windows 计划任务
- **每天 8:00 自动运行**，后台静默，不打扰日常工作
- **历史去重**：已推送文献自动记录，永不重复发送

---

## 技术栈

| 组件 | 技术 |
|------|------|
| RSS 解析 | `feedparser` + `requests`（伪装 UA + 重试）|
| 大规模检索 | `requests` → OpenAlex /works API（免费，无需 Key）|
| AI 评分 | `openai` SDK → DeepSeek Chat API（4 维度）|
| 邮件发送 | `smtplib` + QQ邮箱 SSL 465 端口 |
| 引文导出 | 标准 `.ris` 格式（兼容 EndNote / Zotero）|
| 定时任务 | `schtasks` → Windows 计划任务 |
| 环境配置 | `python-dotenv` |

## 文件结构

```
Geo_Paper_Radar/
├── .env                              # 环境变量
├── paper_radar.py                    # 核心脚本 V3.0（~540行）
├── SKILL.md                          # 本说明书
├── setup_windows_scheduler.bat       # 一键部署每日自动推送
├── history.json                      # 自动生成 — 已推送文献记录
└── EndNote_Watch/                    # .ris 引文文件存放处
    ├── 2026-06-08_RSS_33分_Innovative model.ris
    ├── 2026-06-08_Open_30分_Landslide prediction.ris
    └── ...