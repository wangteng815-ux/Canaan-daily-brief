import os, re, json
from datetime import datetime, timezone
from dateutil import tz
import yaml
import feedparser
from jinja2 import Environment, FileSystemLoader, select_autoescape

SECTION_RULES = [
    ("Foundry（产能 / 价格 / 客户拉货 / 项目）", {"foundry", "price", "utilization", "capacity", "customers", "projects"}),
    ("Foundry 产业链（设备/材料/上下游）", {"supplychain"}),
    ("OSAT / Advanced Packaging", {"osat", "packaging"}),
    ("EDA（含合规/出口管制）", {"eda", "compliance"}),
    ("矿机 / 矿厂（Bitmain/MicroBT/Canaan/Bitdeer等）", {"miner", "oem", "farms", "company"}),
]

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def pick_dt(entry):
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc)

def main():
    with open("sources.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    tz_name = cfg.get("timezone", "Asia/Tokyo")
    local_tz = tz.gettz(tz_name)

    items, sources = [], []

    for feed in cfg.get("feeds", []):
        name, url = feed["name"], feed["url"]
        tags = feed.get("tags", [])
        sources.append(name)

        parsed = feedparser.parse(url)
        for e in parsed.entries[:60]:
            dt = pick_dt(e) or datetime.now(timezone.utc)
            local_dt = dt.astimezone(local_tz)

            items.append({
                "title": clean(getattr(e, "title", "")) or "(no title)",
                "link": getattr(e, "link", ""),
                "source": name,
                "tags": tags,
                "dt": dt,
                "published": local_dt.strftime("%Y-%m-%d %H:%M"),
            })

    items.sort(key=lambda x: x["dt"], reverse=True)

    # Top10 = newest across all
    top10 = items[:10]

    # Need-to-check: heuristics on titles
    title_blob = " ".join([it["title"].lower() for it in items[:50]])
    need_to_check = []
    patterns = [
        ("utilization", "出现“utilization/产能利用率”信号：建议对照你关心的节点/厂别，判断是否进入紧张或去库阶段。"),
        ("price", "出现“price/ASP/surcharge”信号：建议记录涨幅/节点/客户范围，准备议价与锁量策略。"),
        ("pull-in", "出现“pull-in orders/拉货/提前下单”信号：建议核对大客户项目节奏与交期波动。"),
        ("allocation", "出现“allocation/配额”信号：建议检查关键工艺/封装资源是否被AI挤占。"),
        ("export control", "出现“export control/制裁/限制”信号：建议马上评估EDA与供应链合规风险。"),
        ("coWoS".lower(), "出现“CoWoS/先进封装”信号：建议关注封装产能、良率与排产窗口。"),
    ]
    for k, msg in patterns:
        if k in title_blob:
            need_to_check.append(msg)

    if not need_to_check:
        need_to_check = [
            "每天看三件事：① 利用率/交期（紧不紧）② wafer/封装价格（涨不涨）③ 大客户项目与拉货（快不快）",
            "如果出现：配额/拉货/涨价/出口管制任一关键词 → 当天拉相关源做二次确认（官方/研究机构优先）。",
        ]

    # Sections by tag match
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
