"""Bounded official-site collection, with provenance and explicit source failures.

Disclosures corroborate an institution's stated activity; they do not attribute
an ETF's daily creation/redemption or determine the institution's trade amount.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import requests

CN = timezone(timedelta(hours=8))
DATA = Path(__file__).resolve().parent / "data"
SOURCES = (
    {"id": "huijin", "name": "中央汇金", "url": "https://www.huijin-inv.cn/huijin-inv/SC20262/Information_Center.shtml"},
    {"id": "csf", "name": "中国证券金融", "url": "https://www.csf.com.cn/publish/main/"},
    {"id": "ssf", "name": "全国社保基金理事会", "url": "https://www.ssf.gov.cn/portal/hndt/hndt/A000302index_1.htm"},
)
RELEVANT = re.compile(r"增持|减持|买入|持仓|持股|股票投资|资本市场|交易型开放式|ETF", re.I)


def _date(text: str) -> str | None:
    match = re.search(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})", text)
    if not match:
        return None
    try:
        return datetime(*map(int, match.groups())).date().isoformat()
    except ValueError:
        return None


def _soup(url: str) -> BeautifulSoup:
    response = requests.get(url, timeout=(5, 12), headers={"User-Agent": "EverQuantRadar/1.2 (public disclosure monitor)"})
    response.raise_for_status()
    if urlparse(response.url).hostname != urlparse(url).hostname:
        raise ValueError("unexpected_redirect")
    # Some official sites send malformed 'utf-8,gbk' HTTP charsets. Use the
    # document declaration, rather than requests' latin-1 fallback.
    soup = BeautifulSoup(response.content, "html.parser", from_encoding="utf-8")
    for tag in soup.select("script,style,nav,header,footer"):
        tag.decompose()
    return soup


def _record(source: dict, day: str, title: str, text: str, url: str, now: str) -> dict:
    # Plans and completed increases have different meanings; this deliberately
    # does not infer an amount, ticker or exact execution date from an article.
    action = "statement"
    if re.search(r"已.{0,8}(增持|买入)|近日.{0,8}增持|当日买入", text):
        action = "disclosed_increase"
    elif "已于近日扩大" in text and "增持范围" in text:
        action = "disclosed_scope_expansion"
    elif re.search(r"已.{0,8}减持", text):
        action = "disclosed_decrease"
    elif re.search(r"将.{0,12}增持|计划.{0,12}增持", text):
        action = "increase_intention"
    return {"id": hashlib.sha256(url.encode()).hexdigest()[:20], "date": day,
            "publication_date": day, "title": title, "url": url, "kind": "official",
            "source_id": source["id"], "source_name": source["name"], "entity": source["name"],
            "excerpt": text[:180], "action": action, "first_seen_at": now, "fetched_at": now,
            "verification": "official_source", "affects_score": False,
            "attribution_limit": "仅证明机构在该公告中披露的行为，不证明某只ETF当日申赎由该机构实施"}


def collect_source(source: dict, now: str) -> tuple[list[dict], dict]:
    rows, status = [], {"name": source["name"], "url": source["url"], "checked_at": now}
    try:
        soup = _soup(source["url"])
        if source["id"] == "huijin":
            pages = [(source["url"], soup)]
            # A category page includes official links to previous years. Scan
            # three years, bounded; no guessed article IDs or web-search claims.
            current_year = datetime.fromisoformat(now).year
            for year in (current_year, current_year - 1, current_year - 2):
                anchor = next((a for a in soup.select("a[href]") if a.get_text(strip=True) == str(year)), None)
                if anchor:
                    link = urljoin(source["url"], anchor["href"])
                    if link != source["url"]:
                        pages.append((link, _soup(link)))
            count = 0
            for base, page in pages:
                for item in page.select(".infor-list-item"):
                    count += 1
                    title = item.select_one("h1,h2,h3")
                    anchor, body = item.select_one("a[href]"), item.select_one("p")
                    year, day = item.select_one(".year"), item.select_one(".day")
                    published = _date(f"{year.get_text(strip=True)}.{day.get_text(strip=True)}") if year and day else None
                    text = body.get_text(" ", strip=True) if body else ""
                    if published and title and anchor and RELEVANT.search(text):
                        rows.append(_record(source, published, title.get_text(" ", strip=True), text, urljoin(base, anchor["href"]), now))
            if not count:
                raise ValueError("list_structure_changed")
        else:
            candidates, dated_links = [], 0
            for anchor in soup.select("a[href]"):
                title = anchor.get_text(" ", strip=True)
                link = urljoin(source["url"], anchor["href"])
                if urlparse(link).hostname != urlparse(source["url"]).hostname:
                    continue
                parent = anchor.parent.get_text(" ", strip=True)
                published = _date(parent) or _date(link)
                if published:
                    dated_links += 1
                if published and (RELEVANT.search(title) or title in {"公告", "公司公告"}):
                    candidates.append((title, link, published))
            if not dated_links:
                raise ValueError("list_structure_changed")
            for title, link, published in list(dict.fromkeys(candidates))[:8]:
                article = _soup(link)
                body = article.select_one("#zoom,.TRS_Editor,.article-content,.content-detail")
                if body is None:
                    continue  # Unrecognized layout is not verified article text.
                text = body.get_text(" ", strip=True)
                if RELEVANT.search(text):
                    rows.append(_record(source, published, title, text, link, now))
        status.update(state="ok", matched=len(rows), error=None)
    except Exception as exc:
        status.update(state="unavailable", matched=len(rows), error=type(exc).__name__)
    return rows, status


def load_disclosures() -> dict:
    try:
        return json.loads((DATA / "disclosures.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"items": [], "sources": {s["id"]: {"name": s["name"], "url": s["url"], "state": "not_fetched"} for s in SOURCES}}


def refresh_disclosures(force: bool = False) -> dict:
    from .server import atomic_json
    cached = load_disclosures()
    now = datetime.now(CN)
    try:
        if not force and now - datetime.fromisoformat(cached["checked_at"]) < timedelta(hours=6):
            return cached
    except (KeyError, ValueError, TypeError):
        pass
    stamp = now.isoformat(timespec="seconds")
    prior = {item["url"]: item for item in cached.get("items", [])}
    sources = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for records, status in pool.map(lambda s: collect_source(s, stamp), SOURCES):
            source_id = next(s["id"] for s in SOURCES if s["name"] == status["name"])
            sources[source_id] = status
            for item in records:
                if item["url"] in prior:
                    item["first_seen_at"] = prior[item["url"]].get("first_seen_at", stamp)
                prior[item["url"]] = item
    result = {"checked_at": stamp, "items": sorted(prior.values(), key=lambda x: x["date"], reverse=True)[:500], "sources": sources,
              "scope": "官方站点近页与汇金最近三年栏目；非全网、非全历史覆盖；失败保留旧公告且明确标识"}
    atomic_json(DATA / "disclosures.json", result)
    return result
