# Trade Review

[简体中文](README.md) | **English**

> Turn trade records into a full review result: validation, market context, attribution, per-trade review, pattern detection, advice, and optional LLM narrative.

## What this is

`trade-review` is a self-contained skill for daily, range-based, strategy-level, or account-level trade retrospective analysis.

## Structure

```text
skill-trade-review/
├─ agents/
├─ references/
├─ scripts/
├─ LICENSE
├─ README.md
├─ README.en.md
├─ requirements.txt
└─ SKILL.md
```

## Main outputs

| Module | Content |
|---|---|
| `summary` | win rate, pnl, exposure, profit factor |
| `attribution` | attribution by asset, direction, account, variety, tag |
| `market_review` | stock and futures context review |
| `trade_reviews` | per-trade review |
| `patterns` | mistake pattern detection |
| `advice` | actionable advice |
| `narrative_md` | readable markdown review |

## Quick start

```bash
python scripts/test.py
python scripts/review.py --trades trades.csv --mode daily --date 2026-06-04
```

