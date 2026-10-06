"""Datenquellen der Integration (IMAP, APIs).

Die gemeinsame Schnittstelle steht in `base`, die erste echte Quelle in
`imap_source`; die 17track-Quelle kommt in Step 3.1 dazu und traegt sich in
`API_SOURCE_FACTORIES` ein.
"""

from __future__ import annotations

from .base import (
    API_SOURCE_FACTORIES,
    ApiSourceFactory,
    SourceAuthError,
    SourceError,
    SourcePermanentError,
    SourceQuotaError,
    TrackingSource,
)
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
    "API_SOURCE_FACTORIES",
    "ApiSourceFactory",
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
    "SourceAuthError",
    "SourceError",
    "SourcePermanentError",
    "SourceQuotaError",
    "TrackingSource",
]
