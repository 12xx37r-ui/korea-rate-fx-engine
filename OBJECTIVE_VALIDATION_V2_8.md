# V2.8 point-in-time-gated validation

- Korean policy-rate production status requires at least 80 observations, Brier skill >= 10%, accuracy >= 55%, a 95% Wilson lower bound above 50%, and true real-time vintages.
- Release-lag reconstructions are labeled `재구성 OOS 후보`, not institutional-grade.
- USD/KRW horizon gates require active accuracy >= 55%, its Wilson lower bound above 50%, positive random-walk skill of at least 2% (3% at 12m), calibrated interval coverage, and adequate signal coverage.
- Horizons that fail are explicit `참고용/관망`; no forced directional call is made.
- Daily vintages already written by the engine should remain immutable so genuine live OOS evidence can accumulate.
