import os
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import urllib.request

import yaml
import feedparser
from dateutil import tz
from jinja2 import Environment, FileSystemLoader, select_autoescape


# =========================
# Sections (tags -> section)
# =========================
SECTION_RULES = [
    ("Foundry（产能 / 价格 / 客户拉货 / 项目）", {"foundry", "price", "utilization", "capacity", "customers", "projects"}),
    ("Foundry 产业链（设备/材料/上下游）", {"supplychain"}),
    ("OSAT / Advanced Packaging", {"osat", "packaging"}),
    ("EDA（含合规/出口管制）", {"eda", "compliance"}),
    ("矿机 / 矿厂（Bitmain/MicroBT/Canaan/Bitdeer等）", {"miner", "oem", "farms", "company"}),
]


# =========================
# Utilities
# =========================
def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def pick_dt(entry):
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc)


def safe_domain(url: str) -> str:
    try:
        d = (urlparse(url).netloc or "").lower()
        return d.replace("www.", "")
    except Exception:
        return ""


def hit_keywords(text: str, keywords) -> bool:
    t = (text or "").lower()
    for k in keywords:
        if not k:
            continue
        if k in t:
            return True
    return False


def fetch_feed(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; daily-brief/1.0; +GitHub · Change is constant. GitHub keeps you ahead.)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# =========================
# Noise filters & relevance
# =========================
STOCK_NOISE_KEYWORDS = [
    "stock", "stocks", "shares", "is the stock", "should you buy", "buy now", "strong buy",
    "price target", "rating", "analyst", "wall street", "nasdaq", "nyse", "rally", "surges",
    "plunges", "soars", "undervalued", "overvalued", "earnings preview", "dividend",
    "top ", "best ", "to buy", "to sell",
]

STOCK_NOISE_DOMAINS = {
    "aol.com",
    "finance.yahoo.com",
    "fool.com",
    "seekingalpha.com",
    "marketwatch.com",
    "benzinga.com",
    "zacks.com",
    "investorplace.com",
    "tipranks.com",
    "barrons.com",
    "adhocnews.de",
    "openpr.com",
}

FOUNDRY_SIGNAL_KEYWORDS = [
    "utilization", "capacity", "capacity utilization", "loadings", "wafer starts",
    "lead time", "allocation", "tight", "shortage", "bottleneck",
    "5nm", "4nm", "3nm", "2nm", "n3", "n2",
    "tsmc", "samsung foundry", "samsung", "gfs", "intel foundry",
    "cowos", "info", "soic", "chiplet",
    "pull-in", "pull in", "order", "bookings", "backlog",
    "customer", "tape-out", "tape out", "design win",
]

OSAT_KEYWORDS = [
    "osat", "packaging", "advanced packaging", "2.5d", "3d", "fan-out", "fan out",
    "substrate", "abf", "flip chip", "bumping", "wlcsp", "test", "ate",
    "cowos", "hybrid bonding", "tsv",
]

EDA_KEYWORDS = [
    "eda", "synopsys", "cadence", "siemens eda", "mentor",
    "export control", "export controls", "bis", "entity list", "sanction", "compliance",
    "license", "restriction", "regulation",
]

MINING_KEYWORDS = [
    "canaan", "bitmain", "microbt", "whatsminer", "antminer",
    "bitdeer", "marathon", "riot", "cleanspark", "hive", "hut 8", "core scientific",
    "hashrate", "difficulty", "halving", "mining", "miner",
    "immersion", "hosting", "power", "ppa", "tariff",
]


def base_relevance_score(section_name: str, text_l: str, domain: str) -> int:
    score = 0

    # 股票噪音先扣
    if domain in STOCK_NOISE_DOMAINS:
        score -= 10
    if hit_keywords(text_l, STOCK_NOISE_KEYWORDS):
        score -= 6

    sec = section_name.lower()

    if "foundry" in sec and "产业链" not in section_name:
        if hit_keywords(text_l, FOUNDRY_SIGNAL_KEYWORDS):
            score += 10
        if any(k in text_l for k in ["3nm", "2nm", "4nm", "5nm", "n2", "n3"]):
            score += 8
        if any(k in text_l for k in ["utilization", "capacity", "lead time", "allocation", "pull-in", "order"]):
            score += 6
        if any(k in text_l for k in ["tsmc", "samsung foundry", "samsung"]):
            score += 5

    elif "产业链" in section_name:
        if any(k in text_l for k in ["asml", "lam", "applied materials", "tokyo electron", "tel", "kokusai", "screen",
                                     "jsr", "sumco", "substrate", "abf"]):
            score += 7
        if any(k in text_l for k in ["export", "restriction", "ban", "controls", "license"]):
            score += 3

    elif "osat" in sec or "packaging" in sec:
        if hit_keywords(text_l, OSAT_KEYWORDS):
            score += 9

    elif "eda" in sec:
        if hit_keywords(text_l, EDA_KEYWORDS):
            score += 9

    elif "矿机" in section_name or "miner" in sec:
        if hit_keywords(text_l, MINING_KEYWORDS):
            score += 9

    # 温和奖励“更像产业情报”的词
    if any(k in text_l for k in ["guidance", "capex", "backlog", "shipment", "tool", "fab", "node", "yield", "ramp"]):
        score += 2

    return score


def recency_score(dt_utc: datetime, now_utc: datetime, lookback_days: int) -> int:
    """
    只在 lookback 窗口内评分；越新分越高。
    - 0~24h: +12
    - 1~2d: +10
    - 2~3d: +8
    - 3~5d: +6
    - 5~7d: +4
    - 超出窗口: -999 (直接丢掉)
    """
    age = now_utc - dt_utc
    if age < timedelta(0):
        age = timedelta(0)

    if age > timedelta(days=lookback_days):
        return -999

    hours = age.total_seconds() / 3600.0
    if hours <= 24:
        return 12
    if hours <= 48:
        return 10
    if hours <= 72:
        return 8
    if hours <= 120:
        return 6
    return 4


# =========================
# Main
# =========================
def main():
    with open("sources.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    tz_name = cfg.get("timezone", "Asia/Tokyo")
    local_tz = tz.gettz(tz_name)

    flt = cfg.get("filter", {}) or {}
    blocked_domains = set([str(d).lower().strip().replace("www.", "") for d in flt.get("blocked_domains", []) if d])
    blocked_keywords = [str(k).lower() for k in flt.get("blocked_keywords", []) if k]
    paywall_keywords = [str(k).lower() for k in flt.get("paywall_keywords", []) if k]

    display = cfg.get("display", {}) or {}
    max_per = int(display.get("max_items_per_section", 10))
    lookback_days = int(display.get("lookback_days", 7))

    now_utc = datetime.now(timezone.utc)
    cutoff_utc = now_utc - timedelta(days=lookback_days)

    items = []
    sources = []

    # ---- read feeds
    for feed in cfg.get("feeds", []) or []:
        name = feed.get("name")
        url = feed.get("url")
        tags = feed.get("tags", []) or []
        if not name or not url:
            continue

        sources.append(name)

        try:
            data = fetch_feed(url, timeout=12)
            parsed = feedparser.parse(data)
        except Exception as e:
            print(f"Feed failed: {url} -> {e}")
            parsed = feedparser.parse(b"")

        for e in (parsed.entries or [])[:120]:
            dt = pick_dt(e) or now_utc
            if dt < cutoff_utc:
                # 直接丢掉：你要“1周内”
                continue

            local_dt = dt.astimezone(local_tz)

            raw_title = getattr(e, "title", "") or ""
            raw_link = getattr(e, "link", "") or ""
            summary = getattr(e, "summary", "") or ""

            # ---- IMPORTANT: for GNews, prefer entry.source.href as the "real publisher"
            src_href = ""
            try:
                src = getattr(e, "source", None)
                if isinstance(src, dict):
                    src_href = src.get("href", "") or ""
                else:
                    src_href = getattr(src, "href", "") or ""
            except Exception:
                src_href = ""

            real_domain = safe_domain(src_href or raw_link)

            # ---- hard domain block
            if real_domain and real_domain in blocked_domains:
                continue

            # ---- paywall / blocked keywords
            text_l = (raw_title + " " + summary).lower()
            if hit_keywords(text_l, paywall_keywords):
                continue
            if hit_keywords(text_l, blocked_keywords):
                continue

            # ---- stock noise: 强噪音直接丢
            if real_domain in STOCK_NOISE_DOMAINS and hit_keywords(text_l, STOCK_NOISE_KEYWORDS):
                continue

            # ---- record
            items.append({
                "title": clean(raw_title) or "(no title)",
                "link": raw_link,
                "source": name,
                "real_domain": real_domain,
                "tags": tags,
                "dt": dt,
                "published": local_dt.strftime("%Y-%m-%d %H:%M"),
                "summary": clean(summary),
            })

    # ---- sort newest overall
    items.sort(key=lambda x: x["dt"], reverse=True)

    # ---- Top10 newest overall (仍然只会来自 7 天窗口，因为旧的已丢)
    top10 = items[:10]

    # ---- Need-to-check
    title_blob = " ".join([(it.get("title", "") + " " + it.get("summary", "")).lower() for it in items[:120]])
    need_to_check = []
    signals = [
        ("utilization", "出现“utilization/产能利用率”信号：重点看 TSMC / Samsung Foundry 在 5nm-2nm 的紧张程度。"),
        ("lead time", "出现“lead time/交期”信号：记录节点/封装类型/客户范围，评估是否要提前锁量。"),
        ("allocation", "出现“allocation/配额”信号：可能被 AI/先进封装挤占，建议核对关键工艺与封装产能。"),
        ("surcharge", "出现“surcharge/附加费/涨价”信号：准备议价与备选供应链。"),
        ("export control", "出现“出口管制/制裁”信号：立即评估 EDA/设备/材料合规与替代方案。"),
        ("cowos", "出现“CoWoS/先进封装”信号：关注封装排产窗口、良率与产能扩张节奏。"),
        ("3nm", "出现“3nm”相关：关注客户项目/良率/产能爬坡与交期。"),
        ("2nm", "出现“2nm”相关：关注试产/设备到位/客户 tape-out 节奏。"),
    ]
    for k, msg in signals:
        if k in title_blob:
            need_to_check.append(msg)
    if not need_to_check:
        need_to_check = [
            "每天看三件事：① 利用率/交期（紧不紧）② wafer/封装价格（涨不涨）③ 大客户项目与拉货（快不快）",
            "如果出现：配额/拉货/涨价/出口管制 任一关键词 → 当天拉相关源做二次确认（官方/研究机构优先）。",
        ]

    # ---- sections: within 1 week, rank by (relevance + recency)
    sections = []
    for sec_name, sec_tags in SECTION_RULES:
        pool = []
        for it in items:
            it_tags = set(it.get("tags", []) or [])
            if not sec_tags.intersection(it_tags):
                continue

            dt = it.get("dt") or now_utc
            rcy = recency_score(dt, now_utc, lookback_days)
            if rcy < -100:
                continue  # 超出窗口（理论上前面已经丢）

            text_l = (it.get("title", "") + " " + it.get("summary", "")).lower()
            domain = (it.get("real_domain") or safe_domain(it.get("link", ""))).lower()

            rel = base_relevance_score(sec_name, text_l, domain)
            total = rel + rcy

            pool.append((total, rel, rcy, dt, it))

        # 排序：总分 > 时间（防止同分时老的靠前）
        pool.sort(key=lambda x: (x[0], x[3]), reverse=True)
        chosen = [it for total, rel, rcy, dt, it in pool[:max_per]]

        sections.append({"name": sec_name, "items": chosen})

    # ---- render
    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(["html"]),
    )
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
