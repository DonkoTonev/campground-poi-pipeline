"""LLM extraction with a cost ladder: skip → deterministic → cheap model → strong model → human.

The LLM layer is provider-agnostic (`poi.extract.llm`). Every call is recorded in the
`llm_calls` ledger; every value is grounded against the input before it is trusted.
"""
