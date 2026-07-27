# V2.2 validation implementation

- U.S. engine remains read-only and unchanged.
- Rate validation now uses monthly market-rate forecast origins rather than only policy-rate observation dates.
- CPI and industrial-production release lags are applied in the rate backtest.
- USD/KRW now has actual expanding walk-forward validation for 1, 3, 6 and 12 months.
- FX validation reports RMSE, MAE, direction accuracy, random-walk/persistence skill and 80% interval coverage.
- Quality gates use the newly calculated metrics; thresholds are not lowered.
- A daily `output/vintages/YYYY-MM-DD.json` snapshot is created so genuine real-time vintage validation can accumulate from deployment onward.
- Existing legacy JSON outputs and the U.S. engine are preserved.
