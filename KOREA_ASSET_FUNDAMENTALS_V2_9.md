# Korea asset fundamentals V2.9

- KRX direct unauthenticated POST removed.
- Primary: pykrx 1.2.8 KRX authenticated/session collector.
- Optional GitHub Secrets: `KRX_ID`, `KRX_PW`.
- Secondary: INDEXerGO public KRX/KOFIA republished index metrics.
- REIT: 329200 trailing-12-month cash distributions from FnGuide divided by current NAVER price.
- Last-good reuse remains explicit with `stale=true`.
- Output: `output/korea_asset_fundamentals.json`, schema 1.1.0.
