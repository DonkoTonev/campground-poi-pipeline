"""Record / replay: responses cached by hash(provider, model, instructions, input).

Re-runs, tests and evaluations cost nothing and are reproducible. Recordings of real
documents go to data/cache/llm (gitignored: they quote third-party pages); recordings of the
synthetic gold pages go to eval/recordings/synthetic and are committed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from poi.extract.llm.base import LLMClient, LLMRequest, LLMResponse, Usage

Mode = Literal["replay", "record", "live"]


class MissingRecordingError(LookupError):
    pass


def request_hash(provider: str, request: LLMRequest) -> str:
    canon = json.dumps(
        {
            "provider": provider,
            "model": request.model,
            "system": request.system,
            "user": request.user,
            "schema": request.schema.__name__,
            "max_output_tokens": request.max_output_tokens,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canon.encode()).hexdigest()


class ReplayClient:
    """`replay`: recordings only (never calls the provider). `record`: call when missing.
    `live`: always call and overwrite. Reads from all `read_dirs`, writes to `write_dir`."""

    def __init__(
        self,
        provider: str,
        inner: LLMClient | None,
        mode: Mode,
        write_dir: Path,
        read_dirs: tuple[Path, ...] = (),
    ) -> None:
        self.provider = provider
        self.inner = inner
        self.mode = mode
        self.write_dir = write_dir
        self.read_dirs = (write_dir, *read_dirs)

    def _find(self, key: str) -> Path | None:
        return next(
            (d / f"{key}.json" for d in self.read_dirs if (d / f"{key}.json").exists()), None
        )

    def extract(self, request: LLMRequest) -> LLMResponse:
        key = request_hash(self.provider, request)
        path = self._find(key)
        if path is not None and self.mode != "live":
            data = json.loads(path.read_text())
            data["usage"] = Usage(**data["usage"])
            data["replayed"] = True
            return LLMResponse(**data)
        if self.mode == "replay" or self.inner is None:
            raise MissingRecordingError(f"no recording for {request.key} ({key[:12]})")
        response = self.inner.extract(request)
        if response.outcome != "error":  # transient errors are not worth replaying
            self.write_dir.mkdir(parents=True, exist_ok=True)
            data = asdict(response)
            data["replayed"] = False
            (self.write_dir / f"{key}.json").write_text(json.dumps(data, indent=1, sort_keys=True))
        return response

    def count_tokens(self, request: LLMRequest) -> int:
        if self.inner is None:
            raise MissingRecordingError("token counting needs a provider client")
        return self.inner.count_tokens(request)
