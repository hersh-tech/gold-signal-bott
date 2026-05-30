"""
XAU/USD Advanced Gold Signal Bot v2.0
Multi-Timeframe Confluence + Signal Scoring + Dynamic SL/TP
Timeframes: 15M + 4H + Daily
Only sends signals scoring 75+/100
"""

import os
import asyncio
import logging
import json
import re
import math
from datetime import datetime, timezone
from typing import Optional

import httpx
from telegram import Bot
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Config ───────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
SIGNAL_INTERVAL  = int(os.getenv("SIGNAL_INTERVAL_MINUTES", "15"))
MIN_SCORE        = int(os.getenv("MIN_SIGNAL_SCORE", "75"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Price History (multi-timeframe buckets) ──────────────
prices_15m:  list = []   # last 100 15-min prices
prices_4h:   list = []   # last 60  4-hour prices
prices_1d:   list = []   # last 30  daily prices
tick_count   = 0         # counts fetch cycles for bucketing

# ─── Technical Indicators ─────────────────────────────────

def calc_ma(prices: list, period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return round(sum(prices[-period:]) / period, 2)

def calc_ema(prices: list, period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = prices[-period]
    for p in prices[-period+1:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 2)

def calc_rsi(prices: list, period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        d = prices[i] - prices[i-1]
        if d > 0: gains += d
        else: losses -= d
    rs = gains / (losses or 1e-9)
    return round(100 - 100 / (1 + rs), 1)

def calc_rsi_divergence(prices: list, rsi_period: int = 14) -> str:
    """Detect bullish/bearish RSI divergence."""
    if len(prices) < rsi_period + 10:
        return "NONE"
    rsi_now  = calc_rsi(prices, rsi_period)
    rsi_prev = calc_rsi(prices[:-5], rsi_period)
    if not rsi_now or not rsi_prev:
        return "NONE"
    price_up = prices[-1] > prices[-6]
    rsi_up   = rsi_now > rsi_prev
    if price_up and not rsi_up:
        return "BEARISH_DIV"   # price up, RSI down = bearish divergence
    if not price_up and rsi_up:
        return "BULLISH_DIV"   # price down, RSI up = bullish divergence
    return "NONE"

def calc_macd(prices: list) -> dict:
    if len(prices) < 26:
        return {"macd": None, "signal": None, "histogram": None, "cross": "NONE"}
    ema12 = calc_ema(prices, 12)
    ema26 = calc_ema(prices, 26)
    if not ema12 or not ema26:
        return {"macd": None, "signal": None, "histogram": None, "cross": "NONE"}
    macd_line = round(ema12 - ema26, 3)
    # signal = 9-period EMA of MACD (simplified)
    macd_values = []
    for i in range(9, 0, -1):
        e12 = calc_ema(prices[:-i] if i > 0 else prices, 12)
        e26 = calc_ema(prices[:-i] if i > 0 else prices, 26)
        if e12 and e26:
            macd_values.append(e12 - e26)
    signal_line = round(sum(macd_values) / len(macd_values), 3) if macd_values else None
    histogram   = round(macd_line - signal_line, 3) if signal_line else None
    cross = "NONE"
    if signal_line:
        if macd_line > signal_line and macd_line > 0:
            cross = "BULLISH"
        elif macd_line < signal_line and macd_line < 0:
            cross = "BEARISH"
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram, "cross": cross}

def calc_bollinger(prices: list, period: int = 20) -> dict:
    if len(prices) < period:
        return {"upper": None, "middle": None, "lower": None, "position": "MIDDLE"}
    slice_p = prices[-period:]
    middle  = sum(slice_p) / period
    std     = math.sqrt(sum((p - middle)**2 for p in slice_p) / period)
    upper   = round(middle + 2 * std, 2)
    lower   = round(middle - 2 * std, 2)
    middle  = round(middle, 2)
    price   = prices[-1]
    if price >= upper:
        position = "ABOVE_UPPER"
    elif price <= lower:
        position = "BELOW_LOWER"
    elif price > middle:
        position = "UPPER_HALF"
    else:
        position = "LOWER_HALF"
    return {"upper": upper, "middle": middle, "lower": lower, "position": position}

def calc_atr(prices: list, period: int = 14) -> Optional[float]:
    """Simplified ATR using price range as proxy."""
    if len(prices) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        high = prices[i] * 1.0015  # estimated high
        low  = prices[i] * 0.9985  # estimated low
        prev = prices[i-1]
        tr = max(high - low, abs(high - prev), abs(low - prev))
        trs.append(tr)
    return round(sum(trs) / len(trs), 2)

def calc_fib(prices: list) -> dict:
    if len(prices) < 2:
        return {}
    hi = max(prices[-50:]) if len(prices) >= 50 else max(prices)
    lo = min(prices[-50:]) if len(prices) >= 50 else min(prices)
    d  = hi - lo
    return {
        "high":    round(hi, 2),
        "low":     round(lo, 2),
        "fib786":  round(hi - d * 0.786, 2),
        "fib618":  round(hi - d * 0.618, 2),
        "fib500":  round(hi - d * 0.500, 2),
        "fib382":  round(hi - d * 0.382, 2),
        "fib236":  round(hi - d * 0.236, 2),
        "ext1272": round(hi + d * 0.272, 2),
        "ext1618": round(hi + d * 0.618, 2),
    }

def calc_support_resistance(prices: list) -> dict:
    if len(prices) < 20:
        return {"support": prices[-1] - 5, "resistance": prices[-1] + 5}
    recent = prices[-20:]
    resistance = max(recent)
    support    = min(recent)
    return {"support": round(support, 2), "resistance": round(resistance, 2)}

def calc_dynamic_levels(signal: str, entry: float, atr: float) -> dict:
    """Dynamic SL/TP based on ATR — adapts to real market volatility."""
    if signal == "HOLD" or not atr:
        return {}
    d = 1 if signal == "BUY" else -1
    sl_mult  = 1.5   # SL = 1.5x ATR
    tp1_mult = 2.0   # TP1 = 2x ATR
    tp2_mult = 3.5   # TP2 = 3.5x ATR
    tp3_mult = 5.0   # TP3 = 5x ATR (extended target)
    return {
        "sl":  round(entry - d * atr * sl_mult,  2),
        "tp1": round(entry + d * atr * tp1_mult, 2),
        "tp2": round(entry + d * atr * tp2_mult, 2),
        "tp3": round(entry + d * atr * tp3_mult, 2),
        "sl_pips":  round(atr * sl_mult  / 0.10),
        "tp1_pips": round(atr * tp1_mult / 0.10),
        "tp2_pips": round(atr * tp2_mult / 0.10),
        "tp3_pips": round(atr * tp3_mult / 0.10),
        "rr1": f"1:{round(tp1_mult/sl_mult, 1)}",
        "rr2": f"1:{round(tp2_mult/sl_mult, 1)}",
        "rr3": f"1:{round(tp3_mult/sl_mult, 1)}",
    }

# ─── Signal Scoring Engine ────────────────────────────────

def score_signal(direction: str, tf_data: dict) -> dict:
    """
    Score a signal 0-100 based on indicator confluence.
    Only signals >= MIN_SCORE get sent to Telegram.
    """
    score  = 0
    breakdown = {}

    is_buy  = direction == "BUY"
    is_sell = direction == "SELL"

    # 1. RSI (15 pts)
    rsi = tf_data.get("rsi_15m")
    if rsi:
        if is_buy  and 30 < rsi < 50: score += 15; breakdown["RSI"] = f"+15 (oversold recovery {rsi})"
        elif is_buy and rsi < 30:      score += 12; breakdown["RSI"] = f"+12 (oversold {rsi})"
        elif is_sell and 50 < rsi < 70: score += 15; breakdown["RSI"] = f"+15 (overbought pullback {rsi})"
        elif is_sell and rsi > 70:     score += 12; breakdown["RSI"] = f"+12 (overbought {rsi})"
        else:                          score += 5;  breakdown["RSI"] = f"+5 (neutral {rsi})"

    # 2. RSI Divergence (10 pts)
    div = tf_data.get("rsi_div", "NONE")
    if (is_buy and div == "BULLISH_DIV") or (is_sell and div == "BEARISH_DIV"):
        score += 10; breakdown["RSI Divergence"] = "+10 (confirmed divergence)"
    else:
        breakdown["RSI Divergence"] = "+0 (no divergence)"

    # 3. MACD (15 pts)
    macd_cross = tf_data.get("macd_cross", "NONE")
    if (is_buy and macd_cross == "BULLISH") or (is_sell and macd_cross == "BEARISH"):
        score += 15; breakdown["MACD"] = "+15 (confirmed crossover)"
    else:
        score += 5; breakdown["MACD"] = "+5 (no crossover)"

    # 4. MA Confluence (20 pts)
    ma20  = tf_data.get("ma20")
    ma50  = tf_data.get("ma50")
    ma200 = tf_data.get("ma200")
    price = tf_data.get("price", 0)
    ma_pts = 0
    if ma20 and ma50:
        if is_buy  and ma20 > ma50:  ma_pts += 8
        if is_sell and ma20 < ma50:  ma_pts += 8
    if ma200 and price:
        if is_buy  and price > ma200: ma_pts += 7
        if is_sell and price < ma200: ma_pts += 7
    if ma20 and price:
        if is_buy  and price > ma20:  ma_pts += 5
        if is_sell and price < ma20:  ma_pts += 5
    score += ma_pts
    breakdown["MA Confluence"] = f"+{ma_pts} (MA20/50/200 alignment)"

    # 5. Bollinger Bands (10 pts)
    bb_pos = tf_data.get("bb_position", "MIDDLE")
    if (is_buy and bb_pos == "BELOW_LOWER") or (is_sell and bb_pos == "ABOVE_UPPER"):
        score += 10; breakdown["Bollinger"] = "+10 (price at band extreme)"
    elif (is_buy and bb_pos == "LOWER_HALF") or (is_sell and bb_pos == "UPPER_HALF"):
        score += 5;  breakdown["Bollinger"] = "+5 (price in correct half)"
    else:
        breakdown["Bollinger"] = "+0 (opposing BB zone)"

    # 6. Fibonacci Zone (15 pts)
    fib_zone = tf_data.get("fib_zone", "NONE")
    if "support" in fib_zone.lower() and is_buy:
        score += 15; breakdown["Fibonacci"] = f"+15 ({fib_zone})"
    elif "resistance" in fib_zone.lower() and is_sell:
        score += 15; breakdown["Fibonacci"] = f"+15 ({fib_zone})"
    elif "0.500" in fib_zone or "neutral" in fib_zone.lower():
        score += 7;  breakdown["Fibonacci"] = f"+7 ({fib_zone})"
    else:
        score += 3;  breakdown["Fibonacci"] = f"+3 ({fib_zone})"

    # 7. Multi-timeframe alignment (15 pts)
    mtf = tf_data.get("mtf_alignment", "NONE")
    if mtf == "STRONG":
        score += 15; breakdown["MTF"] = "+15 (all timeframes aligned)"
    elif mtf == "MODERATE":
        score += 8;  breakdown["MTF"] = "+8 (2/3 timeframes aligned)"
    else:
        score += 2;  breakdown["MTF"] = "+2 (weak MTF confluence)"

    score = min(score, 100)
    return {"score": score, "breakdown": breakdown}

# ─── API Helper ───────────────────────────────────────────

def api_headers() -> dict:
    return {
        "x-api-key":         ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }

async def claude(body: dict, timeout: int = 50) -> list:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers=api_headers(), json=body,
        )
        return r.json().get("content", [])

def first_text(blocks: list) -> str:
    for b in blocks:
        if b.get("type") == "text":
            return b["text"].replace("```json","").replace("```","").strip()
    return "{}"

# ─── Live Price ───────────────────────────────────────────

async def fetch_live_price() -> Optional[float]:
    blocks = await claude({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 150,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "system": 'Search current XAU/USD spot price. Return ONLY JSON: {"price": NUMBER}',
        "messages": [{"role": "user", "content": "Current XAU/USD gold spot price now?"}],
    })
    text = first_text(blocks)
    try:
        p = float(json.loads(text).get("price", 0))
        if 1000 < p < 5000:
            return p
    except Exception:
        pass
    m = re.search(r"(\d{3,4}(?:\.\d+)?)", text)
    if m:
        v = float(m.group(1))
        if 1000 < v < 5000:
            return v
    return None

# ─── Fundamental News ─────────────────────────────────────

async def fetch_news() -> list:
    blocks = await claude({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 500,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "system": (
            'Search latest gold market news. Return ONLY JSON array of 3: '
            '[{"title":"...","impact":"BULLISH"|"BEARISH"|"NEUTRAL","summary":"one sentence"}]'
        ),
        "messages": [{"role": "user", "content": "Latest XAU gold news today: Fed, USD, CPI, geopolitics."}],
    })
    try:
        arr = json.loads(first_text(blocks))
        if isinstance(arr, list):
            return arr[:3]
    except Exception:
        pass
    return []

# ─── Multi-Timeframe AI Analysis ──────────────────────────

async def analyze_mtf(price: float, news: list) -> Optional[dict]:
    """Full multi-timeframe analysis with scoring."""

    # Build indicator data for all timeframes
    atr_15m = calc_atr(prices_15m) or 2.0
    atr_4h  = calc_atr(prices_4h)  or 5.0

    fib_15m = calc_fib(prices_15m) if len(prices_15m) >= 10 else {}
    fib_4h  = calc_fib(prices_4h)  if len(prices_4h)  >= 10 else {}
    fib_1d  = calc_fib(prices_1d)  if len(prices_1d)  >= 5  else {}

    rsi_15m = calc_rsi(prices_15m, 14)
    rsi_4h  = calc_rsi(prices_4h,  14)
    rsi_1d  = calc_rsi(prices_1d,  14)

    macd_15m = calc_macd(prices_15m)
    macd_4h  = calc_macd(prices_4h)

    bb_15m = calc_bollinger(prices_15m)
    bb_4h  = calc_bollinger(prices_4h)

    ma20_15m  = calc_ma(prices_15m, 20)
    ma50_15m  = calc_ma(prices_15m, 50)
    ma200_15m = calc_ma(prices_15m, 100)  # proxy for 200 on 15m
    ma20_4h   = calc_ma(prices_4h,  20)
    ma50_4h   = calc_ma(prices_4h,  50)
    ma200_4h  = calc_ma(prices_4h,  100)
    ma20_1d   = calc_ma(prices_1d,  10)
    ma50_1d   = calc_ma(prices_1d,  20)

    rsi_div  = calc_rsi_divergence(prices_15m)
    sr       = calc_support_resistance(prices_15m)
    news_str = "; ".join(f"{n['title']}: {n['impact']}" for n in news) if news else "N/A"

    # Determine fib zone
    def fib_zone(p, fib):
        if not fib: return "Unknown zone"
        if p <= fib.get("fib382", 0) + 1:    return "0.382 support zone"
        if p <= fib.get("fib500", 0) + 1:    return "0.500 support zone"
        if p >= fib.get("fib618", 0) - 1:    return "0.618 resistance zone"
        if p >= fib.get("fib786", 0) - 1:    return "0.786 resistance zone"
        return "0.500 neutral zone"

    fz_15m = fib_zone(price, fib_15m)
    fz_4h  = fib_zone(price, fib_4h)
    fz_1d  = fib_zone(price, fib_1d)

    prompt = f"""
XAU/USD Multi-Timeframe Analysis Request:

PRICE: ${price}
ATR(14) 15M: {atr_15m} | ATR(14) 4H: {atr_4h}

=== 15-MINUTE TIMEFRAME ===
RSI: {rsi_15m} | RSI Divergence: {rsi_div}
MACD: {macd_15m['macd']} | Signal: {macd_15m['signal']} | Cross: {macd_15m['cross']}
BB Upper: {bb_15m['upper']} | Middle: {bb_15m['middle']} | Lower: {bb_15m['lower']} | Position: {bb_15m['position']}
MA20: {ma20_15m} | MA50: {ma50_15m} | MA200: {ma200_15m}
Fib Zone: {fz_15m}
Fib Levels: {fib_15m}

=== 4-HOUR TIMEFRAME ===
RSI: {rsi_4h}
MACD Cross: {macd_4h['cross']}
BB Position: {bb_4h['position']}
MA20: {ma20_4h} | MA50: {ma50_4h} | MA200: {ma200_4h}
Fib Zone: {fz_4h}

=== DAILY TIMEFRAME ===
RSI: {rsi_1d}
MA20: {ma20_1d} | MA50: {ma50_1d}
Fib Zone: {fz_1d}

=== SUPPORT & RESISTANCE ===
Support: {sr['support']} | Resistance: {sr['resistance']}

=== FUNDAMENTALS ===
{news_str}

Analyze confluence across all 3 timeframes. 
If 2+ timeframes agree → STRONG confluence.
If 1 timeframe agrees → MODERATE confluence.
"""

    system = """You are an elite XAU/USD gold trader with 20 years experience.
Analyze the multi-timeframe data and return ONLY this JSON:
{
  "signal": "BUY"|"SELL"|"HOLD",
  "primary_tf": "15M"|"4H"|"DAILY",
  "mtf_alignment": "STRONG"|"MODERATE"|"WEAK",
  "fib_zone": "describe the key fib level",
  "macd_cross": "BULLISH"|"BEARISH"|"NONE",
  "bb_position": "ABOVE_UPPER"|"BELOW_LOWER"|"UPPER_HALF"|"LOWER_HALF"|"MIDDLE",
  "trend_15m": "BULLISH"|"BEARISH"|"NEUTRAL",
  "trend_4h": "BULLISH"|"BEARISH"|"NEUTRAL",
  "trend_1d": "BULLISH"|"BEARISH"|"NEUTRAL",
  "fundamental_bias": "BULLISH"|"BEARISH"|"NEUTRAL",
  "key_level": number,
  "reasoning": "3-4 sentences: explain the confluence across timeframes, key indicator signals, and why this setup has edge"
}"""

    blocks = await claude({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    })

    try:
        analysis = json.loads(first_text(blocks))
    except Exception as e:
        log.error(f"Analysis parse error: {e}")
        return None

    # Build tf_data for scorer
    tf_data = {
        "price":        price,
        "rsi_15m":      rsi_15m,
        "rsi_div":      rsi_div,
        "macd_cross":   analysis.get("macd_cross", "NONE"),
        "ma20":         ma20_15m,
        "ma50":         ma50_15m,
        "ma200":        ma200_15m,
        "bb_position":  analysis.get("bb_position", "MIDDLE"),
        "fib_zone":     analysis.get("fib_zone", "neutral zone"),
        "mtf_alignment":analysis.get("mtf_alignment", "WEAK"),
    }

    # Score the signal
    signal    = analysis.get("signal", "HOLD")
    scored    = score_signal(signal, tf_data)
    atr_use   = atr_4h if signal != "HOLD" else atr_15m
    levels    = calc_dynamic_levels(signal, price, atr_use)

    analysis["score"]      = scored["score"]
    analysis["breakdown"]  = scored["breakdown"]
    analysis["levels"]     = levels
    analysis["atr"]        = atr_use
    analysis["entry"]      = price
    analysis["rsi_15m"]    = rsi_15m
    analysis["rsi_4h"]     = rsi_4h
    analysis["rsi_1d"]     = rsi_1d
    analysis["macd_15m"]   = macd_15m
    analysis["bb_15m"]     = bb_15m
    analysis["fib_15m"]    = fib_15m

    return analysis

# ─── Message Formatter ────────────────────────────────────

def esc(s: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, f"\\{ch}")
    return s

def score_bar(score: int) -> str:
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)

def trend_emoji(t: str) -> str:
    return {"BULLISH": "📈", "BEARISH": "📉", "NEUTRAL": "➡️"}.get(t, "➡️")

def format_message(price: float, a: dict, news: list) -> str:
    sig    = a.get("signal", "HOLD")
    score  = a.get("score", 0)
    levels = a.get("levels", {})
    now    = datetime.now(timezone.utc).strftime("%Y\\-%m\\-%d %H:%M UTC")

    sig_map = {"BUY": ("🟢", "BUY ▲"), "SELL": ("🔴", "SELL ▼"), "HOLD": ("🟡", "HOLD ●")}
    se, sk  = sig_map.get(sig, ("🟡", "HOLD ●"))

    fund_map = {"BULLISH": "🟢 Bullish", "BEARISH": "🔴 Bearish", "NEUTRAL": "🟡 Neutral"}
    mtf_map  = {"STRONG": "🔥 STRONG", "MODERATE": "⚡ MODERATE", "WEAK": "💤 WEAK"}

    lines = [
        f"{se} *XAU/USD PRO Signal*",
        f"🕐 `{now}`",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 *Signal:*   `{sk}`",
        f"💰 *Price:*    `${price:.2f}`",
        f"🏆 *Score:*    `{score}/100`  `{score_bar(score)}`",
        f"🔗 *MTF:*      `{mtf_map.get(a.get('mtf_alignment','WEAK'), '💤 WEAK')}`",
        "",
    ]

    if sig != "HOLD" and levels:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━",
            f"📐 *Trade Levels \\(ATR\\-based\\)*",
            f"🔵 Entry:   `${price:.2f}`",
            f"🔴 SL:      `${levels['sl']:.2f}`  \\({levels['sl_pips']} pips\\)",
            f"🟡 TP1:     `${levels['tp1']:.2f}`  \\({levels['tp1_pips']} pips\\)  `{levels['rr1']}`",
            f"🟢 TP2:     `${levels['tp2']:.2f}`  \\({levels['tp2_pips']} pips\\)  `{levels['rr2']}`",
            f"💎 TP3:     `${levels['tp3']:.2f}`  \\({levels['tp3_pips']} pips\\)  `{levels['rr3']}`",
            f"📏 ATR:     `${a.get('atr', 0):.2f}`",
            "",
        ]

    # Timeframe breakdown
    t15 = a.get("trend_15m", "NEUTRAL")
    t4h = a.get("trend_4h",  "NEUTRAL")
    t1d = a.get("trend_1d",  "NEUTRAL")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        "⏱ *Timeframe Confluence*",
        f"{trend_emoji(t15)} 15M:   `{t15}`",
        f"{trend_emoji(t4h)} 4H:    `{t4h}`",
        f"{trend_emoji(t1d)} Daily: `{t1d}`",
        "",
    ]

    # Indicators
    rsi15 = a.get("rsi_15m")
    rsi4h = a.get("rsi_4h")
    rsi1d = a.get("rsi_1d")
    bb    = a.get("bb_15m", {})
    macd  = a.get("macd_15m", {})
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        "🔬 *Indicators*",
        f"📈 RSI 15M:   `{rsi15 or '—'}`",
        f"📈 RSI 4H:    `{rsi4h or '—'}`",
        f"📈 RSI Daily: `{rsi1d or '—'}`",
        f"📉 MACD:      `{macd.get('cross','—')}`",
        f"〰️  BB:        `{esc(bb.get('position','—'))}`",
        f"📐 Fib Zone:  `{esc(str(a.get('fib_zone','—')))}`",
        "",
    ]

    # Score breakdown
    breakdown = a.get("breakdown", {})
    if breakdown:
        lines += ["━━━━━━━━━━━━━━━━━━━━", "🏆 *Score Breakdown*"]
        for k, v in breakdown.items():
            lines.append(f"• {esc(k)}: `{esc(v)}`")
        lines.append("")

    # Fundamentals
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        f"🌍 *Fundamental:* {fund_map.get(a.get('fundamental_bias','NEUTRAL'), '🟡 Neutral')}",
    ]
    if news:
        lines.append("")
        for n in news[:3]:
            imp = n.get("impact", "NEUTRAL")
            em  = "🟢" if imp == "BULLISH" else "🔴" if imp == "BEARISH" else "🟡"
            lines.append(f"{em} {esc(n.get('summary',''))}")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "🧠 *AI Analysis*",
        f"_{esc(a.get('reasoning',''))}_",
        "",
        f"⚠️ _Score threshold: {MIN_SCORE}/100 \\| Only high\\-confidence signals sent_",
        f"🤖 _XAU PRO Bot v2\\.0 \\| 15M\\+4H\\+Daily confluence_",
    ]
    return "\n".join(lines)

# ─── Telegram Send ────────────────────────────────────────

bot = Bot(token=TELEGRAM_TOKEN)

async def send(text: str):
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        log.info("✅ Signal sent")
    except Exception as e:
        log.warning(f"MarkdownV2 failed: {e} — retrying plain")
        plain = re.sub(r"[*_`\[\]()~>#+=|{}.!\\]", "", text)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=plain)

# ─── Main Job ─────────────────────────────────────────────

async def run_job():
    global tick_count
    log.info(f"⏱ Signal cycle #{tick_count}")

    price = await fetch_live_price()
    if not price:
        log.warning("No price — skip")
        return

    # Update price history buckets
    prices_15m.append(price)
    if len(prices_15m) > 200: prices_15m.pop(0)

    tick_count += 1
    # Every 16 ticks (~4 hours at 15min interval) → add to 4H bucket
    if tick_count % 16 == 0:
        prices_4h.append(price)
        if len(prices_4h) > 100: prices_4h.pop(0)
    # Every 96 ticks (~1 day at 15min interval) → add to Daily bucket
    if tick_count % 96 == 0:
        prices_1d.append(price)
        if len(prices_1d) > 60: prices_1d.pop(0)

    # Need at least some history for meaningful analysis
    if len(prices_15m) < 5:
        log.info(f"Building price history... ({len(prices_15m)}/5 minimum)")
        return

    news     = await fetch_news()
    analysis = await analyze_mtf(price, news)
    if not analysis:
        return

    sig   = analysis.get("signal", "HOLD")
    score = analysis.get("score", 0)
    log.info(f"→ {sig} | Score: {score}/100 | MTF: {analysis.get('mtf_alignment')} | Price: ${price}")

    # Only send if score >= minimum threshold
    if sig == "HOLD":
        log.info(f"HOLD — not sending")
        return
    if score < MIN_SCORE:
        log.info(f"Score {score} below threshold {MIN_SCORE} — not sending")
        return

    await send(format_message(price, analysis, news))

# ─── Entry Point ──────────────────────────────────────────

async def main():
    log.info(f"🚀 XAU PRO Bot v2.0 starting | interval={SIGNAL_INTERVAL}min | min_score={MIN_SCORE}")

    startup = (
        "🏅 *XAU\\/USD PRO Signal Bot v2\\.0 is LIVE\\!*\n\n"
        "🔬 *Indicators:*\n"
        "• Fibonacci \\+ Extensions\n"
        "• MA20 \\/ MA50 \\/ MA200\n"
        "• RSI \\+ Divergence Detection\n"
        "• MACD Crossover\n"
        "• Bollinger Bands\n"
        "• Dynamic ATR\\-based SL\\/TP\n\n"
        "⏱ *Timeframes:* 15M \\+ 4H \\+ Daily confluence\n"
        f"🏆 *Min score to send:* `{MIN_SCORE}/100`\n"
        "💎 *3 TP levels* per signal\n\n"
        "⚠️ _Advanced analysis only — no weak signals_"
    )
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=startup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        log.warning(f"Startup msg failed: {e}")

    await run_job()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_job, "interval", minutes=SIGNAL_INTERVAL)
    scheduler.start()
    log.info(f"✅ Scheduler running every {SIGNAL_INTERVAL} min")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
