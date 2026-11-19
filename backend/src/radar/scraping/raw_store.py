"""Content-addressed store for raw HTML: data/raw/ab/abcdef....html.gz

The same page content is only ever stored once, and keeping the raw page means a parser
fix can be replayed over history (`radar reprocess`) without re-fetching anything.
Swapping this for S3 later only touches this file.
"""

import gzip
import hashlib
import time
from collections.abc import Iterator
from pathlib import Path

SUFFIX = ".html.gz"


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        path = self._path(digest)
        if path.exists():
            # Refresh the age even when the content is already stored, so the orphan purge
            # (which spares recent files) can't delete it before its observation is written.
            path.touch()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(content))
            tmp.replace(path)
        return digest

    def get(self, digest: str) -> bytes | None:
        path = self._path(digest)
        return gzip.decompress(path.read_bytes()) if path.exists() else None

    def delete(self, digest: str) -> bool:
        path = self._path(digest)
        if not path.exists():
            return False
        path.unlink()
        return True

    def older_than(self, seconds: float) -> Iterator[str]:
        """Digests of stored pages last written more than `seconds` ago."""
        if not self.root.exists():
            return
        cutoff = time.time() - seconds
        for path in self.root.glob(f"*/*{SUFFIX}"):
            if path.stat().st_mtime < cutoff:
                yield path.name.removesuffix(SUFFIX)

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / f"{digest}{SUFFIX}"
