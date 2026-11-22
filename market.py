"""Market data, news sentiment and conditional intraday scenarios (Python port of the Node app)."""
from __future__ import annotations

import html
import math
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote as urlquote, urlparse

import requests

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN = 9 * 60 + 15
SQUARE_OFF = 15 * 60 + 15
MORNING_REFRESH = 9 * 60 + 30

SOURCES = [
    {"name": "Economic Times", "url": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"},
    {"name": "Mint", "url": "https://www.livemint.com/rss/markets"},
    {"name": "Google News", "url": "https://news.google.com/rss/search?q=(Reliance+OR+Infosys+OR+TCS+OR+HDFC+OR+ICICI+OR+Airtel+OR+%22Tata+Motors%22+OR+ITC+OR+SBI+OR+Wipro+OR+NTPC+OR+ONGC)+shares+when:1d&hl=en-IN&gl=IN&ceid=IN:en"},
]

_UNIVERSE = [
    ("RELIANCE", "Reliance Industries", "Energy", ["reliance industries", "ril", "reliance"]),
    ("TCS", "Tata Consultancy Services", "Technology", ["tcs", "tata consultancy"]),
    ("INFY", "Infosys", "Technology", ["infosys"]),
    ("HCLTECH", "HCL Technologies", "Technology", ["hcl tech", "hcltech", "hcl technologies"]),
    ("WIPRO", "Wipro", "Technology", ["wipro"]),
    ("TECHM", "Tech Mahindra", "Technology", ["tech mahindra"]),
    ("HDFCBANK", "HDFC Bank", "Banking", ["hdfc bank", "hdfcbank"]),
    ("ICICIBANK", "ICICI Bank", "Banking", ["icici bank"]),
    ("SBIN", "State Bank of India", "Banking", ["sbi", "state bank of india"]),
    ("AXISBANK", "Axis Bank", "Banking", ["axis bank"]),
    ("KOTAKBANK", "Kotak Mahindra Bank", "Banking", ["kotak"]),
    ("BAJFINANCE", "Bajaj Finance", "Finance", ["bajaj finance"]),
    ("LT", "Larsen & Toubro", "Industrials", ["larsen", "l&t"]),
    ("TMPV", "Tata Motors Passenger Vehicles", "Automotive", ["tata motors passenger", "tata motors pv", "tmpv"]),
    ("M&M", "Mahindra & Mahindra", "Automotive", ["mahindra & mahindra", "m&m"]),
    ("MARUTI", "Maruti Suzuki", "Automotive", ["maruti"]),
    ("BHARTIARTL", "Bharti Airtel", "Telecom", ["airtel", "bharti"]),
    ("ITC", "ITC", "Consumer", ["itc"]),
    ("HINDUNILVR", "Hindustan Unilever", "Consumer", ["hindustan unilever", "hul"]),
    ("TITAN", "Titan Company", "Consumer", ["titan"]),
    ("SUNPHARMA", "Sun Pharma", "Healthcare", ["sun pharma"]),
    ("DRREDDY", "Dr. Reddy's Laboratories", "Healthcare", ["dr reddy", "dr. reddy"]),
    ("CIPLA", "Cipla", "Healthcare", ["cipla"]),
    ("TATASTEEL", "Tata Steel", "Metals", ["tata steel"]),
    ("JSWSTEEL", "JSW Steel", "Metals", ["jsw steel"]),
    ("ADANIENT", "Adani Enterprises", "Industrials", ["adani enterprises"]),
    ("ADANIPORTS", "Adani Ports", "Industrials", ["adani ports"]),
    ("NTPC", "NTPC", "Energy", ["ntpc"]),
    ("POWERGRID", "Power Grid", "Energy", ["power grid", "powergrid"]),
    ("ONGC", "ONGC", "Energy", ["ongc"]),
]
UNIVERSE = [{"symbol": s, "name": n, "sector": sec, "aliases": a} for s, n, sec, a in _UNIVERSE]


# ---------------------------------------------------------------- time helpers
def ist_parts(moment: datetime | None = None) -> dict:
    moment = (moment or datetime.now(timezone.utc)).astimezone(IST)
    return {"day": moment.strftime("%Y-%m-%d"), "minutes": moment.hour * 60 + moment.minute, "weekday": moment.strftime("%a")}


def in_session(moment: datetime | None = None) -> bool:
    p = ist_parts(moment)
    return p["weekday"] not in ("Sat", "Sun") and SESSION_OPEN <= p["minutes"] < SQUARE_OFF


def news_window_start(now: datetime | None = None) -> datetime:
    """The overnight window opens at the 15:30 IST close of the previous session."""
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(IST).replace(hour=15, minute=30, second=0, microsecond=0)
    return day - timedelta(days=1) if now < day else day


def snapshot_due(snapshot: dict | None, now: datetime | None = None) -> bool:
    """A snapshot is due when missing, from an earlier IST day, or taken before today's 09:30 refresh."""
    now = now or datetime.now(timezone.utc)
    if not snapshot:
        return True
    p = ist_parts(now)
    if snapshot.get("day") != p["day"]:
        return True
    updated = ist_parts(datetime.fromisoformat(snapshot["updatedAt"]))
    return p["minutes"] >= MORNING_REFRESH and updated["minutes"] < MORNING_REFRESH


# ------------------------------------------------------------------- sentiment
POSITIVE = re.compile(r"\b(beat(?:s)?|surge[sd]?|gain[sd]?|rall(?:y|ies|ied)|rise[sn]?|rose|jump(?:s|ed)?|growth|upgrade[sd]?|record high|wins?|approval|approved|profit(?:s)?|dividend|buyback|strong)\b", re.I)
NEGATIVE = re.compile(r"\b(miss(?:es|ed)?|fall(?:s|en)?|fell|drop(?:s|ped)?|slump[sd]?|loss(?:es)?|downgrade[sd]?|probe|fraud|penalty|penalties|weak|decline[sd]?|crash(?:es|ed)?|selloff|plunge[sd]?|cuts?|concerns?|reject(?:s|ed)?|lawsuit)\b", re.I)
NEGATION = re.compile(r"\b(?:no|not|fails? to)\s+(?:\w+\s+)?(growth|approval|approved|profit|gain|beat|strong)\b")
PROFIT_DOWN = re.compile(r"\bprofit(?:s)?\s+(?:fall|fell|drop|declin|slump|plung|down|cut)|\b(?:fall|drop|decline|cut)\w*\s+in\s+profit")
LOSS_NARROW = re.compile(r"\bloss(?:es)?\s+(?:narrow|shrink|reduc)|\bnarrow\w*\s+loss")


def sentiment(title: str) -> dict:
    text = title.lower()
    text = re.sub(r"\bstop[-\s]+loss\b", "", text)
    text = re.sub(r"\bprofit[-\s]+booking\b", "selling", text)
    positive = len(POSITIVE.findall(text))
    negative = len(NEGATIVE.findall(text))
    if re.search(r"\bstocks? to buy\b|\bbuy (?:rating|call|recommendation)\b", text):
        positive += 1
    if re.search(r"\bsell (?:rating|call|recommendation)\b|\bselling\b", text):
        negative += 1
    for _ in NEGATION.finditer(text):
        positive = max(0, positive - 1)
        negative += 1
    if PROFIT_DOWN.search(text):
        positive = max(0, positive - 1)
        if negative == 0:
            negative += 1
    if LOSS_NARROW.search(text):
        negative = max(0, negative - 1)
        positive += 1
    score = max(-3, min(3, positive - negative))
    return {"score": score, "label": "Positive" if score > 0 else "Negative" if score < 0 else "Neutral"}


def matches_stock(title: str, stock: dict) -> bool:
    if (stock["symbol"] == "SBIN"
            and re.search(r"\bsbi\s+(?:life|cards?)\b|\bstate bank of india\s+(?:life|cards?)\b", title, re.I)
            and not re.search(r"\bsbi\s+(?:bank|shares?|stock|profit)|\bstate bank of india\b(?!\s+(?:life|cards?))", title, re.I)):
        return False
    return any(re.search(rf"(^|[^a-z0-9]){re.escape(alias)}($|[^a-z0-9])", title, re.I) for alias in stock["aliases"])


def safe_url(value) -> str | None:
    try:
        parsed = urlparse(str(value).strip())
    except ValueError:
        return None
    return parsed.geturl() if parsed.scheme in ("http", "https") and parsed.netloc else None


def publisher_key(url: str | None, name: str) -> str:
    link = safe_url(url) if url else None
    if not link:
        return re.sub(r"[^a-z0-9]", "", re.sub(r"^the\s+", "", name.lower()))
    host = (urlparse(link).hostname or "").removeprefix("www.")
    if host == "economictimes.indiatimes.com" or host.endswith(".economictimes.indiatimes.com"):
        return "economic-times"
    if host == "livemint.com" or host.endswith(".livemint.com"):
        return "mint"
    return host


def news_confidence(news: list[dict], score: int, now: datetime | None = None) -> dict:
    """Heuristic headline-evidence percentage (not a probability of profit)."""
    now = now or datetime.now(timezone.utc)
    window_start = news_window_start(now)
    publishers: dict[str, dict] = {}
    for article in news:
        published = datetime.fromisoformat(article["publishedAt"])
        age = (now - published).total_seconds() * 1000
        if not article["score"] or age < 0 or published < window_start:
            continue
        key = article.get("publisherKey") or publisher_key(None, article["source"].split(" · via ")[0])
        ev = publishers.setdefault(key, {"votes": 0, "count": 0, "newest": math.inf})
        ev["votes"] += 1 if article["score"] > 0 else -1
        ev["count"] += 1
        ev["newest"] = min(ev["newest"], age)
    count = len(publishers)
    if not score or not count:
        return {"percent": None, "label": "No directional evidence", "publishers": count}
    direction = 1 if score > 0 else -1
    evs = list(publishers.values())
    agreement = sum((direction * e["votes"] / e["count"] + 1) / 2 for e in evs) / count
    freshness = sum(max(0, 1 - e["newest"] / (12 * 3600000)) for e in evs) / count
    breadth = min(count / 4, 1)
    cap = 49 if count == 1 else 74 if count == 2 else 90
    percent = min(cap, round(agreement * 60 + breadth * 25 + freshness * 15))
    return {
        "percent": percent, "label": "Strong" if percent >= 75 else "Moderate" if percent >= 50 else "Weak",
        "publishers": count, "agreementPct": round(agreement * 100), "freshnessPct": round(freshness * 100),
        "breadthPct": round(breadth * 100), "capped": percent == cap,
    }


# ------------------------------------------------------------------- RSS feeds
def parse_feed(xml_text: str, source: dict, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    root = ET.fromstring(xml_text)
    items = root.findall("./channel/item")
    if not items and root.find("./channel") is None:
        raise ValueError("Feed contains no RSS articles")
    window_start = news_window_start(now)
    articles = []
    for item in items:
        src = item.find("source")
        publisher = (src.text or "").strip() if src is not None else ""
        raw_title = re.sub(r"<[^>]*>", "", html.unescape(item.findtext("title") or "")).strip()
        suffix = f" - {publisher}"
        title = (raw_title[: -len(suffix)] if publisher and raw_title.endswith(suffix) else raw_title)[:220]
        url = safe_url(item.findtext("link") or "")
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if not title or not url or published > now or published < window_start:
            continue
        articles.append({
            "title": title, "url": url, "publishedAt": published.astimezone(timezone.utc).isoformat(),
            "source": f"{publisher} · via {source['name']}" if publisher else source["name"],
            "publisherKey": publisher_key((src.get("url") if src is not None else None) or source["url"], publisher or source["name"]),
            **sentiment(title),
        })
    return articles


HEADERS = {"User-Agent": "DaybreakResearch/1.0 (personal market research)"}


def _get(url: str, as_json: bool = False):
    response = requests.get(url, headers=HEADERS, timeout=14)
    response.raise_for_status()
    return response.json() if as_json else response.text


# ---------------------------------------------------------------------- quotes
def _chart(symbol: str, rng: str, interval: str) -> dict:
    data = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urlquote(symbol, safe='')}?range={rng}&interval={interval}", True)
    chart = data.get("chart") or {}
    result = (chart.get("result") or [None])[0]
    if not result or chart.get("error"):
        raise ValueError((chart.get("error") or {}).get("description") or "Price response missing")
    return result


def _candles(result: dict) -> list[dict]:
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    out = []
    for i, ts in enumerate(result.get("timestamp") or []):
        values = [(quote.get(k) or [])[i] if i < len(quote.get(k) or []) else None for k in ("open", "high", "low", "close")]
        if all(isinstance(v, (int, float)) and v > 0 for v in values):
            out.append({"time": datetime.fromtimestamp(ts, timezone.utc), "open": values[0], "high": values[1], "low": values[2], "close": values[3]})
    return out


def fetch_quote(symbol: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    result = _chart(symbol, "1mo", "1d")
    candles = _candles(result)
    meta = result.get("meta") or {}
    price, market_time = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if not isinstance(price, (int, float)) or not market_time or meta.get("currency") != "INR":
        raise ValueError("Missing INR price or timestamp")
    as_of = datetime.fromtimestamp(market_time, timezone.utc)
    today = ist_parts(now)["day"]
    previous = [c for c in candles if ist_parts(c["time"])["day"] < today]
    reference = previous[-1]["close"] if previous else None
    ranges = [max(c["high"] - c["low"], abs(c["high"] - previous[i]["close"]), abs(c["low"] - previous[i]["close"]))
              for i, c in enumerate(previous[1:])][-14:]
    atr = sum(ranges) / len(ranges) if len(ranges) >= 10 else None
    age = (now - as_of).total_seconds()
    return {
        "price": price, "asOf": as_of.isoformat(), "referenceClose": reference, "atr": atr,
        "changePct": (price / reference - 1) * 100 if reference else None,
        "fresh": ist_parts(as_of)["day"] == today and 0 <= age <= 20 * 60,
        "candles": [{"time": c["time"].isoformat(), "close": c["close"]} for c in candles],
        "url": f"https://finance.yahoo.com/quote/{urlquote(symbol, safe='')}/",
    }


def fetch_intraday(symbol: str) -> list[dict]:
    """Five-minute candles for the last few sessions (used to replay stops, targets and the 15:15 square-off)."""
    return _candles(_chart(symbol, "5d", "5m"))


# ------------------------------------------------------------------- scenarios
def _tick(x: float) -> float:
    return round(round(x / 0.05) * 0.05, 2)


def build_scenario(quote: dict | None, score: int, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    if not score or not quote or not quote.get("atr") or not quote.get("referenceClose"):
        return None
    direction = 1 if score > 0 else -1
    entry = _tick(quote["referenceClose"] + direction * quote["atr"] * 0.15)
    stop = _tick(entry - direction * quote["atr"] * 0.6)
    risk = direction * (entry - stop)
    if stop <= 0 or risk <= 0:
        return None
    target = _tick(entry + direction * risk * 2)
    if entry <= 0 or target <= 0:
        return None
    as_of = datetime.fromisoformat(quote["asOf"])
    fresh = 0 <= (now - as_of).total_seconds() <= 20 * 60 and ist_parts(as_of)["day"] == ist_parts(now)["day"]
    crossed = quote["price"] >= entry if direction > 0 else quote["price"] <= entry
    if not fresh:
        status = "Await fresh quote"
    elif not in_session(now):
        status = "Outside entry window"
    elif crossed:
        status = "Trigger already crossed"
    else:
        status = "Watch for breakout" if direction > 0 else "Watch for breakdown"
    return {
        "side": "LONG" if direction > 0 else "SHORT",
        "entryAction": "BUY" if direction > 0 else "SELL",
        "exitAction": "SELL" if direction > 0 else "BUY BACK",
        "entry": entry, "stop": stop, "target": target, "riskReward": direction * (target - entry) / risk,
        "status": status,
        "condition": (f"Illustrative {'buy then sell' if direction > 0 else 'sell then buy back'} scenario only: confirm a break and hold "
                      f"{'above' if direction > 0 else 'below'} entry using live broker prices and volume. Do not chase gaps. Exit at stop/target or by 15:15 IST; never carry overnight."
                      + (" Short selling requires broker eligibility and margin." if direction < 0 else "")),
    }


# ------------------------------------------------------------------ collection
def collect_market(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    errors: list[str] = []

    def load(source: dict) -> dict:
        try:
            articles = parse_feed(_get(source["url"]), source, now)
            return {**source, "status": "ok", "count": len(articles), "articles": articles}
        except Exception as error:  # noqa: BLE001 - surfaced to the user as a source error
            errors.append(f"{source['name']}: {error}")
            return {**source, "status": "error", "count": 0, "error": str(error), "articles": []}

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        sources = list(pool.map(load, SOURCES))
    seen, articles = set(), []
    for a in sorted((a for s in sources for a in s["articles"]), key=lambda a: a["publishedAt"], reverse=True):
        key = re.sub(r"[^a-z0-9]", "", a["title"].lower())
        if key not in seen:
            seen.add(key)
            articles.append(a)
    candidates = []
    for stock in UNIVERSE:
        news = [a for a in articles if matches_stock(a["title"], stock)]
        if news:
            score = sum(a["score"] for a in news)
            candidates.append({**{k: v for k, v in stock.items() if k != "aliases"}, "news": news, "score": score,
                               "sentiment": "Positive" if score > 0 else "Negative" if score < 0 else "Neutral"})
    candidates.sort(key=lambda s: (-abs(s["score"]), -len(s["news"])))

    def price(stock: dict) -> None:
        try:
            stock["quote"] = fetch_quote(f"{stock['symbol']}.NS", now)
        except Exception as error:  # noqa: BLE001
            stock["quoteError"] = str(error)
            errors.append(f"{stock['symbol']}: {error}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(price, candidates))
    index = None
    try:
        index = fetch_quote("^NSEI", now)
    except Exception as error:  # noqa: BLE001
        errors.append(f"NIFTY 50: {error}")
    return {
        "day": ist_parts(now)["day"], "updatedAt": datetime.now(timezone.utc).isoformat(),
        "sources": [{k: v for k, v in s.items() if k != "articles"} for s in sources],
        "articles": articles, "stocks": candidates, "index": index, "errors": errors, "universeSize": len(UNIVERSE),
        "provider": "Yahoo Finance (unofficial public endpoint; delayed, availability not guaranteed)",
    }


def present_stocks(snapshot: dict, now: datetime | None = None) -> list[dict]:
    """Recompute time-dependent fields (confidence, scenario status) when the snapshot is read."""
    now = now or datetime.now(timezone.utc)
    return [{**s, "scenario": build_scenario(s.get("quote"), s["score"], now), "confidence": news_confidence(s["news"], s["score"], now)}
            for s in snapshot.get("stocks", [])]
