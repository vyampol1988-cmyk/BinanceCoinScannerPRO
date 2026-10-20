#!/usr/bin/env python3
# Binance Spot market scanner — analysis only, no trading or API keys required.
# Python 3.10+
import csv
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://data-api.binance.vision"
QUOTE_ASSET = "USDT"
TOP_LIQUID_PAIRS_TO_ANALYZE = 60
MIN_24H_QUOTE_VOLUME_USDT = 1_000_000
KLINE_INTERVAL = "1h"
KLINE_LIMIT = 60
REQUEST_PAUSE_SECONDS = 0.12
OUTPUT_DIR = Path("scanner_results")
TIMEOUT_SECONDS = 15

# This is a practical watchlist, not an official Binance classification.
# Binance does not provide a reliable "memecoin" label in its spot ticker API.
MEME_BASE_ASSETS = {
    "DOGE", "SHIB", "PEPE", "BONK", "FLOKI", "WIF", "BOME", "TURBO",
    "NEIRO", "NOT", "BRETT", "MEME", "POPCAT", "PENGU", "TRUMP",
    "PNUT", "ACT", "GOAT", "MEW", "MYRO", "MOG", "BABYDOGE", "1000SATS",
    "ORDI", "CAT", "WHY", "BAN", "CHEEMS", "TOSHI", "PONKE", "SLERF",
    "DEGEN", "MOODENG", "FWOG", "HIPPO", "CHILLGUY", "USELESS"
}

session = requests.Session()
session.headers.update({"User-Agent": "BinanceCoinScanner/1.0"})


def get_json(path, params=None):
    response = session.get(BASE_URL + path, params=params, timeout=TIMEOUT_SECONDS)
    if response.status_code in (418, 429):
        retry_after = float(response.headers.get("Retry-After", "3"))
        print(f"Binance rate limit response ({response.status_code}); waiting {retry_after:.1f}s.")
        time.sleep(max(retry_after, 1.0))
        response = session.get(BASE_URL + path, params=params, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def safe_float(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = alpha * value + (1 - alpha) * current
    return current


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes[-period:]]
    losses = [max(-change, 0.0) for change in changes[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def pct_change(new, old):
    if old == 0:
        return 0.0
    return (new / old - 1.0) * 100.0


def analyze_symbol(symbol):
    klines = get_json("/api/v3/klines", {
        "symbol": symbol, "interval": KLINE_INTERVAL, "limit": KLINE_LIMIT
    })
    if len(klines) < 30:
        return None

    # Exclude the last candle if it is still forming.
    candles = klines[:-1]
    closes = [safe_float(c[4]) for c in candles]
    quote_volumes = [safe_float(c[7]) for c in candles]
    highs = [safe_float(c[2]) for c in candles]
    lows = [safe_float(c[3]) for c in candles]
    if len(closes) < 25 or closes[-1] <= 0:
        return None

    last_price = closes[-1]
    change_1h = pct_change(closes[-1], closes[-2])
    change_4h = pct_change(closes[-1], closes[-5])
    change_24h = pct_change(closes[-1], closes[-25]) if len(closes) >= 25 else 0.0
    fast_ema = ema(closes, 9)
    slow_ema = ema(closes, 21)
    current_rsi = rsi(closes, 14)

    recent_volume = sum(quote_volumes[-5:]) / 5
    previous_volume = sum(quote_volumes[-25:-5]) / 20
    volume_ratio = recent_volume / previous_volume if previous_volume > 0 else 0.0

    # Simple score (0–100): momentum, trend, volume expansion, and a mild RSI penalty.
    score = 0.0
    score += max(0.0, min(25.0, change_4h * 2.5))
    score += 20.0 if fast_ema and slow_ema and fast_ema > slow_ema else 0.0
    score += max(0.0, min(25.0, (volume_ratio - 1.0) * 12.5))
    score += max(0.0, min(15.0, change_1h * 3.0))
    if current_rsi is not None:
        if 50 <= current_rsi <= 68:
            score += 15.0
        elif 42 <= current_rsi < 50 or 68 < current_rsi <= 75:
            score += 8.0
        elif current_rsi > 82:
            score -= 10.0
    score = max(0.0, min(100.0, score))

    # Recent candle range as a rough volatility indicator.
    ranges = [
        (high - low) / close * 100.0
        for high, low, close in zip(highs[-24:], lows[-24:], closes[-24:])
        if close > 0
    ]
    avg_hourly_range = sum(ranges) / len(ranges) if ranges else 0.0

    return {
        "symbol": symbol,
        "base_asset": symbol[:-len(QUOTE_ASSET)],
        "price": last_price,
        "score": round(score, 1),
        "change_1h_pct": round(change_1h, 2),
        "change_4h_pct": round(change_4h, 2),
        "change_24h_pct": round(change_24h, 2),
        "rsi_14": round(current_rsi, 1) if current_rsi is not None else "",
        "volume_ratio_5h_vs_prev20h": round(volume_ratio, 2),
        "avg_hourly_range_pct": round(avg_hourly_range, 2),
        "trend": "UP" if fast_ema and slow_ema and fast_ema > slow_ema else "NOT UP",
        "category": "MEME WATCHLIST" if symbol[:-len(QUOTE_ASSET)] in MEME_BASE_ASSETS else "OTHER",
    }


def main():
    print("Loading public Binance Spot market data...")
    exchange_info = get_json("/api/v3/exchangeInfo")
    allowed = {
        item["symbol"] for item in exchange_info["symbols"]
        if item.get("status") == "TRADING"
        and item.get("quoteAsset") == QUOTE_ASSET
        and item.get("isSpotTradingAllowed", True)
    }

    tickers = get_json("/api/v3/ticker/24hr")
    candidates = []
    for item in tickers:
        symbol = item.get("symbol", "")
        if symbol not in allowed or not symbol.endswith(QUOTE_ASSET):
            continue
        # Exclude leveraged tokens and common non-coin quote/base oddities.
        base = symbol[:-len(QUOTE_ASSET)]
        if any(tag in base for tag in ("UP", "DOWN", "BULL", "BEAR")):
            continue
        quote_volume = safe_float(item.get("quoteVolume"))
        if quote_volume < MIN_24H_QUOTE_VOLUME_USDT:
            continue
        candidates.append({
            "symbol": symbol,
            "quote_volume": quote_volume,
            "change_24h_ticker": safe_float(item.get("priceChangePercent")),
        })

    candidates.sort(key=lambda item: item["quote_volume"], reverse=True)
    candidates = candidates[:TOP_LIQUID_PAIRS_TO_ANALYZE]
    print(f"Analyzing {len(candidates)} liquid USDT pairs (1h candles)...")

    results = []
    errors = 0
    for index, candidate in enumerate(candidates, start=1):
        try:
            row = analyze_symbol(candidate["symbol"])
            if row:
                row["quote_volume_24h_usdt"] = round(candidate["quote_volume"], 0)
                # Use ticker's official rolling 24h change for display.
                row["change_24h_pct"] = round(candidate["change_24h_ticker"], 2)
                results.append(row)
            print(f"\rProgress: {index}/{len(candidates)}", end="", flush=True)
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            errors += 1
            print(f"\nSkipping {candidate['symbol']}: {exc}")
        time.sleep(REQUEST_PAUSE_SECONDS)

    print("\n")
    if not results:
        print("No results received. Check your internet connection and try again.")
        return

    results.sort(key=lambda row: row["score"], reverse=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_UTC")
    csv_path = OUTPUT_DIR / f"binance_scan_{stamp}.csv"
    fields = [
        "symbol", "category", "score", "price", "change_1h_pct", "change_4h_pct",
        "change_24h_pct", "rsi_14", "volume_ratio_5h_vs_prev20h",
        "avg_hourly_range_pct", "trend", "quote_volume_24h_usdt"
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    print("TOP 20 — strongest heuristic scores (not buy recommendations)")
    print("-" * 105)
    print(f"{'PAIR':<15} {'TYPE':<14} {'SCORE':>6} {'24H%':>8} {'4H%':>8} {'RSI':>6} {'VOL x':>7} {'TREND':>8}")
    print("-" * 105)
    for row in results[:20]:
        rsi_text = str(row["rsi_14"]) if row["rsi_14"] != "" else "n/a"
        print(
            f"{row['symbol']:<15} {row['category']:<14} {row['score']:>6.1f} "
            f"{row['change_24h_pct']:>8.2f} {row['change_4h_pct']:>8.2f} "
            f"{rsi_text:>6} {row['volume_ratio_5h_vs_prev20h']:>7.2f} {row['trend']:>8}"
        )

    meme_rows = [row for row in results if row["category"] == "MEME WATCHLIST"]
    meme_rows.sort(key=lambda row: row["score"], reverse=True)
    print("\nTOP MEME-COIN WATCHLIST")
    print("-" * 80)
    if not meme_rows:
        print("No watchlist meme coins passed the liquidity filter in this scan.")
    else:
        for row in meme_rows[:15]:
            print(
                f"{row['symbol']:<15} score={row['score']:>5.1f} | "
                f"24h={row['change_24h_pct']:>7.2f}% | "
                f"4h={row['change_4h_pct']:>7.2f}% | "
                f"volume ratio={row['volume_ratio_5h_vs_prev20h']:.2f}x"
            )

    print(f"\nCSV report saved to: {csv_path.resolve()}")
    print(f"Pairs analyzed: {len(results)}; skipped/errors: {errors}")
    print("\nIMPORTANT: Score is a simple ranking heuristic, not a prediction.")
    print("It cannot identify scams, token unlocks, news risk, or guarantee future gains.")
    print("Memecoin labels are a manually maintained watchlist, not an official Binance category.")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as exc:
        print(f"\nNetwork/API error: {exc}")
        print("Try again later. This scanner uses public market-data endpoints and needs no API key.")
