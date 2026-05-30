# XAU/USD PRO Signal Bot v2.0

## What's new vs v1
- Multi-timeframe confluence: 15M + 4H + Daily
- Signal scoring system (0-100) — only sends if score >= 75
- RSI divergence detection
- MACD crossover confirmation
- Bollinger Bands
- MA20 / MA50 / MA200
- Dynamic SL/TP based on ATR (adapts to volatility)
- 3 TP levels per signal

## Environment Variables
| Key | Value |
|-----|-------|
| TELEGRAM_TOKEN | Your bot token |
| TELEGRAM_CHAT_ID | Your group ID |
| ANTHROPIC_API_KEY | Your Claude key |
| SIGNAL_INTERVAL_MINUTES | 15 |
| MIN_SIGNAL_SCORE | 75 |

## Deploy on Railway
Same steps as v1 — connect GitHub repo, add variables, deploy.
