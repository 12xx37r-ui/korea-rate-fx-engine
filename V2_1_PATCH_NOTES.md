# Korea V2.1 validation patch

- U.S. engine remains read-only and unchanged.
- Korea V2 rejects implausible meeting reconstruction spikes and prefers monthly ZQ/SOFR contract averages.
- Data coverage now counts KRX and REB as missing when they are not configured.
- Rate quality gate strengthened: 72 samples, Brier skill >= 8%, accuracy >= 52%, coverage >= 85%, plus vintage-backtest disclosure.
- FX quality gate no longer calls RMSE-only validation quasi-institutional. It also requires direction accuracy, persistence/random-walk skill, interval coverage, and horizon-specific OOS validation.
- Collector progress logging and 30-minute workflow timeout included.
