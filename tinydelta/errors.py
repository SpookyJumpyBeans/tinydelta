class TinyDeltaError(Exception):
    """User-facing table error (missing table, bad schema, conflict, etc.)."""


class ConcurrentWriteError(TinyDeltaError):
    def __init__(self, version: int) -> None:
        self.version = version
        super().__init__(
            f"commit {version} already exists; another writer committed first"
        )
