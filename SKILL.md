---
AgentSkillMetadata:
  name: "Geo_Paper_Radar"
  display_name: "🌍 地学文献雷达 V2.0"
  description: "全自动地学文献 RSS 抓取 → AI 多维度打分 → 双轨制筛选 → 邮件推送 + EndNote .ris 引文导出"
  version: "2.0.0"
  author: "Geo_Paper_Radar"
  tags: ["地学", "文献", "RSS", "DeepSeek", "邮件推送", "EndNote", "双轨制"]
---

# 🌍 Geo_Paper_Radar V2.0 — 地学文献雷达

> 全自动抓取地学顶级期刊最新文献 → DeepSeek AI 四维度智能评分 → 双轨制筛选 → HTML 邮件推送 + EndNote .ris 引文导出

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
SMTP_PASSWORD=your-auth-code-here    # 注意：这是 SMTP 授权码，不是登录密码
SMTP_RECEIVER=receiver@example.com    # 接收文献推送的邮箱
```

**如何获取 QQ 邮箱 SMTP 授权码？**
1. 登录 QQ 邮箱 → 设置 → 账户
2. 找到 "POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务"
3. 开启 "SMTP服务"，生成授权码并复制到 `.env` 的 `SMTP_PASSWORD` 字段

### 2.2 配置 RSS 期刊源（可选）

编辑 `paper_radar.py` 中的 `RSS_SOURCES` 列表，替换或增加你关注的期刊 RSS 链接。

---

## Level 3 — 执行

在终端中执行以下命令：

```bash
cd Geo_Paper_Radar
python paper_radar.py
```

### V2.0 运行流程

```
🌟 Geo_Paper_Radar V2.0 — 地学文献雷达启动 🌟

【模块一】RSS 文献抓取
  → 抓取 Engineering Geology ... 30篇
  → 抓取 Geomorphology ... 13篇

【模块二】DeepSeek AI 多维度智能打分 (V2.0)
  → (1/42) 正在打分 [Engineering Geology]: xxx...
  → [得分] 总分 35/40  S:8 R:9 P:8 M:10 | 创新突破

【模块三】双轨制筛选 (V2.0)
  → [轨道A] 总分≥30/40: 4 篇
  → [轨道B] 创新分≥9/10: 2 篇
  → [通关] 5 篇 → 推送邮件 + 生成 .ris
  → [备选] 3 篇 → 仅终端 + 生成 .ris

【EndNote 联动】生成 .ris 引文文件
  ✅ 2025-06-07_32分_创新热-水-力耦合模型.ris
  ✅ 2025-06-07_30分_土壤入渗空间异质性.ris

【模块四】发送邮件
  → [成功] 邮件已发送至 receiver@example.com

🏁 任务完成！
📊 42篇 → 通关 5 篇 → 备选 3 篇 → .ris 文件 8 个
```

### 执行结果

| 结果 | 说明 |
|------|------|
| ✅ 邮件 + .ris | 通关文献 → 邮件推送 **+** `.ris` 存入 `EndNote_Watch/` |
| 📖 仅 .ris（备选） | 未通关但 ≥60 分 → 终端显示 **+** `.ris` 存入 `EndNote_Watch/` |
| ℹ️ 今日无强相关 | 终端提示，不发送邮件 |

---

## V2.0 新特性

### 🧠 四维度智能评分（每项 0-10 分）

| 维度 | 说明 |
|------|------|
| 🏔️ 斜坡稳定性 | 边坡失稳机理、稳定性分析、加固技术 |
| 🌧️ 降雨入渗 | 雨水入渗过程、渗流场分析、入渗模型 |
| 💧 优先流 | 大孔隙流、根土间隙流、裂隙流 |
| 🔬 方法创新 | 方法/模型/实验设计的新颖性 |

### 🎯 双轨制筛选

- **轨道A（总分达标）**：`total_score ≥ 30/40`（等价旧版 75 分）
- **轨道B（创新突破）**：`method_innovation ≥ 9/10`（单项亮点）
- **备选泛读**：`total_score ≥ 24/40`（等价旧版 60 分），仅存 .ris 不打扰

### 📁 EndNote 联动

- 所有通关及备选文献自动生成标准 `.ris` 格式引文文件
- 存入 `EndNote_Watch/` 文件夹，文件名：`日期_分数_标题.ris`
- 可直接导入 EndNote / Zotero / Mendeley 等文献管理软件

---

## 技术栈

| 组件 | 技术 |
|------|------|
| RSS 解析 | `feedparser` + `requests`（伪装 UA + 重试）|
| AI 评分 | `openai` SDK → DeepSeek Chat API（4 维度）|
| 邮件发送 | `smtplib` + QQ邮箱 SSL 465 端口 |
| 引文导出 | 标准 `.ris` 格式 |
| 环境配置 | `python-dotenv` |

## 文件结构

```
Geo_Paper_Radar/
├── .env                  # 环境变量配置
├── paper_radar.py        # 核心脚本 V2.0
├── history.json          # 自动生成 — 已推送文献记录
├── SKILL.md              # 本说明书
└── EndNote_Watch/        # 自动生成 — .ris 引文文件存放处
    ├── 2025-06-07_32分_创新热-水-力耦合模型.ris
    └── ...