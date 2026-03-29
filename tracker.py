#!/usr/bin/env python3
"""
News Tracker — 每天下午两点（北京时间）推送中文新闻简报
支持互动：回复「翻译第2篇」获取完整中文翻译

用法:
  python tracker.py setup    # 首次配置
  python tracker.py run      # 启动定时推送 + 消息监听
  python tracker.py test     # 立刻推一次测试
"""

import asyncio
import json
import os
import re
import sys
import httpx
import feedparser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent

# 加载 .env 环境变量
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
SENT_FILE = DATA_DIR / "sent.json"
TODAY_FILE = DATA_DIR / "today.json"   # 今日简报的文章列表，供「翻译第X篇」使用
COOKIES_FILE = BASE_DIR / "cookies.json"  # The Verge 登录 Cookie

DATA_DIR.mkdir(exist_ok=True)
CST = timezone(timedelta(hours=8))


# ── 配置 ───────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

def load_sent() -> set:
    if not SENT_FILE.exists():
        return set()
    return set(json.loads(SENT_FILE.read_text(encoding="utf-8")))

def save_sent(sent: set):
    SENT_FILE.write_text(json.dumps(list(sent)[-2000:], ensure_ascii=False, indent=2), encoding="utf-8")

def load_today() -> dict:
    if not TODAY_FILE.exists():
        return {}
    return json.loads(TODAY_FILE.read_text(encoding="utf-8"))

def save_today(articles: list[dict]):
    data = {str(i + 1): a for i, a in enumerate(articles)}
    TODAY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def load_cookies() -> dict:
    if not COOKIES_FILE.exists():
        return {}
    return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))


# ── RSS 抓取 ───────────────────────────────────────────────────────────────────

def fetch_articles(feed_cfg: dict, sent: set, limit: int) -> list[dict]:
    if "max" in feed_cfg:
        limit = min(limit, feed_cfg["max"])
    try:
        parsed = feedparser.parse(feed_cfg["url"])
    except Exception as e:
        print(f"  [错误] 抓取 {feed_cfg['name']} 失败: {e}")
        return []

    keywords = [k.lower() for k in feed_cfg.get("keywords", [])]
    results = []
    for entry in parsed.entries:
        if len(results) >= limit:
            break
        article_id = entry.get("id") or entry.get("link", "")
        if article_id in sent:
            continue
        title = entry.get("title", "")
        summary = _strip_html(entry.get("summary", ""))[:500]
        if keywords and not any(kw in (title + summary).lower() for kw in keywords):
            continue
        results.append({
            "id": article_id,
            "source": feed_cfg["name"],
            "title": title,
            "link": entry.get("link", ""),
            "summary": summary,
        })
    return results

def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


# ── Kickstarter 抓取 ───────────────────────────────────────────────────────────

def _pw_context():
    """返回配置好反检测参数的 Playwright browser context（调用方负责关闭 browser）"""
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    browser = p.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/134.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )
    return p, browser, ctx


def fetch_kickstarter_projects(sent: set, max_count: int = 5) -> list[dict]:
    """抓取 Kickstarter 科技类正在众筹（非预发布）的最新项目，并预取正文"""
    import time as _time
    print(f"[{_now()}] 抓取 Kickstarter 科技类项目…")

    try:
        p, browser, ctx = _pw_context()
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.goto(
            "https://www.kickstarter.com/discover/advanced?category_id=16&sort=newest",
            wait_until="domcontentloaded", timeout=30000,
        )
        _time.sleep(5)

        raw = page.evaluate("""() => {
            const items = [];
            document.querySelectorAll("[data-project]").forEach(el => {
                try { items.push(JSON.parse(el.getAttribute("data-project"))); } catch(e) {}
            });
            return items;
        }""")

        SOFTWARE_CATEGORIES = {"software", "apps", "web"}

        results = []
        for proj in raw:
            if proj.get("state") != "live":
                continue
            cat_name = proj.get("category", {}).get("name", "").lower()
            if cat_name in SOFTWARE_CATEGORIES:
                continue
            pid = f"ks_{proj['id']}"
            if pid in sent:
                continue
            url = proj.get("urls", {}).get("web", {}).get("project", "")
            percent = int(proj.get("percent_funded", 0))
            goal_usd = round(float(proj.get("goal", 0)) * float(proj.get("static_usd_rate", 1)))
            deadline_ts = proj.get("deadline", 0)
            deadline = datetime.fromtimestamp(deadline_ts, CST).strftime("%-m月%-d日") if deadline_ts else "未知"
            backers = proj.get("backers_count", 0)
            is_prelaunch = bool(proj.get("prelaunch_activated"))
            if is_prelaunch:
                summary = proj.get("blurb", "")
            else:
                summary = (
                    f"{proj.get('blurb', '')} "
                    f"（已达目标 {percent}%，目标金额 ${goal_usd:,}，"
                    f"支持者 {backers} 人，截止 {deadline}）"
                )
            results.append({
                "id": pid,
                "source": "KS",
                "title": proj.get("name", "") + ("（预发布）" if is_prelaunch else ""),
                "summary": summary,
                "link": url,
                "percent_funded": percent,
                "goal_usd": goal_usd,
                "deadline": deadline,
                "backers_count": backers,
                "creator": proj.get("creator", {}).get("name", ""),
            })
            if len(results) >= max_count:
                break

        browser.close()
        p.stop()
    except Exception as e:
        print(f"  [错误] Kickstarter 抓取失败: {e}")
        return []

    print(f"  找到 {len(results)} 个新项目")
    return results


# ── 获取文章正文 ───────────────────────────────────────────────────────────────

def fetch_article_body(url: str) -> str:
    """抓取文章完整正文，优先从 __NEXT_DATA__ 提取，自动使用登录 Cookie"""
    cookies_data = load_cookies()
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies_data.items())
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
        "Cookie": cookie_header,
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=20, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        return f"[无法获取正文: {e}]"

    # 优先：从 Next.js hydration 数据提取完整正文（The Verge 专用）
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            responses = data["props"]["pageProps"]["hydration"]["responses"]
            paragraphs = []
            for response in responses:
                _extract_paragraphs(response, paragraphs)
            if paragraphs:
                return "\n\n".join(paragraphs)[:8000]
        except Exception:
            pass

    # 兜底：BeautifulSoup 解析 HTML
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup.select("script,style,nav,header,footer,aside"):
        tag.decompose()
    for sel in ["div.duet--article--article-body-component", "article", "main"]:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 300:
                return text[:8000]
    paragraphs = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 50]
    return "\n\n".join(paragraphs[:40])


def _extract_paragraphs(obj, result: list, depth: int = 0):
    """递归从 Next.js hydration 数据里提取所有段落文本"""
    if depth > 12:
        return
    if isinstance(obj, dict):
        # paragraphContents[].html 是正文所在字段
        if "paragraphContents" in obj:
            for pc in obj["paragraphContents"]:
                html = pc.get("html", "")
                if html:
                    text = _strip_html(html).strip()
                    if text and len(text) > 20:
                        result.append(text)
            return
        for v in obj.values():
            _extract_paragraphs(v, result, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _extract_paragraphs(item, result, depth + 1)


# ── DeepSeek 翻译 ──────────────────────────────────────────────────────────────

def _deepseek(prompt: str, max_tokens: int = 2048) -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("未设置 DEEPSEEK_API_KEY")
    resp = httpx.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens},
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


AD_KEYWORDS = {
    "deal", "deals", "sale", "sales", "discount", "off", "save", "saving",
    "promo", "coupon", "sponsored", "buy", "shop", "price", "cheap",
    "best buy", "amazon", "walmart", "prime", "spring sale", "big spring",
}

def _is_ad(article: dict) -> bool:
    text = (article.get("title", "") + " " + article.get("summary", "")).lower()
    return sum(1 for kw in AD_KEYWORDS if kw in text) >= 2


def translate_digest(articles: list[dict]) -> tuple[list[dict], str]:
    """返回 (过滤后文章列表, 简报文本)，序号与文章列表严格对应"""
    filtered = [a for a in articles if not _is_ad(a)]
    if not filtered:
        return [], "今日无重要新闻"
    items = "\n\n".join(
        f"{i+1}. [{a['source']}] {a['title']}\n{a['summary']}"
        for i, a in enumerate(filtered)
    )
    text = _deepseek(
        "以下是今天的科技新闻，请整理成中文日报。\n"
        "要求：\n"
        "- 严格按序号顺序处理，每条对应输入中的同一序号，不要跳过或合并\n"
        "- 纯新闻/发布类：一两句说清楚发生了什么\n"
        "- 观点/分析类：说清楚论点、主要论据和结论，可写3-5句\n"
        "- 序号列表，每条之间空一行，不加标题\n\n"
        + items
    )
    # 回填来源标注：在行首序号后插入 [来源]
    source_map = {i + 1: a["source"] for i, a in enumerate(filtered)}
    def _insert_source(m):
        num = int(m.group(1))
        src = source_map.get(num, "")
        return f"{num}. [{src}] " if src else m.group(0)
    text = re.sub(r"(?m)^(\d+)\.\s*", _insert_source, text)
    return filtered, text


def translate_ks_digest(articles: list[dict], offset: int = 0) -> str:
    """将 KS 项目列表翻译成中文简报，序号从 offset+1 开始"""
    if not articles:
        return ""
    items = "\n\n".join(
        f"{offset + i + 1}. {a['title']}\n{a['summary']}"
        for i, a in enumerate(articles)
    )
    text = _deepseek(
        "以下是 Kickstarter 上的最新硬件众筹项目，请用中文整理成简报。\n"
        "要求：\n"
        "- 严格按序号顺序，每条一两句，说清楚是什么产品、解决什么问题\n"
        "- 序号列表，每条之间空一行，不加标题\n\n"
        + items
    )
    # 回填正确序号（DeepSeek 可能重新从1编号）
    correct_nums = iter(range(offset + 1, offset + len(articles) + 1))
    text = re.sub(r"(?m)^(\d+)\.\s*", lambda m: f"{next(correct_nums)}. ", text)
    return text


def translate_full_article(title: str, body: str) -> str:
    return _deepseek(
        f"请用中文对以下文章做一个详细解读，要求：\n"
        f"- 开头一句话说清楚文章的核心议题\n"
        f"- 展开核心观点和主要论据，说清楚作者在论证什么、用了哪些证据\n"
        f"- 结尾一句话给出结论或作者的最终判断\n"
        f"- 语言流畅自然，像一个朋友在给你讲这篇文章\n"
        f"- 篇幅控制在500-800字，不要逐段翻译\n\n"
        f"标题：{title}\n\n{body}",
        max_tokens=2000,
    )


# ── 微信发送 ───────────────────────────────────────────────────────────────────

def _load_openclaw_account() -> dict | None:
    """自动读取 openclaw 最新授权的账号信息，包含 context_token"""
    accounts_dir = Path.home() / ".openclaw" / "openclaw-weixin" / "accounts"
    if not accounts_dir.exists():
        return None
    files = sorted(
        [f for f in accounts_dir.glob("*-im-bot.json")],
        key=lambda f: f.stat().st_mtime,
        reverse=True
    )
    for f in files:
        try:
            data = json.loads(f.read_text())
            if not (data.get("token") and data.get("userId")):
                continue
            # 读取 context_token（消息送达必需）
            ctx_file = f.parent / f.name.replace(".json", ".context-tokens.json")
            if ctx_file.exists():
                ctx = json.loads(ctx_file.read_text())
                data["context_token"] = ctx.get(data["userId"], "")
            return data
        except Exception:
            continue
    return None


def wechat_send_sync(text: str, cfg: dict) -> bool:
    """直接调用 iLink API，复现 openclaw-weixin 插件的完整请求格式（含必需 headers）"""
    import base64, struct, uuid

    account = _load_openclaw_account()
    if account:
        token = account["token"]
        user_id = account["userId"]
        api_base = account.get("baseUrl", "https://ilinkai.weixin.qq.com")
        context_token = account.get("context_token") or None
    else:
        wc = cfg["wechat"]
        if not wc.get("token") or not wc.get("user_id"):
            print("  [错误] 未配置 token 或 user_id")
            return False
        token = wc["token"]
        user_id = wc["user_id"]
        api_base = wc.get("api_base", "https://ilinkai.weixin.qq.com")
        context_token = None

    # 构造请求体（与 JS 插件完全一致）
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": user_id,
            "client_id": f"news-tracker-{uuid.uuid4().hex[:8]}",
            "message_type": 2,   # BOT
            "message_state": 2,  # FINISH
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        },
        "base_info": {"channel_version": "2.1.1"},
    }
    if context_token:
        body["msg"]["context_token"] = context_token

    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

    # X-WECHAT-UIN: random uint32 → decimal → base64
    uin = base64.b64encode(str(struct.unpack(">I", os.urandom(4))[0]).encode()).decode()

    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body_bytes)),
        "X-WECHAT-UIN": uin,
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": "131329",  # 2.1.1 encoded
        "Authorization": f"Bearer {token.strip()}",
    }

    url = api_base.rstrip("/") + "/ilink/bot/sendmessage"
    try:
        resp = httpx.post(url, content=body_bytes, headers=headers, timeout=15.0)
        print(f"  [发送] status={resp.status_code} body={resp.text[:200]}")
        if resp.status_code >= 400:
            print(f"  [错误] HTTP {resp.status_code}: {resp.text}")
            return False
        return True
    except Exception as e:
        print(f"  [错误] 发送失败: {e}")
        return False


# ── 每日推送 ───────────────────────────────────────────────────────────────────

def do_daily_push(cfg: dict):
    sent = load_sent()
    max_per_day = cfg.get("max_per_day", 8)
    rss_articles = []

    for feed_cfg in cfg.get("feeds", []):
        if not feed_cfg.get("enabled", True):
            continue
        articles = fetch_articles(feed_cfg, sent, max_per_day - len(rss_articles))
        rss_articles.extend(articles)
        if len(rss_articles) >= max_per_day:
            break

    ks_articles = []
    ks_cfg = cfg.get("kickstarter", {})
    if ks_cfg.get("enabled", False):
        ks_max = ks_cfg.get("max_per_day", 5)
        ks_articles = fetch_kickstarter_projects(sent, ks_max)

    if not rss_articles and not ks_articles:
        print(f"[{_now()}] 今天没有新文章")
        return

    kept_rss, rss_text = [], ""
    if rss_articles:
        print(f"[{_now()}] 共 {len(rss_articles)} 篇新闻，翻译中…")
        try:
            kept_rss, rss_text = translate_digest(rss_articles)
        except Exception as e:
            print(f"  [错误] 新闻翻译失败: {e}")

    ks_text = ""
    if ks_articles:
        print(f"[{_now()}] 共 {len(ks_articles)} 个众筹项目，翻译中…")
        try:
            ks_text = translate_ks_digest(ks_articles, offset=len(kept_rss))
        except Exception as e:
            print(f"  [错误] 众筹翻译失败: {e}")

    if not kept_rss and not ks_articles:
        print(f"[{_now()}] 全部为广告，跳过推送")
        return

    date_str = datetime.now(CST).strftime("%Y年%-m月%-d日")
    parts = ["我最帅气的碳基主人，这是今天为你整理的消息"]
    if rss_text:
        parts.append(f"📰 科技日报 · {date_str}\n\n{rss_text}")
    if ks_text:
        parts.append(f"🚀 最新众筹\n\n{ks_text}")
    parts.append("——\n回复数字（如「1」）可获取该篇详细解读")
    msg = "\n\n".join(parts)

    print(f"\n--- 消息预览 ---\n{msg[:300]}…\n---\n")
    all_kept = kept_rss + ks_articles
    ok = wechat_send_sync(msg, cfg)
    if ok:
        save_today(all_kept)
        for a in all_kept:
            sent.add(a["id"])
        save_sent(sent)
        print(f"[{_now()}] ✅ 推送成功")
    else:
        print(f"[{_now()}] ❌ 推送失败")


# ── 队列文件监视（openclaw 写文件 → tracker 处理翻译） ─────────────────────────

QUEUE_FILE = Path("/tmp/news_translate_queue")

async def queue_watcher(cfg: dict):
    """监视 /tmp/news_translate_queue，openclaw 写入数字后触发翻译"""
    print(f"[{_now()}] 队列监视已启动")
    while True:
        try:
            if QUEUE_FILE.exists():
                idx = QUEUE_FILE.read_text().strip()
                QUEUE_FILE.unlink()
                if idx.isdigit():
                    print(f"[{_now()}] 收到翻译请求：第 {idx} 篇")
                    await asyncio.get_event_loop().run_in_executor(
                        None, _do_translate, idx, cfg
                    )
        except Exception as e:
            print(f"[{_now()}] 队列监视错误: {e}")
        await asyncio.sleep(2)


def _do_translate(idx: str, cfg: dict):
    """同步执行翻译并发送（供队列监视和 translate 命令调用）"""
    today = load_today()
    article = today.get(idx)
    if not article:
        wechat_send_sync(f"没有找到第 {idx} 篇，当天简报共 {len(today)} 篇", cfg)
        return
    wechat_send_sync(f"正在解读《{article['title']}》，稍等…", cfg)
    print(f"[{_now()}] 解读第 {idx} 篇: {article['title']}")

    is_kickstarter = article.get("source") == "KS"

    if is_kickstarter:
        creator = article.get("creator", "")
        prompt = (
            f"请用中文对这个 Kickstarter 众筹项目做详细解读，要求：\n"
            f"- 开头一句话说清楚这是什么产品、解决什么问题\n"
            f"- 介绍核心功能和技术亮点\n"
            f"- 评估众筹进度和可信度（目标金额、支持者数量等）\n"
            f"- 给出一句你的判断：值得关注还是持观望态度，理由是什么\n"
            f"- 语言流畅自然，篇幅 300-500 字\n\n"
            f"项目名称：{article['title']}\n"
            + (f"发起人：{creator}\n" if creator else "")
            + f"众筹信息：{article.get('summary', '')}"
        )
        try:
            translated = _deepseek(prompt, max_tokens=1500)
        except Exception as e:
            wechat_send_sync(f"解读失败：{e}", cfg)
            return
    else:
        body = fetch_article_body(article["link"])
        if not body or body.startswith("[无法获取正文") or len(body) < 200:
            body = article.get("summary", "") or body
            print(f"[{_now()}]   正文抓取失败，改用摘要（{len(body)} 字符）")
        try:
            translated = translate_full_article(article["title"], body)
        except Exception as e:
            wechat_send_sync(f"解读失败：{e}", cfg)
            return

    # DeepSeek 偶尔返回过短响应，重试一次
    if len(translated) < 80:
        print(f"[{_now()}]   回复过短，重试…")
        try:
            translated = translate_full_article(article["title"], body)
        except Exception as e:
            wechat_send_sync(f"解读失败：{e}", cfg)
            return

    chunks = _split_text(translated, 2000)
    for i, chunk in enumerate(chunks):
        prefix = f"📄 {article['title']}\n\n" if i == 0 else ""
        wechat_send_sync(prefix + chunk, cfg)
    wechat_send_sync("如需继续解读其他文章，请回复对应序号。", cfg)


def _split_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks


# ── 定时调度 ───────────────────────────────────────────────────────────────────

def seconds_until_next(send_time: str) -> float:
    h, m = map(int, send_time.split(":"))
    now = datetime.now(CST)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def daily_scheduler(cfg: dict):
    send_time = cfg.get("send_time", "14:00")
    while True:
        wait = seconds_until_next(send_time)
        next_time = (datetime.now(CST) + timedelta(seconds=wait)).strftime("%m-%d %H:%M")
        print(f"[{_now()}] 下次推送：{next_time} CST")
        await asyncio.sleep(wait)
        cfg = load_config()
        await asyncio.get_event_loop().run_in_executor(None, do_daily_push, cfg)


# ── 命令 ───────────────────────────────────────────────────────────────────────

async def cmd_setup():
    cfg = load_config()
    wc = cfg["wechat"]
    if not wc.get("token"):
        print("请先填写 config.json 里的 wechat.token")
        return
    if wc.get("user_id"):
        print(f"已配置 user_id: {wc['user_id']}\n运行: python tracker.py test")
        return
    from wechat_clawbot.api.client import get_updates
    buf = ""
    print("请在微信 ClawBot 里发一条消息…")
    while True:
        resp = await get_updates(base_url=wc.get("api_base", "https://ilinkai.weixin.qq.com"), token=wc["token"], get_updates_buf=buf)
        if resp.msgs:
            user_id = resp.msgs[0].from_user_id
            cfg["wechat"]["user_id"] = user_id
            save_config(cfg)
            print(f"✅ 已保存 user_id: {user_id}")
            return
        if resp.get_updates_buf:
            buf = resp.get_updates_buf
        await asyncio.sleep(3)


async def message_listener(cfg: dict):
    """独立监听用户消息，翻译任务在后台运行，不阻塞轮询"""
    from wechat_clawbot.api.client import get_updates
    account = _load_openclaw_account()
    base_url = account.get("baseUrl", "https://ilinkai.weixin.qq.com") if account else cfg["wechat"].get("api_base", "https://ilinkai.weixin.qq.com")
    token = account["token"] if account else cfg["wechat"]["token"]
    buf = ""
    in_progress: set[str] = set()  # 正在翻译的序号，防重复
    print(f"[{_now()}] 消息监听已启动")

    loop = asyncio.get_event_loop()

    async def run_translate(idx: str):
        try:
            await loop.run_in_executor(None, _do_translate, idx, cfg)
        finally:
            in_progress.discard(idx)

    while True:
        try:
            resp = await get_updates(base_url=base_url, token=token,
                                     get_updates_buf=buf, timeout_ms=8000)
            if resp.get_updates_buf:
                buf = resp.get_updates_buf
            for msg in (resp.msgs or []):
                for item in (msg.item_list or []):
                    if item.text_item and item.text_item.text:
                        text = item.text_item.text.strip()
                        m = re.search(r"(\d+)", text)
                        if m:
                            idx = m.group(1)
                            if idx in in_progress:
                                print(f"[{_now()}] 跳过重复请求：第 {idx} 篇")
                                continue
                            in_progress.add(idx)
                            print(f"[{_now()}] 收到解读请求：第 {idx} 篇")
                            asyncio.ensure_future(run_translate(idx))
        except Exception:
            await asyncio.sleep(3)


async def cmd_run():
    cfg = load_config()
    print(f"启动中，每天 {cfg.get('send_time', '14:00')} CST 推送，回复数字触发解读\n")
    await asyncio.gather(
        daily_scheduler(cfg),
        queue_watcher(cfg),
        message_listener(cfg),
    )


async def cmd_test():
    cfg = load_config()
    print(f"[{_now()}] 测试推送…\n")
    await asyncio.get_event_loop().run_in_executor(None, do_daily_push, cfg)


def _now() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M")


async def cmd_translate(idx: str):
    """写队列文件触发解读（供 openclaw CLAUDE.md 调用）"""
    QUEUE_FILE.write_text(idx)
    print(f"[{_now()}] 已写入队列：第 {idx} 篇")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "setup":
        asyncio.run(cmd_setup())
    elif cmd == "run":
        asyncio.run(cmd_run())
    elif cmd == "test":
        asyncio.run(cmd_test())
    elif cmd == "translate" and len(sys.argv) > 2:
        asyncio.run(cmd_translate(sys.argv[2]))
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
