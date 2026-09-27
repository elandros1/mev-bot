# MEV 夹子攻击检测与受害者触达系统

> [English Documentation](./README.md)

全自动 MEV 夹子攻击检测系统，实时监控链上 DEX 交易，识别受害者钱包，通过 Telegram 和 ntfy 推送双语诊断告警 — 全程零人工干预。

## 功能特性

- **实时夹子检测** — 监听每个新区块的 Uniswap V2/V3 Swap 事件
- **受害者地址提取** — 自动识别被夹的钱包地址
- **Web3 身份解析** — ENS 反向解析 + Farcaster/Lens 社交账号查找
- **HTML 诊断报告** — 自动生成结构化报告卡片，含利润/损失分析
- **多渠道推送** — Telegram Bot（内联按钮 + Deep Linking）+ ntfy.sh
- **双语告警卡片** — 中英文对照，全球用户一眼看懂
- **钱包订阅防护** — `/watch <地址>` 零门槛自助订阅
- **Deep Link 自动绑定** — 群成员点击按钮即可自动订阅受害者钱包
- **i18n 国际化架构** — 所有面向用户的文本抽离至 JSON 语言包

## 架构流程

```
新区块 → Swap 事件解析 → 夹子检测 → 受害者地址提取
    → ENS 反解 + Farcaster/Lens 查找
    → HTML 报告生成
    → JSON 检测存档
    → ntfy 公共广播
    → 订阅者定向通知
    → Telegram 命令处理 (/watch /unwatch /status)
```

## 快速开始

### 前置条件

- Python 3.10+
- 以太坊 RPC 节点（如 [DRPC](https://drpc.org) 公共节点）
- Telegram Bot Token（通过 [@BotFather](https://t.me/BotFather) 申请）
- （可选）[Neynar API key](https://neynar.com) 用于 Farcaster 查找

### 安装

```bash
git clone https://github.com/elandros1/mev-bot.git
cd mev-bot
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入 RPC URL、Telegram Token、ntfy topic
```

### 配置 (.env)

```env
RPC_URL=https://eth.drpc.org
TELEGRAM_BOT_TOKEN=你的Bot_Token
TELEGRAM_CHAT_ID=你的Chat_ID
NTFY_TOPIC=mev-sandwich-alerts
# 可选
NEYNAR_API_KEY=         # Farcaster 查找
TELEGRAM_API_BASE=      # Telegram API 自定义代理
HTTP_PORT=8080          # 报告 HTTP 服务端口
```

### 启动

```bash
python main.py
```

## Bot 命令

| 命令 | 说明 |
|------|------|
| `/watch <地址>` | 订阅钱包监控 |
| `/unwatch <地址>` | 取消订阅 |
| `/status` | 查看订阅列表 |
| `/stats` | 查看全局统计 |
| `/help` | 显示帮助 |

**Deep Linking**：点击告警卡片上的「保护此钱包」按钮，自动跳转 Bot 私信并绑定受害者地址，无需手动输入。

## 部署方式

### GitHub Actions（免费云端）

仓库包含 GitHub Actions 工作流（`.github/workflows/mev-bot.yml`），在云端运行 Bot。配置仓库 Secrets：

- `RPC_URL`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`NTFY_TOPIC`

在 Actions 页面点「Run workflow」即可触发。

### VPS 服务器（24/7）

```bash
# 使用 systemd 或 screen
python main.py &
```

## 国际化架构

所有面向用户的文本存储在 JSON 语言包中：

```
src/locales/
├── en.json    # 英文
└── zh.json    # 中文
```

`src/i18n.py` 模块提供：
- `t(lang, key)` — 获取单语言字符串
- `bi(key)` — 获取双语「中文 / English」组合字符串

## 项目结构

```
mev-bot/
├── main.py                     # 主程序入口
├── requirements.txt
├── .env.example
├── .github/workflows/
│   └── mev-bot.yml             # GitHub Actions 工作流
├── src/
│   ├── analyzer.py             # 夹子检测引擎
│   ├── tg_bot.py               # Telegram + ntfy + 命令处理
│   ├── report_generator.py     # HTML 报告卡片生成
│   ├── subscription_manager.py # SQLite 钱包订阅
│   ├── victim_outreach.py      # ENS + Farcaster + Lens 查找
│   ├── i18n.py                 # 国际化模块
│   └── locales/
│       ├── en.json             # 英文语言包
│       └── zh.json             # 中文语言包
├── reports/                    # 生成的 HTML 报告
├── detections/                 # JSON 检测记录
└── telegram-proxy-worker.js    # Cloudflare Worker 代理（可选）
```

## 技术栈

- **Python 3.10+** + web3.py + requests + python-dotenv
- **Uniswap V2/V3** Swap 事件解析
- **Telegram Bot API** + Deep Linking + Inline Keyboard
- **ntfy.sh** 免费推送通知
- **ENS Registry** 链上反向解析
- **Farcaster**（Neynar API）+ **Lens Protocol**（GraphQL）社交查找
- **SQLite** 轻量级订阅存储
- **GitHub Actions** 免费云端部署

## License

MIT
