"""Raw blob storage behind one interface: local filesystem (default) or S3."""

from poi.storage.base import RawStore, make_store

__all__ = ["RawStore", "make_store"]
