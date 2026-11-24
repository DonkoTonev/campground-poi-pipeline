# Token report

Region `sierra`, reduce run 30 (2026-11-24), generated 2026-11-24.

Token counts are **estimates** (`estimate:utf8_bytes/4`); the extraction stage
calibrates them against the API's `count_tokens` endpoint.

## Per page (76 crawled HTML pages)

| representation | total tokens | per page | vs raw HTML |
|---|---|---|---|
| raw HTML | 2,769,231 | 36,437 | 100.0% |
| cleaned HTML | 162,160 | 2,133 | 5.9% |
| Markdown (+ salvaged facts) | 71,995 | 947 | 2.6% |
| plain text | 70,491 | 927 | 2.5% |

Facts surviving reduction (distinct values in the whole cleaned page vs. in the final
Markdown; a free proxy, the LLM evaluation measures real accuracy):

| fact | in page | kept | share |
|---|---|---|---|
| price | 148 | 144 | 97% |
| phone | 65 | 63 | 97% |
| month | 173 | 166 | 96% |

## LLM input documents

| kind | documents | avg tokens | max tokens | total tokens |
|---|---|---|---|---|
| ridb_description | 85 | 760 | 1,942 | 64,595 |
| web_bundle | 15 | 4,290 | 11,410 | 64,353 |

Site bundles: 71,995 tokens of page Markdown became 64,353 after removing 311 repeated lines (menus, footers); 1 of 15 bundles hit the 12,000-token cap and were cut by relevance.

Deterministic facts found without any LLM: 109 via jsonld, 24 via mailto_link, 24 via microdata, 49 via tel_link.
