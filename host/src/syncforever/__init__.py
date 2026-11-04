"""Sync Forever: the box that keeps EverDrive PRO carts in step with your own storage."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("syncforever")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0"
