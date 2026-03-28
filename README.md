# News Tracker

每天定时抓取科技新闻和 Kickstarter 众筹项目，通过微信 ClawBot 推送中文简报，支持回复序号获取详细解读。

## 功能

- **定时推送**：每天指定时间（默认 14:00 北京时间）自动推送
- **新闻来源**：The Verge Tech + Reviews（RSS）
- **众筹项目**：Kickstarter 科技类最新正在众筹项目
- **广告过滤**：自动过滤赞助/促销内容
- **按需解读**：回复数字序号，获取 500-800 字中文详细解读
- **开机自启**：通过 macOS LaunchAgent 自动运行

## 依赖

- Python 3.10+
- [uv](https://github.com/astral-sh/uv)（包管理）
- [DeepSeek API](https://platform.deepseek.com/)（中文摘要与解读）
- [微信 ClawBot / openclaw](https://openclaw.ai/)（微信消息收发）
- Playwright + Chromium（Kickstarter 抓取）

## 快速开始

### 1. 安装依赖

```bash
uv sync
uv pip install playwright
uv run playwright install chromium
```

### 2. 配置

复制示例配置并填写：

```bash
cp config.example.json config.json
```

编辑 `config.json`，填入微信 ClawBot 的 `token` 和 `user_id`。

创建 `.env` 文件：

```
DEEPSEEK_API_KEY=your_deepseek_api_key
```

如需抓取 The Verge 付费文章正文，将登录 Cookie 保存到 `cookies.json`：

```json
{
  "duet:identitySession": "...",
  "duet:identityAuthenticated": "true"
}
```

### 3. 获取微信 user_id

```bash
python tracker.py setup
```

在微信 ClawBot 里发一条消息，自动保存 user_id。

### 4. 运行测试

```bash
python tracker.py test
```

### 5. 启动定时服务

```bash
python tracker.py run
```

或配置 macOS LaunchAgent 开机自启（见下方）。

## macOS 自启配置

创建 `~/Library/LaunchAgents/com.news.tracker.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.news.tracker</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/.venv/bin/python</string>
        <string>/path/to/tracker.py</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/news</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/path/to/news/data/tracker.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/news/data/tracker.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.news.tracker.plist
```

## 配置说明

| 字段 | 说明 |
|------|------|
| `feeds` | RSS 订阅源列表，`enabled: false` 可禁用 |
| `kickstarter.enabled` | 是否启用 Kickstarter 抓取 |
| `kickstarter.max_per_day` | 每天最多推送的 Kickstarter 项目数 |
| `send_time` | 每日推送时间（北京时间，24h 格式） |
| `max_per_day` | The Verge 每日最多文章数 |
| `wechat.token` | ClawBot 账号 Token |
| `wechat.user_id` | 接收消息的微信用户 ID |

## 命令

```bash
python tracker.py run      # 启动定时推送 + 消息监听
python tracker.py test     # 立刻推送一次（测试用）
python tracker.py setup    # 首次配置，获取 user_id
```
