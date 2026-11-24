"""Normalization and validation: every source maps into one canonical model.

Per-source mappers (`osm`, `ridb`, `nps`) only *extract* values into a `Draft`. Shared
normalizers (`names`, `contact`, `geo`, `category`, `season`) clean and validate them and
emit `Flag`s. Nothing is fixed silently: every rule violation becomes a flag.
"""
