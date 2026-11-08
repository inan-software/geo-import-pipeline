"""Source adapters. Importing this package registers the built-in types."""

from . import wfs  # noqa: F401
from .base import SourceAdapter, adapter_for, register

__all__ = ["SourceAdapter", "adapter_for", "register"]
