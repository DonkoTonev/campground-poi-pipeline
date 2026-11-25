# Representation ablation

Same gold cases, cheap model only (claude-haiku-5-5, provider `anthropic`), one call per case and representation, 2026-11-25. The input tokens are the provider's real counts (instructions included); the Haiku column prices the same token counts on Claude Haiku 5.5 and is an estimate.

| representation | input tokens / page | field accuracy | page kind | bait violations | est. Haiku $ / page |
|---|---|---|---|---|---|
| raw_html | 5,876 | 94% | 94% | 0 | $0.000681 |
| cleaned_html | 5,855 | 94% | 94% | 0 | $0.000680 |
| markdown | 5,010 | 94% | 94% | 0 | $0.000592 |
| text | 5,641 | 94% | 100% | 0 | $0.000654 |

Synthetic pages are small and clean, so raw HTML is far less bloated here than on real
sites (real pages average about 36,000 estimated tokens of raw HTML; see
reports/tokens.md). The token ratio on real pages is the one that matters for cost.
