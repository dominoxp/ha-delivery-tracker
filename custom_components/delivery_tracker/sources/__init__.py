"""Datenquellen der Integration (IMAP, APIs).

Die gemeinsame Schnittstelle steht in `base`, die erste echte Quelle in
`imap_source`; die 17track-Quelle kommt in Step 3.1 dazu.
"""

from __future__ import annotations

from .base import TrackingSource
from .imap_source import (
    ImapAuthError,
    ImapConfig,
    ImapConnectionError,
    ImapError,
    ImapFolderError,
    ImapMode,
    ImapPermanentError,
    ImapProgress,
    ImapSecurity,
    ImapSource,
    ImapState,
    MailFolder,
    MailHandler,
    RawMail,
)

__all__ = [
    "ImapAuthError",
    "ImapConfig",
    "ImapConnectionError",
    "ImapError",
    "ImapFolderError",
    "ImapMode",
    "ImapPermanentError",
    "ImapProgress",
    "ImapSecurity",
    "ImapSource",
    "ImapState",
    "MailFolder",
    "MailHandler",
    "RawMail",
    "TrackingSource",
]
