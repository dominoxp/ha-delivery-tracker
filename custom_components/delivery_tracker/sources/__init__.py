"""Datenquellen der Integration (IMAP, APIs).

Die gemeinsame Schnittstelle steht in `base`, das Postfach in `imap_source`.
API-Quellen wie `seventeentrack_source` tragen sich beim Import in
`API_SOURCE_FACTORIES` ein.
"""

from __future__ import annotations

from .base import (
    API_SOURCE_FACTORIES,
    ApiSourceFactory,
    SourceAuthError,
    SourceContext,
    SourceError,
    SourcePermanentError,
    SourceQuotaError,
    SourceRateLimitError,
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
from .seventeentrack_source import SeventeenTrackSource

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
    "SeventeenTrackSource",
    "SourceAuthError",
    "SourceContext",
    "SourceError",
    "SourcePermanentError",
    "SourceQuotaError",
    "SourceRateLimitError",
    "TrackingSource",
]
