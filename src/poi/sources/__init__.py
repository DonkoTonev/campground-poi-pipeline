"""Source adapters. Each fetches through `poi.fetch` (raw store first) and yields records."""

from poi.sources.base import Source, SourceRecord
from poi.sources.nps import NpsSource
from poi.sources.osm import OsmSource
from poi.sources.ridb import RidbSource

ALL_SOURCES: dict[str, Source] = {s.name: s for s in (OsmSource(), RidbSource(), NpsSource())}

__all__ = ["ALL_SOURCES", "Source", "SourceRecord"]
