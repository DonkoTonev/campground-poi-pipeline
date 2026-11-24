from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from poi.settings import Settings


class RawStore(Protocol):
    """A dumb byte store keyed by relative path. Compression is the caller's concern."""

    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


def make_store(settings: Settings) -> RawStore:
    if settings.raw_store == "s3":
        from poi.storage.s3 import S3Store

        if not settings.raw_store_s3_bucket:
            raise ValueError("RAW_STORE=s3 requires RAW_STORE_S3_BUCKET")
        return S3Store(settings.raw_store_s3_bucket)
    from poi.storage.local import LocalStore

    return LocalStore(settings.raw_store_path)
