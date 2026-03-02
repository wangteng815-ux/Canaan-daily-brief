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

    # ✅ 拆成两个同级：EDA + 合规（你要的）
    ("EDA（工具/生态/合作/版本）", {"eda"}),
    ("出口管制/黑名单（芯片设计合规）", {"compliance", "export", "sanctions", "entitylist"}),

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
        headers={"User-Agent": "Mozilla/5.0 (compatible; daily-brief/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# =========================
# Noise filters (stock junk)
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


# =========================
# Keyword sets (EDA + Compliance are separate)
# =========================
FOUNDRY_KEYWORDS = [
    "utilization", "capacity", "capacity utilization", "loadings", "wafer starts",
    "lead time", "allocation", "tight", "shortage", "bottleneck",
    "5nm", "4nm", "3nm", "2nm", "n3", "n2",
    "tsmc", "samsung foundry", "samsung", "gfs", "intel foundry",
    "cowos", "info", "soic", "chiplet",
    "pull-in", "pull in", "order", "bookings", "backlog",
    "customer", "tape-out", "tape out", "design win",
]

SUPPLYCHAIN_KEYWORDS = [
    "asml", "euv", "duv", "pellicle", "mask", "photoresist",
    "applied materials", "lam research", "tokyo electron", "tel", "kokusai", "screen", "kla",
    "sumco", "shin-etsu", "wafer", "substrate", "abf", "ajinomoto",
    "chemical", "gas", "slurry", "cmp",
]

OSAT_KEYWORDS = [
    "osat", "packaging", "advanced packaging", "2.5d", "3d", "fan-out", "fan out",
    "substrate", "abf", "flip chip", "bumping", "wlcsp", "test", "ate",
    "cowos", "hybrid bonding", "tsv", "hbm",
    "ase", "amkor", "spil", "jcet",
]

EDA_KEYWORDS = [
    "eda", "synopsys", "cadence", "siemens eda", "mentor",
    "pdk", "ip", "verification", "formal", "simulation", "emulation",
    "static timing", "sta", "signoff", "physical design", "place and route",
    "lvs", "drc", "dfm", "calibre",
]

COMPLIANCE_KEYWORDS = [
    # 机构/法规/清单
    "bis", "bureau of industry and security", "ear",
    "entity list", "meu", "uvl", "fdpr", "foreign direct product rule",
    "license requirement", "license exception", "reexport", "end user", "end-use", "end use",
    "ofac", "treasury", "sdn", "sanctions",

    # 执法/处罚/指引
    "compliance", "enforcement", "penalty", "settlement", "fine", "plea", "guidance", "faq",

    # 半导体相关限制（设计公司关心）
    "advanced computing", "ai chip", "gpu", "npu", "hbm", "chiplet",
    "3nm", "2nm", "5nm", "7nm",
    "eda", "ip", "pdk",
]

MINING_KEYWORDS = [
    "canaan", "bitmain", "microbt", "whatsminer", "antminer",
    "bitdeer", "marathon", "riot", "cleanspark", "hive", "hut 8", "core scientific",
    "hashrate", "difficulty", "halving", "mining", "miner",
    "immersion", "hosting", "power", "ppa", "tariff",
]


# =========================
# Scoring: relevance + recency (within lookback window)
# =========================
def recency_score(dt_utc: datetime, now_utc: datetime, lookback_days: int) -> int:
    """
    越新分越高；超出窗口直接 -999（丢）。
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


def base_relevance_score(section_name: str, text_l: str, domain: str) -> int:
    score = 0

    # 股票垃圾先扣
    if domain in STOCK_NOISE_DOMAINS:
        score -= 10
    if hit_keywords(text_l, STOCK_NOISE_KEYWORDS):
        score -= 6

    # 通用产业词轻微加分
    if any(k in text_l for k in ["guidance", "capex", "backlog", "shipment", "fab", "node", "yield", "ramp", "allocation"]):
        score += 2

    # 分板块加权
    if "foundry" in section_name and "产业链" not in section_name:
        if hit_keywords(text_l, FOUNDRY_KEYWORDS):
            score += 10
        if any(k in text_l for k in ["3nm", "2nm", "4nm", "5nm", "n2", "n3"]):
            score += 8
        if any(k in text_l for k in ["utilization", "capacity", "lead time", "allocation", "pull-in", "order", "customer"]):
            score += 6
        if any(k in text_l for k in ["tsmc", "samsung foundry", "samsung"]):
            score += 5

    elif "产业链" in section_name:
        if hit_keywords(text_l, SUPPLYCHAIN_KEYWORDS):
            score += 9

    elif "OSAT" in section_name or "Packaging" in section_name:
        if hit_keywords(text_l, OSAT_KEYWORDS):
            score += 9

    elif "EDA" in section_name and "合规" not in section_name:
        if hit_keywords(text_l, EDA_KEYWORDS):
            score += 9

    elif "出口管制" in section_name or "合规" in section_name or "黑名单" in section_name:
        # ✅ 合规更关键：分数更高
        if hit_keywords(text_l, COMPLIANCE_KEYWORDS):
            score += 14
        # 强信号再加
        if any(k in text_l for k in ["entity list", "sdn", "meu", "uvl", "fdpr", "license requirement"]):
            score += 8

    elif "矿机" in section_name:
        if hit_keywords(text_l, MINING_KEYWORDS):
            score += 9

    return score


def total_score(section_name: str, it: dict, now_utc: datetime, lookback_days: int) -> int:
    dt = it.get("dt") or now_utc
    r = recency_score(dt, now_utc, lookback_days)
    if r < -100:
        return -999
    text_l = (it.get("title", "") + " " + it.get("summary", "")).lower()
    domain = (it.get("real_domain") or safe_domain(it.get("link", ""))).lower()
    rel = base_relevance_score(section_name, text_l, domain)
    return rel + r


# =========================
# Main
# =========================
def main():
    with open("sources.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    tz_name = cfg.get("timezone", "Asia/Tokyo")
    local_tz = tz.gettz(tz_name)

    # filters
    flt = cfg.get("filter", {}) or {}
    blocked_domains = set([str(d).lower().strip().replace("www.", "") for d in flt.get("blocked_domains", []) if d])
    blocked_keywords = [str(k).lower() for k in flt.get("blocked_keywords", []) if k]
    paywall_keywords = [str(k).lower() for k in flt.get("paywall_keywords", []) if k]

    # display
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
                continue  # ✅ 只要 1 周内

            local_dt = dt.astimezone(local_tz)

            raw_title = getattr(e, "title", "") or ""
            raw_link = getattr(e, "link", "") or ""
            summary = getattr(e, "summary", "") or ""

            # ✅ GNews：优先取真实发布源域名
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

            # hard domain block
            if real_domain and real_domain in blocked_domains:
                continue

            # paywall / blocked keywords
            text_l = (raw_title + " " + summary).lower()
            if hit_keywords(text_l, paywall_keywords):
                continue
            if hit_keywords(text_l, blocked_keywords):
                continue

            # stock noise: 强噪音直接丢
            if real_domain in STOCK_NOISE_DOMAINS and hit_keywords(text_l, STOCK_NOISE_KEYWORDS):
                continue

            items.append({
                "title": clean(raw_title) or "(no title)",
                "link": raw_link,
                "source": name,              # feed name
                "real_domain": real_domain,  # publisher domain if available
                "tags": tags,
                "dt": dt,
                "published": local_dt.strftime("%Y-%m-%d %H:%M"),
                "summary": clean(summary),
            })

    # newest overall (still within 1 week)
    items.sort(key=lambda x: x["dt"], reverse=True)

    # =========================
    # Top10: 先按分数选，再按时间展示（最新在前）
    # =========================
    scored = []
    for it in items:
        # 全局：取它在所有 section 里的最高分
        best = None
        for sec_name, _ in SECTION_RULES:
            s = total_score(sec_name, it, now_utc, lookback_days)
            if best is None or s > best:
                best = s
        best = best if best is not None else -999
        if best < -100:
            continue
        scored.append((best, it["dt"], it))

    # 先按分数挑 10
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top10_pool = [it for _, __, it in scored[:10]]

    # 展示按时间（最新在前）
    top10_pool.sort(key=lambda x: x["dt"], reverse=True)
    top10 = top10_pool

    # =========================
    # Need-to-check（更偏采购/合规）
    # =========================
    blob = " ".join([(it.get("title", "") + " " + it.get("summary", "")).lower() for it in items[:140]])
    need_to_check = []

    checks = [
        ("utilization", "【代工】出现利用率信号：重点看 TSMC / Samsung Foundry 的 5nm-2nm 紧张程度与交期。"),
        ("lead time", "【代工/封装】出现交期信号：记录节点/封装类型/客户范围，评估是否要提前锁量。"),
        ("allocation", "【代工/封装】出现配额信号：可能被 AI/先进封装挤占，建议核对关键工艺与封装产能。"),
        ("surcharge", "【价格】出现附加费/涨价：准备议价与备选供应链。"),
        ("export control", "【合规】出现出口管制：立即评估 EDA/IP/代工/封装的合规与替代方案。"),
        ("entity list", "【合规】出现 Entity List：检查是否涉及客户/供应商/合作方。"),
        ("sdn", "【合规】出现 SDN/制裁：检查收款、物流、服务交付与许可风险。"),
        ("cowos", "【封装】出现 CoWoS：关注产能、良率与排产窗口。"),
        ("3nm", "【先进制程】出现 3nm：关注客户项目、良率、爬坡与交期。"),
        ("2nm", "【先进制程】出现 2nm：关注试产、设备到位、客户 tape-out 节奏。"),
    ]
    for k, msg in checks:
        if k in blob:
            need_to_check.append(msg)

    if not need_to_check:
        need_to_check = [
            "每天看三件事：① 利用率/交期（紧不紧）② wafer/封装价格（涨不涨）③ 大客户项目与拉货（快不快）",
            "合规优先级：Entity List / SDN / FDPR / 许可要求 任一出现 → 当天做二次核验（官方优先）。",
        ]

    # =========================
    # Sections: 先按分数挑 max_per，再按时间展示（最新在前）
    # =========================
    sections = []
    for sec_name, sec_tags in SECTION_RULES:
        pool = []
        for it in items:
            it_tags = set(it.get("tags", []) or [])
            if not sec_tags.intersection(it_tags):
                continue

            s = total_score(sec_name, it, now_utc, lookback_days)
            if s < -100:
                continue
            pool.append((s, it["dt"], it))

        # 先分数挑选
        pool.sort(key=lambda x: (x[0], x[1]), reverse=True)
        chosen = [it for _, __, it in pool[:max_per]]

        # 展示：按时间最新在前（你要的）
        chosen.sort(key=lambda x: x["dt"], reverse=True)

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
