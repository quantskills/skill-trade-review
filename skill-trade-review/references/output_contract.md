# Output Contract

## Required top-level output columns

| Field | Required | Notes |
|---|---|---|
| `trade_date` | yes | review date |
| `build_id` | yes | fixed `R00` |
| `build_name` | yes | fixed `交易复盘` |
| `target_id` | yes | account id or `default` |
| `result_type` | yes | `daily_review` or `range_review` |
| `result_value` | yes | five-level rating |
| `result_json` | yes | valid JSON string |
| `data_version` | yes | current `real-v1` |
| `update_time` | yes | ISO timestamp |

## Required `result_json` keys

| Key | Required |
|---|---|
| `period` | yes |
| `summary` | yes |
| `attribution` | yes |
| `market_review` | yes |
| `trade_reviews` | yes |
| `patterns` | yes |
| `advice` | yes |
| `narrative_md` | yes |
| `warnings` | yes |

## Acceptance

- `result_json` must parse successfully.
- `result_value` must be one of `excellent`, `good`, `neutral`, `poor`, `bad`.
- `trade_reviews` must distinguish `closed` and `open`.
- `advice` must include `actions_now`, `actions_next_session`, `process_changes`, `flat`.

