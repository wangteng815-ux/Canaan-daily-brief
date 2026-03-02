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
        # ====== 相关性打分：每个板块挑 10 条最相关 ======
    def relevance_score(it: dict, sec_name: str) -> int:
        """
        分数越高越相关。规则很简单：命中关键词加分 + 越新稍微加分
        """
        title = (it.get("title") or "").lower()
        # 如果你后面把 summary 存进 items，也可以一起用
        text = title

        # 通用“产业相关”加分（压掉纯股评）
        industry_boost = [
            "utilization", "capacity", "wafer", "lead time", "leadtime", "allocation",
            "2nm", "3nm", "4nm", "5nm", "gate-all-around", "gaa", "n2", "a14",
            "yield", "ramp", "hvm", "risk production",
            "cowos", "inFO".lower(), "fo-wlp".lower(), "chiplet", "advanced packaging",
            "export control", "bis", "sanction", "entity list",
            "siemens", "synopsys", "cadence", "eda"
        ]

        score = 0
        for k in industry_boost:
            if k in text:
                score += 3

        # 板块关键词加分（你关心：TSMC/Samsung foundry、5nm以下、利用率、客户）
        sec_kw = {
            "Foundry（产能 / 价格 / 客户拉货 / 项目）": [
                "tsmc", "taiwan semiconductor", "samsung foundry", "samsung",
                "utilization", "capacity", "fab", "ramp", "yield", "wafer",
                "n2", "2nm", "3nm", "4nm", "5nm", "gaa", "gate-all-around",
                "customer", "order", "pull-in", "pull in", "demand", "allocation",
                "hpc", "ai", "nvidia", "apple", "qualcomm", "amd"
            ],
            "Foundry 产业链（设备/材料/上下游）": [
                "asml", "applied materials", "lam research", "tokyo electron", "tel",
                "kokusai", "screen", "kLA".lower(),
                "photoresist", "euv", "duv", "mask", "pellicle",
                "slurry", "gas", "chemical", "substrate", "silicon wafer"
            ],
            "OSAT / Advanced Packaging": [
                "tsmc", "cowos", "inFO".lower(), "soic", "hybrid bonding",
                "amkor", "ase", "spil", "jcet", "tfme".lower(), "hbm",
                "substrate", "abf", "advanced packaging", "test", "burn-in"
            ],
            "EDA（含合规/出口管制）": [
                "synopsys", "cadence", "siemens eda", "mentor",
                "export control", "bis", "entity list", "sanction", "compliance"
            ],
            "矿机 / 矿厂（Bitmain/MicroBT/Canaan/Bitdeer等）": [
                "canaan", "bitmain", "microbt", "whatsminer", "bitdeer",
                "hashrate", "difficulty", "block reward", "halving",
                "miner", "asic", "t21", "s21", "m60".lower(),
                "public miner", "marathon", "riot", "cleanSpark".lower(), "hut 8".lower()
            ],
        }.get(sec_name, [])

        for k in sec_kw:
            if k in text:
                score += 10

        # 越新稍微加分（避免很老的“高分词”霸榜）
        dt = it.get("dt")
        if isinstance(dt, datetime):
            age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
            if age_hours < 24:
                score += 6
            elif age_hours < 72:
                score += 3
            elif age_hours < 168:
                score += 1

        return score

    sections = []
    for sec_name, sec_tags in SECTION_RULES:
        # 先按 tag 进这个板块
        pool = [it for it in items if sec_tags.intersection(set(it.get("tags", [])))]

        # 再按“相关性”排序（高分在前），同分按时间新在前
        pool.sort(
            key=lambda it: (relevance_score(it, sec_name), it.get("dt")),
            reverse=True
        )

        # 每个板块只取前 max_per 条（你 sources.yaml 里已经是 10）
        sections.append({"name": sec_name, "items": pool[:max_per]})
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
