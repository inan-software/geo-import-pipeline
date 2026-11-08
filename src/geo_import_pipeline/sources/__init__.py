"""Source adapters. Importing this package registers the built-in types."""

from . import csv, wfs  # noqa: F401
from .base import SourceAdapter, adapter_for, load_plugins, register

__all__ = ["SourceAdapter", "adapter_for", "load_plugins", "register"]
