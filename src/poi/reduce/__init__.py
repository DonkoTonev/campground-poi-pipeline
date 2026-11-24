"""Content reduction: raw HTML → cleaned HTML → Markdown → text, plus deterministic facts.

Everything here is free (no LLM). Its job is to make the LLM input small and relevant, and to
measure how small: token counts per representation are recorded for every page.
"""
