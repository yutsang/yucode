"""MCP server for financial data analysis.

Requires: yfinance >= 0.2, pandas >= 2.0

Run:  python -m coding_agent.plugins.mcp_servers.finance_mcp
"""

from __future__ import annotations

import json
from typing import Any

from ._protocol import McpStdioServer


def _require_yfinance():
    try:
        import yfinance as yf  # noqa: F401
        return yf
    except ImportError:
        raise RuntimeError("yfinance is required: pip install yfinance>=0.2")


def _require_pandas():
    try:
        import pandas  # noqa: F401
        return pandas
    except ImportError:
        raise RuntimeError("pandas is required: pip install pandas>=2.0")


def handle_quote(args: dict[str, Any]) -> Any:
    yf = _require_yfinance()
    ticker = yf.Ticker(args["symbol"])
    info = ticker.info
    keys = [
        "shortName", "longName", "symbol", "currency", "exchange",
        "currentPrice", "previousClose", "open", "dayLow", "dayHigh",
        "fiftyTwoWeekLow", "fiftyTwoWeekHigh", "volume", "averageVolume",
        "marketCap", "trailingPE", "forwardPE", "dividendYield",
        "beta", "sector", "industry",
    ]
    return {k: info.get(k) for k in keys if info.get(k) is not None}


def handle_history(args: dict[str, Any]) -> Any:
    yf = _require_yfinance()
    _require_pandas()
    ticker = yf.Ticker(args["symbol"])
    period = args.get("period", "1mo")
    interval = args.get("interval", "1d")
    hist = ticker.history(period=period, interval=interval)
    records = json.loads(hist.reset_index().to_json(orient="records", date_format="iso"))
    max_rows = int(args.get("max_rows", 100))
    return {"symbol": args["symbol"], "period": period, "interval": interval, "data": records[:max_rows]}


def handle_financials(args: dict[str, Any]) -> Any:
    yf = _require_yfinance()
    _require_pandas()
    ticker = yf.Ticker(args["symbol"])
    statement = args.get("statement", "income")
    quarterly = args.get("quarterly", False)

    if statement == "income":
        df = ticker.quarterly_income_stmt if quarterly else ticker.income_stmt
    elif statement == "balance":
        df = ticker.quarterly_balance_sheet if quarterly else ticker.balance_sheet
    elif statement == "cashflow":
        df = ticker.quarterly_cashflow if quarterly else ticker.cashflow
    else:
        raise ValueError(f"Unknown statement type: {statement}. Use income/balance/cashflow.")

    if df is None or df.empty:
        return {"symbol": args["symbol"], "statement": statement, "data": {}}

    result = {}
    for col in df.columns:
        col_label = str(col.date()) if hasattr(col, "date") else str(col)
        col_data = {}
        for idx in df.index:
            val = df.loc[idx, col]
            if val is not None and str(val) != "nan":
                col_data[str(idx)] = float(val)
        result[col_label] = col_data

    return {"symbol": args["symbol"], "statement": statement, "quarterly": quarterly, "data": result}


def handle_news(args: dict[str, Any]) -> Any:
    yf = _require_yfinance()
    ticker = yf.Ticker(args["symbol"])
    max_items = int(args.get("max_items", 10))
    news = ticker.news or []
    items = []
    for item in news[:max_items]:
        items.append({
            "title": item.get("title", ""),
            "publisher": item.get("publisher", ""),
            "link": item.get("link", ""),
            "providerPublishTime": item.get("providerPublishTime"),
        })
    return {"symbol": args["symbol"], "news": items}


def handle_compare(args: dict[str, Any]) -> Any:
    yf = _require_yfinance()
    symbols = args["symbols"]
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]
    metrics = args.get("metrics", ["currentPrice", "marketCap", "trailingPE", "dividendYield", "beta"])
    result = {}
    for sym in symbols:
        info = yf.Ticker(sym).info
        result[sym] = {m: info.get(m) for m in metrics if info.get(m) is not None}
    return {"comparison": result}


def main():
    server = McpStdioServer("yucode-finance")

    server.register_tool("quote", "Get a stock quote with key metrics.", {
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "Ticker symbol (e.g. AAPL, MSFT)"}},
        "required": ["symbol"],
    }, handle_quote)

    server.register_tool("history", "Get historical price data.", {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "period": {"type": "string", "description": "Period: 1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,ytd,max"},
            "interval": {"type": "string", "description": "Interval: 1m,2m,5m,15m,30m,60m,90m,1h,1d,5d,1wk,1mo,3mo"},
            "max_rows": {"type": "integer", "description": "Max data points to return (default 100)"},
        },
        "required": ["symbol"],
    }, handle_history)

    server.register_tool("financials", "Get financial statements (income, balance sheet, cash flow).", {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "statement": {"type": "string", "description": "Statement type: income, balance, cashflow"},
            "quarterly": {"type": "boolean", "description": "Quarterly instead of annual (default false)"},
        },
        "required": ["symbol"],
    }, handle_financials)

    server.register_tool("news", "Get recent news for a stock.", {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "max_items": {"type": "integer", "description": "Max news items (default 10)"},
        },
        "required": ["symbol"],
    }, handle_news)

    server.register_tool("compare", "Compare key metrics across multiple stocks.", {
        "type": "object",
        "properties": {
            "symbols": {
                "oneOf": [
                    {"type": "string", "description": "Comma-separated symbols"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            },
            "metrics": {"type": "array", "items": {"type": "string"}, "description": "Metrics to compare"},
        },
        "required": ["symbols"],
    }, handle_compare)

    server.serve()


if __name__ == "__main__":
    main()
