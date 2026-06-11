from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_CHART_URL_TEMPLATE = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_COOKIE_URL = "https://fc.yahoo.com"
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
EASTMONEY_FUND_URL = "https://fund.eastmoney.com/pingzhongdata/005698.js"

US_MARKET_SYMBOLS = ["^IXIC", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"]
MAGNIFICENT_SEVEN_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"]

DISPLAY_NAMES = {
    "^IXIC": "纳斯达克",
    "AAPL": "AAPL",
    "MSFT": "MSFT",
    "NVDA": "NVDA",
    "AMZN": "AMZN",
    "GOOGL": "GOOGL",
    "META": "META",
    "TSLA": "TSLA",
}

REQUEST_TIMEOUT_SECONDS = 15
LOGGER = logging.getLogger("daily-market-feishu")


class MarketDataError(RuntimeError):
    """Raised when remote market data cannot be fetched or parsed."""


class FeishuPushError(RuntimeError):
    """Raised when Feishu webhook push fails."""


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: float
    change: float
    change_percent: float
    previous_close: float
    market_time: datetime | None


@dataclass(frozen=True)
class Fund:
    code: str
    name: str
    net_value: float
    value_date: str
    daily_change_percent: float


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def fetch_yahoo_quotes() -> dict[str, Quote]:
    LOGGER.info("Fetching Yahoo Finance quotes for: %s", ",".join(US_MARKET_SYMBOLS))
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            )
        }
    )

    try:
        crumb = fetch_yahoo_crumb(session)
        response = yahoo_quote_request(session, crumb=crumb)
        if response.status_code == 401:
            LOGGER.warning("Yahoo Finance quote request returned 401; retrying with crumb and cookie.")
            crumb = fetch_yahoo_crumb(session)
            response = yahoo_quote_request(session, crumb=crumb)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        LOGGER.warning("Yahoo Finance quote 接口请求失败，将尝试 chart 备用接口：%s", exc)
        return fetch_yahoo_quotes_from_chart(session)
    except json.JSONDecodeError:
        LOGGER.warning("Yahoo Finance quote 接口返回内容不是有效 JSON，将尝试 chart 备用接口。")
        return fetch_yahoo_quotes_from_chart(session)
    except MarketDataError as exc:
        LOGGER.warning("Yahoo Finance quote 接口不可用，将尝试 chart 备用接口：%s", exc)
        return fetch_yahoo_quotes_from_chart(session)

    try:
        results = payload["quoteResponse"]["result"]
    except (KeyError, TypeError) as exc:
        raise MarketDataError(f"Yahoo Finance 返回结构异常，缺少 quoteResponse.result：{payload}") from exc

    quotes: dict[str, Quote] = {}
    for item in results:
        try:
            symbol = item["symbol"]
            price = require_number(item, "regularMarketPrice", symbol)
            change = require_number(item, "regularMarketChange", symbol)
            change_percent = require_number(item, "regularMarketChangePercent", symbol)
            previous_close = require_number(item, "regularMarketPreviousClose", symbol)
            market_time = parse_market_time(item.get("regularMarketTime"))
            name = item.get("shortName") or item.get("longName") or DISPLAY_NAMES.get(symbol, symbol)
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketDataError(f"Yahoo Finance 字段解析失败，原始数据：{item}") from exc

        quotes[symbol] = Quote(
            symbol=symbol,
            name=str(name),
            price=price,
            change=change,
            change_percent=change_percent,
            previous_close=previous_close,
            market_time=market_time,
        )

    missing_symbols = [symbol for symbol in US_MARKET_SYMBOLS if symbol not in quotes]
    if missing_symbols:
        raise MarketDataError(f"Yahoo Finance 未返回以下标的：{', '.join(missing_symbols)}")

    return quotes


def yahoo_quote_request(session: requests.Session, crumb: str | None = None) -> requests.Response:
    params = {"symbols": ",".join(US_MARKET_SYMBOLS)}
    if crumb:
        params["crumb"] = crumb
    return session.get(YAHOO_QUOTE_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)


def fetch_yahoo_quotes_from_chart(session: requests.Session) -> dict[str, Quote]:
    LOGGER.info("Fetching Yahoo Finance chart fallback data.")
    quotes: dict[str, Quote] = {}

    for symbol in US_MARKET_SYMBOLS:
        url = YAHOO_CHART_URL_TEMPLATE.format(symbol=symbol)
        try:
            response = session.get(url, params={"range": "5d", "interval": "1d"}, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise MarketDataError(f"Yahoo Finance chart 备用接口请求失败，标的 {symbol}：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise MarketDataError(f"Yahoo Finance chart 备用接口返回内容不是有效 JSON，标的 {symbol}。") from exc

        try:
            chart = payload["chart"]
            error = chart.get("error")
            if error:
                raise MarketDataError(f"Yahoo Finance chart 备用接口返回错误，标的 {symbol}：{error}")
            result = chart["result"][0]
            meta = result["meta"]
            timestamps = result.get("timestamp") or []
            close_values = result["indicators"]["quote"][0]["close"]
            closes = [float(value) for value in close_values if isinstance(value, (int, float))]
            price = meta.get("regularMarketPrice")
            if not isinstance(price, (int, float)):
                price = closes[-1] if closes else None
            if not isinstance(price, (int, float)):
                raise ValueError("缺少 regularMarketPrice 和 close")

            previous_close = closes[-2] if len(closes) >= 2 else meta.get("chartPreviousClose")
            if not isinstance(previous_close, (int, float)) or previous_close == 0:
                raise ValueError("缺少可用的前一交易日收盘价")

            market_timestamp = meta.get("regularMarketTime")
            if not isinstance(market_timestamp, (int, float)) and timestamps:
                market_timestamp = timestamps[-1]

            change = float(price) - float(previous_close)
            change_percent = change / float(previous_close) * 100
            name = meta.get("shortName") or meta.get("longName") or DISPLAY_NAMES.get(symbol, symbol)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise MarketDataError(f"Yahoo Finance chart 备用接口字段解析失败，标的 {symbol}，原始数据：{payload}") from exc

        quotes[symbol] = Quote(
            symbol=symbol,
            name=str(name),
            price=float(price),
            change=change,
            change_percent=change_percent,
            previous_close=float(previous_close),
            market_time=parse_market_time(market_timestamp),
        )

    return quotes


def fetch_yahoo_crumb(session: requests.Session) -> str:
    try:
        session.get(YAHOO_COOKIE_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        response = session.get(YAHOO_CRUMB_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise MarketDataError(f"Yahoo Finance 获取 crumb/cookie 失败：{exc}") from exc

    crumb = response.text.strip()
    if not crumb:
        raise MarketDataError("Yahoo Finance 获取 crumb/cookie 失败：crumb 为空。")
    return crumb


def require_number(item: dict[str, Any], field: str, symbol: str) -> float:
    value = item.get(field)
    if not isinstance(value, (int, float)):
        raise ValueError(f"{symbol} 缺少数值字段 {field}")
    return float(value)


def parse_market_time(timestamp: Any) -> datetime | None:
    if not isinstance(timestamp, (int, float)):
        return None
    return datetime.fromtimestamp(timestamp, tz=ZoneInfo("America/New_York"))


def fetch_eastmoney_fund() -> Fund:
    LOGGER.info("Fetching Eastmoney fund data for 005698")
    try:
        response = requests.get(
            EASTMONEY_FUND_URL,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                "Referer": "https://fund.eastmoney.com/005698.html",
                "User-Agent": "daily-market-feishu/1.0",
            },
        )
        response.raise_for_status()
        response.encoding = "utf-8"
        script = response.text
    except requests.RequestException as exc:
        raise MarketDataError(f"东方财富基金接口请求失败：{exc}") from exc

    name = extract_js_string(script, "fS_name", "基金名称")
    code = extract_js_string(script, "fS_code", "基金代码")
    trend = extract_js_array(script, "Data_netWorthTrend", "基金净值走势")

    if not trend:
        raise MarketDataError("东方财富基金接口未返回净值走势 Data_netWorthTrend。")

    latest = trend[-1]
    try:
        net_value = latest["y"]
        timestamp_ms = latest["x"]
        daily_change_percent = latest["equityReturn"]
    except KeyError as exc:
        raise MarketDataError(f"东方财富基金最新净值字段缺失，最新记录：{latest}") from exc

    if not isinstance(net_value, (int, float)) or not isinstance(timestamp_ms, (int, float)):
        raise MarketDataError(f"东方财富基金最新净值格式异常，最新记录：{latest}")
    if not isinstance(daily_change_percent, (int, float)):
        raise MarketDataError(f"东方财富基金日涨跌字段 equityReturn 格式异常，最新记录：{latest}")

    value_date = datetime.fromtimestamp(timestamp_ms / 1000, tz=ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    return Fund(
        code=code,
        name=name,
        net_value=float(net_value),
        value_date=value_date,
        daily_change_percent=float(daily_change_percent),
    )


def extract_js_string(script: str, variable_name: str, label: str) -> str:
    pattern = rf"var\s+{re.escape(variable_name)}\s*=\s*['\"]([^'\"]+)['\"]\s*;"
    match = re.search(pattern, script)
    if not match:
        raise MarketDataError(f"东方财富基金接口字段变化：无法解析{label}变量 {variable_name}。")
    return match.group(1)


def extract_js_array(script: str, variable_name: str, label: str) -> list[dict[str, Any]]:
    pattern = rf"var\s+{re.escape(variable_name)}\s*=\s*(\[.*?\])\s*;"
    match = re.search(pattern, script, flags=re.DOTALL)
    if not match:
        raise MarketDataError(f"东方财富基金接口字段变化：无法解析{label}变量 {variable_name}。")

    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise MarketDataError(f"东方财富基金接口字段变化：{label}不是有效 JSON 数组。") from exc

    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise MarketDataError(f"东方财富基金接口字段变化：{label}格式不是对象数组。")
    return parsed


def format_percent(value: float) -> str:
    return f"{value:+.2f}%"


def format_price(value: float, with_dollar: bool = False) -> str:
    prefix = "$" if with_dollar else ""
    return f"{prefix}{value:.2f}"


def build_message(quotes: dict[str, Quote], fund: Fund) -> str:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    index_quote = quotes["^IXIC"]
    seven_quotes = [quotes[symbol] for symbol in MAGNIFICENT_SEVEN_SYMBOLS]
    average_change = sum(quote.change_percent for quote in seven_quotes) / len(seven_quotes)
    top_gainer = max(seven_quotes, key=lambda quote: quote.change_percent)
    top_loser = min(seven_quotes, key=lambda quote: quote.change_percent)

    lines = [
        "📈 每日美股与基金速览",
        f"日期：{today}",
        "",
        "【指数】",
        f"{DISPLAY_NAMES[index_quote.symbol]}：{format_price(index_quote.price)}  {format_percent(index_quote.change_percent)}",
        "",
        "【美股七巨头】",
    ]

    for symbol in MAGNIFICENT_SEVEN_SYMBOLS:
        quote = quotes[symbol]
        lines.append(f"{DISPLAY_NAMES[symbol]}：{format_price(quote.price, with_dollar=True)}  {format_percent(quote.change_percent)}")

    lines.extend(
        [
            "",
            f"七巨头平均涨跌：{format_percent(average_change)}",
            f"领涨：{DISPLAY_NAMES[top_gainer.symbol]} {format_percent(top_gainer.change_percent)}",
            f"领跌：{DISPLAY_NAMES[top_loser.symbol]} {format_percent(top_loser.change_percent)}",
            "",
            "【关注基金】",
            f"{fund.name} {fund.code}",
            f"最新净值：{fund.net_value:.4f}",
            f"净值日期：{fund.value_date}",
            f"日涨跌：{format_percent(fund.daily_change_percent)}",
            "",
            "仅供个人记录，不构成投资建议。",
        ]
    )
    return "\n".join(lines)


def build_feishu_payload(text: str, secret: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": text},
    }

    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        sign = base64.b64encode(
            hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        payload["timestamp"] = timestamp
        payload["sign"] = sign

    return payload


def push_to_feishu(webhook_url: str, text: str, secret: str | None = None) -> None:
    payload = build_feishu_payload(text, secret)
    LOGGER.info("Pushing message to Feishu webhook")

    try:
        response = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        raise FeishuPushError(f"飞书 Webhook 请求失败：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise FeishuPushError(f"飞书 Webhook 返回内容不是有效 JSON：{response.text}") from exc

    code = result.get("code")
    if code not in (0, None):
        raise FeishuPushError(f"飞书 Webhook 推送失败，返回：{result}")

    if result.get("StatusCode") not in (0, None):
        raise FeishuPushError(f"飞书 Webhook 推送失败，返回：{result}")

    LOGGER.info("Feishu message pushed successfully")


def main() -> int:
    load_dotenv()
    configure_logging()

    webhook_url = os.getenv("FEISHU_WEBHOOK_URL")
    secret = os.getenv("FEISHU_SECRET") or None

    if not webhook_url:
        LOGGER.error("缺少必填环境变量 FEISHU_WEBHOOK_URL。")
        return 2

    try:
        quotes = fetch_yahoo_quotes()
        fund = fetch_eastmoney_fund()
        message = build_message(quotes, fund)
        LOGGER.info("Message preview:\n%s", message)
        push_to_feishu(webhook_url, message, secret)
    except (MarketDataError, FeishuPushError) as exc:
        LOGGER.exception("任务执行失败：%s", exc)
        return 1
    except Exception as exc:
        LOGGER.exception("任务执行出现未预期错误：%s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
