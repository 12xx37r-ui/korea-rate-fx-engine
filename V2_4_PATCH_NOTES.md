# V2.4 selective FX forecast

- U.S. engine remains read-only and unchanged.
- Replaced the always-on USD/KRW level model with a fixed-spec selective model.
- Signal: 60-observation USD/KRW return.
- Activation: absolute signal >= 3%.
- Forecast: 35% shrunken contrarian adjustment, capped at +/-6%.
- Weak signal: explicit abstention and no-change random-walk center.
- Validation: expanding walk-forward weekly origins for 1/3/6/12 months.
- Reports both all-origin direction accuracy and active-call direction accuracy.
- Quality gate additionally requires active signal coverage >= 30%.
- Production automatically falls back to random walk if 3-month OOS skill, active-call direction, or signal coverage fails.
