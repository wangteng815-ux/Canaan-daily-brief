import os
import re
import json
from datetime import datetime, timezone
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
# Relevance scoring
# =========================
STOCK_NOISE_KEYWORDS = [
    "stock", "stocks", "shares", "is the stock", "should you buy", "buy now", "strong buy",
    "price target", "rating", "analyst", "wall street", "nasdaq", "nyse", "rally", "surges",
    "plunges", "soars", "undervalued", "overvalued", "earnings preview", "dividend",
    "why this", "top ", "best ", "to buy", "to sell",
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

# 你关心的：Samsung Foundry / TSMC，5nm 以下、利用率、客户、拉货等
FOUNDRY_SIGNAL_KEYWORDS = [
    "utilization", "capacity", "capacity utilization", "loadings", "wafer starts",
    "lead time", "allocation", "tight", "shortage", "bottleneck",
    "5nm", "4nm", "3nm", "2nm", "n3", "n2",
    "tsmc", "samsung foundry", "samsung", "gfs", "intel foundry", "if",
    "coWoS".lower(), "inFO".lower(), "soic", "chiplet",
    "pull-in", "pull in", "order", "bookings", "backlog",
    "customer", "tape-out", "tape out", "design win",
]

OSAT_KEYWORDS = [
    "osat", "packaging", "advanced packaging", "2.5d", "3d", "fan-out", "fan out",
    "substrate", "abf", "flip chip", "bumping", "wlcsp", "test", "ate",
    "coWoS".lower(), "hybrid bonding", "tsv",
]

EDA_KEYWORDS = [
    "eda", "synopsys", "cadence", "siemens eda", "mentor",
    "export control", "export controls", "bis", "entity list", "sanction", "compliance",
    "license", "restriction", "regulation",
]

MINING_KEYWORDS = [
    "canaan", "bitmain", "microbt", "whatsminer", "antminer",
    "bitdeer", "marathon", "riot", "cleanSpark".lower(), "hive", "hut 8", "core scientific",
    "hashrate", "difficulty", "halving", "mining", "miner",
    "immersion", "hosting", "power", "ppa", "tariff",
]


def relevance_score(section_name: str, text_l: str, domain: str) -> int:
    score = 0

    # 先扣股票噪音
    if domain in STOCK_NOISE_DOMAINS:
        score -= 10
    if hit_keywords(text_l, STOCK_NOISE_KEYWORDS):
        score -= 6

    sec = section_name.lower()

    if "foundry" in sec and "产业链" not in section_name:
        if hit_keywords(text_l, FOUNDRY_SIGNAL_KEYWORDS):
            score += 10
        # 你明确要 5nm 以下：有这些给高分
        if any(k in text_l for k in ["3nm", "2nm", "4nm", "5nm", "n2", "n3"]):
            score += 8
        if any(k in text_l for k in ["utilization", "capacity", "lead time", "allocation", "pull-in", "order"]):
            score += 6
        if any(k in text_l for k in ["tsmc", "samsung foundry", "samsung"]):
            score += 5

    elif "产业链" in section_name:
        # 产业链：设备/材料/上游
        if any(k in text_l for k in ["asml", "lam", "applied materials", "klaus", "tokyo electron", "tel", "kokusai", "screen", "jsr", "sumco", "abf", "substrate"]):
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


# =========================
# Main
# =========================
def main():
    with open("sources.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    tz_name = cfg.get("timezone", "Asia/Tokyo")
    local_tz = tz.gettz(tz_name)

    # ---- filters from sources.yaml
    flt = cfg.get("filter", {}) or {}
    blocked_domains = set([str(d).lower().strip().replace("www.", "") for d in flt.get("blocked_domains", []) if d])
    blocked_keywords = [str(k).lower() for k in flt.get("blocked_keywords", []) if k]
    paywall_keywords = [str(k).lower() for k in flt.get("paywall_keywords", []) if k]

    # ---- display config
    max_per = int((cfg.get("display", {}) or {}).get("max_items_per_section", 10))

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

        for e in (parsed.entries or [])[:80]:
            dt = pick_dt(e) or datetime.now(timezone.utc)
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

            # ---- extra: drop strong stock-only domains & phrases (but won't kill all content)
            if real_domain in STOCK_NOISE_DOMAINS:
                # 仅当标题/摘要也像股票稿时才丢，避免误杀
                if hit_keywords(text_l, STOCK_NOISE_KEYWORDS):
                    continue
            else:
                # 不是股票域，但标题很像股票稿，也丢
                if hit_keywords(text_l, STOCK_NOISE_KEYWORDS) and not any(k in text_l for k in ["wafer", "foundry", "tsmc", "samsung", "3nm", "2nm", "4nm", "5nm"]):
                    continue

            items.append({
                "title": clean(raw_title) or "(no title)",
                "link": raw_link,
                "source": name,          # your feed name
                "real_domain": real_domain,  # publisher domain if available
                "tags": tags,
                "dt": dt,
                "published": local_dt.strftime("%Y-%m-%d %H:%M"),
                "summary": clean(summary),
            })

    # ---- sort newest
    items.sort(key=lambda x: x["dt"], reverse=True)

    # ---- Top10 newest overall
    top10 = items[:10]

    # ---- Need-to-check (simple signals)
    title_blob = " ".join([(it.get("title", "") + " " + it.get("summary", "")).lower() for it in items[:80]])
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

    # ---- sections: pick TOP relevant per section (not just newest)
    sections = []
    for sec_name, sec_tags in SECTION_RULES:
        pool = []
        for it in items:
            it_tags = set(it.get("tags", []) or [])
            if not sec_tags.intersection(it_tags):
                continue
            text_l = (it.get("title", "") + " " + it.get("summary", "")).lower()
            domain = (it.get("real_domain") or safe_domain(it.get("link", ""))).lower()
            score = relevance_score(sec_name, text_l, domain)
            pool.append((score, it))

        # sort by score first, then by time
        pool.sort(key=lambda x: (x[0], x[1].get("dt")), reverse=True)
        chosen = [it for score, it in pool[:max_per]]
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
