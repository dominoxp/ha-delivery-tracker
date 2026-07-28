"""Datenquellen der Integration (IMAP, APIs).

Die konkreten Quellen kommen in Step 1.1 (IMAP) und 3.1 (17track) dazu; hier
steht nur die gemeinsame Schnittstelle.
"""

from __future__ import annotations

from .base import TrackingSource

__all__ = ["TrackingSource"]
