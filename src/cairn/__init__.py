"""cairn — local-first shared memory for CLI agents."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cairn")
except PackageNotFoundError:  # source tree without install
    __version__ = "0.0.0-dev"

from .client import CairnClient
from .embed import Embedder, get_embedder
from .models import MemoryRecord, Origin, Status, StoreAction

__all__ = [
    "CairnClient",
    "Embedder",
    "MemoryRecord",
    "Origin",
    "Status",
    "StoreAction",
    "__version__",
    "get_embedder",
]
