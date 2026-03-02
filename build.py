import os, re
from datetime import datetime, timezone
from dateutil import tz
import yaml
import feedparser
import urllib.request
from jinja2 import Environment, FileSystemLoader, select_autoescape
from urllib.parse import urlparse

SECTION_RULES = [
    ("Foundry（产能 / 价格 / 客户拉货 / 项目）", {"foundry", "price", "utilization", "capacity", "customers", "projects"}),
    ("Foundry 产业链（设备/材料/上下游）", {"supplychain"}),
    ("OSAT / Advanced Packaging", {"osat", "packaging"}),
    ("EDA（含合规/出口管制）", {"eda", "compliance"}),
    ("矿机 / 矿厂（Bitmain/MicroBT/Canaan/Bitdeer等）", {"miner", "oem", "farms", "company"}),
]

# ===== 股票站强力过滤 =====
STOCK_NOISE_DOMAINS = {
    "aol.com", "zacks.com", "fool.com", "seekingalpha.com",
    "marketwatch.com", "benzinga.com", "adhocnews.de", "openpr.com"
}

STOCK_NOISE_KEYWORDS = {
    "stock", "stocks", "shares", "price target", "should you buy",
    "strong buy", "rating", "analyst", "nasdaq", "nyse",
    "wall street", "rally", "soars", "plunges"
}

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def pick_dt(entry):
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc)

def get_domain(link: str) -> str:
    try:
        return (urlparse(link).netloc or "").lower().replace("www.", "")
    except Exception:
        return ""

def main():
    with open("sources.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    tz_name = cfg.get("timezone", "Asia/Tokyo")
    local_tz = tz.gettz(tz_name)

    # ===== 读取 filter（提前定义，避免变量未定义）=====
    flt = cfg.get("filter", {})
    blocked_domains = set(d.lower().replace("www.", "") for d in flt.get("blocked_domains", []))
    blocked_keywords = [k.lower() for k in flt.get("blocked_keywords", [])]
    paywall_keywords = [k.lower() for k in flt.get("paywall_keywords", [])]

    items, sources = [], []

    for feed in cfg.get("feeds", []):
        name, url = feed["name"], feed["url"]
        tags = feed.get("tags", [])
        sources.append(name)

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = resp.read()
            parsed = feedparser.parse(data)
        except Exception as e:
            print(f"Feed failed: {url} -> {e}")
            parsed = feedparser.parse(b"")

        for e in parsed.entries[:60]:

            raw_title = getattr(e, "title", "") or ""
            raw_link = getattr(e, "link", "") or ""
            summary = getattr(e, "summary", "") or ""

            title_l = raw_title.lower()
            text_blob = (raw_title + " " + summary).lower()

            # ===== 获取真实域名 =====
            src_href = ""
            try:
                src = getattr(e, "source", None)
                if isinstance(src, dict):
                    src_href = src.get("href", "") or ""
                else:
                    src_href = getattr(src, "href", "") or ""
            except:
                pass

            domain = get_domain(src_href or raw_link)

            # ===== 过滤顺序 =====
            if domain in blocked_domains:
                continue

            if domain in STOCK_NOISE_DOMAINS:
                continue

            if any(k in text_blob for k in blocked_keywords):
                continue

            if any(k in text_blob for k in paywall_keywords):
                continue

            if any(k in title_l for k in STOCK_NOISE_KEYWORDS):
                continue

            dt = pick_dt(e) or datetime.now(timezone.utc)
            local_dt = dt.astimezone(local_tz)

            items.append({
                "title": clean(raw_title) or "(no title)",
                "link": raw_link,
                "source": name,
                "tags": tags,
                "dt": dt,
                "published": local_dt.strftime("%Y-%m-%d %H:%M"),
            })

    # ===== 排序 =====
    items.sort(key=lambda x: x["dt"], reverse=True)

    top10 = items[:10]

    # ===== Need-to-check =====
    title_blob = " ".join([it["title"].lower() for it in items[:50]])
    need_to_check = []

    patterns = [
        ("utilization", "出现利用率信号：判断是否紧张或去库。"),
        ("price", "出现价格信号：记录涨幅与节点范围。"),
        ("pull-in", "出现拉货信号：核对大客户节奏。"),
        ("allocation", "出现配额信号：关注AI是否挤占产能。"),
        ("export control", "出现出口管制信号：评估合规风险。"),
        ("cowos", "出现先进封装信号：关注CoWoS产能。"),
    ]

    for k, msg in patterns:
        if k in title_blob:
            need_to_check.append(msg)

    if not need_to_check:
        need_to_check = [
            "每天看三件事：① 利用率 ② 价格 ③ 拉货节奏",
            "出现配额/涨价/出口限制 → 当天做二次确认。"
        ]

    # ===== Section 分类 =====
    sections = []
    for sec_name, sec_tags in SECTION_RULES:
        sec_items = [it for it in items if sec_tags.intersection(set(it["tags"]))][:40]
        sections.append({"name": sec_name, "items": sec_items})

    env = Environment(loader=FileSystemLoader("templates"),
                      autoescape=select_autoescape(["html"]))
    tpl = env.get_template("index.html")

    generated_at = datetime.now(local_tz).strftime("%Y-%m-%d %H:%M")

    html = tpl.render(
        title="Canaan Procurement & Mining Daily Intel",
        generated_at=generated_at,
        timezone=tz_name,
        top10=top10,
        need_to_check=need_to_check,
        sections=sections,
        sources=sources,
    )

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    main()
