# BFXPS Smart Advisor V8.30 - OHLC Zoom Chart Database Truth

Strict rootless runtime. Chart presentation requests are isolated from execution advice.

- `kèo trên chart`, `đặt kèo lên chart`, `chart kèo`, `biểu đồ kèo` use `PLAN_CHART`.
- The answer lists every forward row from `outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv`.
- OHLC supplied in the same question is overlaid on the chart.
- The chart response does not invent fill, failed-breakout, retest, invalidation or R5 rules.
- Execution advice is produced only when the customer explicitly asks what to do, whether to enter, fill status, size, R5 or risk.


## V8.30 OHLC zoom renderer
- Chart requests render a real OHLC candle with Python/Matplotlib.
- Entry/TP overlays come only from BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv.
- Performance charts come only from BEST_ENGINE_RECENT_TRADES.tsv.
- Chart-only requests do not trigger execution advice or invent fill/retest/invalidation rules.
