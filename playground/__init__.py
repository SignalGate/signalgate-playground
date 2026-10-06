"""Playground: an interactive console demo."""

from __future__ import annotations

from importlib import metadata

try:
    CLIENT_VERSION = metadata.version("signalgate-playground")
except metadata.PackageNotFoundError:  # a plain source checkout, not installed
    CLIENT_VERSION = "0.0.1"

__all__ = ["CLIENT_VERSION"]
