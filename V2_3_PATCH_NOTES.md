# V2.3 operational safety and history expansion

- ECOS lookback extended from 2,200 to 5,000 days for rate/FX/yield histories.
- FX candidate gate now requires positive random-walk skill and at least 48% direction accuracy.
- A model with non-positive skill or sub-50% direction accuracy is blocked from production.
- Production automatically falls back to a no-change random-walk center with benchmark-derived 80% bands.
- U.S. engine remains read-only and untouched.
