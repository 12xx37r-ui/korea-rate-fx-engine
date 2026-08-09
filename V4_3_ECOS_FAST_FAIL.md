# V4.3 ECOS fast-fail hotfix

- ECOS transport connect timeout: 4s, read timeout: 10s, per-request retries: 1.
- The first ECOS transport/network timeout opens a per-run circuit breaker.
- Remaining ECOS series make zero external calls and reuse committed last-good official history.
- Data-specific/statistical errors do not open the host circuit breaker.
- Existing prediction models remain active; this patch changes collection resilience only.
- GLOBAL_MARKET V4.2 one-batch FRED + one Yahoo request is unchanged.
