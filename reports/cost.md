# Cost report

Region `sierra`, generated 2026-11-25 from the `llm_calls` ledger.

Live extraction calls in the ledger: provider(s) anthropic. Billed amounts are list prices from `config/pricing.yaml`; the Haiku column prices the same token counts on Claude Haiku 5.5 without caching (a reference point for Claude calls, an estimate for other providers).

## Calls

| provider | model | step | calls | input tokens | output tokens | thinking | billed | est. on Haiku | avg latency |
|---|---|---|---|---|---|---|---|---|---|
| anthropic | claude-sonnet-5-5 | strong | 20 | 140,816 | 10,544 | 0 | $0.2515 | $0.0198 | 3,629 ms |
| anthropic | claude-haiku-5-5 | cheap | 115 | 755,704 | 60,224 | 0 | $0.0639 | $0.1057 | 2,147 ms |

All successful live calls, including runs whose extractions were later replaced. Failed calls (not billed, no tokens): 200 in 1 run(s).

Escalation rate (current extractions): 17 of 100 documents (17%) went to the strong model. Cache hits: 60% of input tokens were read from the provider's cache.

## By input (provenance)

Calls behind each document's current extraction.

| input | documents | input tokens / doc | output tokens / doc | est. Haiku $ / doc | escalations |
|---|---|---|---|---|---|
| ridb_description | 85 | 5,987 | 450 | $0.000824 | 14 |
| web_bundle | 15 | 13,078 | 1,038 | $0.001827 | 3 |

## Outcomes

| input | status | answer from | documents | mean hallucination rate |
|---|---|---|---|---|
| ridb_description | needs_review | review | 5 | 80.0% |
| ridb_description | ok | cheap | 71 | 0.0% |
| ridb_description | ok | strong | 9 | 0.0% |
| web_bundle | needs_review | review | 1 | 66.7% |
| web_bundle | ok | cheap | 12 | 0.6% |
| web_bundle | ok | strong | 2 | 0.0% |

Which ladder step produced each extracted value:

| method | values |
|---|---|
| llm | 579 |

Est. Claude Haiku cost per 1,000 extracted campgrounds: $1.2061 (104 campgrounds extracted).

## Extrapolation to 1M pages (estimate)

Per-page tokens by representation from the reduce stage (bytes/4 estimates), plus the measured average prompt overhead per request (4,595 tokens: instructions, schema, wrapper) and output (707 tokens per request), priced on Claude Haiku 5.5. One page per request; bundling pages per site pays the overhead once per site instead.

| representation | tokens / page | 1M pages, Haiku | 1M pages, Haiku batch |
|---|---|---|---|
| raw HTML | 36,437 | $4,457 | $2,228 |
| cleaned HTML | 2,133 | $1,026 | $513 |
| Markdown | 963 | $909 | $455 |
| plain text | 943 | $907 | $454 |

Raw HTML pages average more than 36k tokens; at that size many real pages cross Haiku's 100k-token long-context threshold when bundled, which this table does not even count.
