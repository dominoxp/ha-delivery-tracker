"""Datenquellen der Integration (IMAP, APIs).

Die gemeinsame Schnittstelle steht in `base`, das Postfach in `imap_source`.
API-Quellen wie `seventeentrack_source` (kostenpflichtige API) und
`seventeentrack_account_source` (kostenloses Benutzerkonto) tragen sich beim
Import in `API_SOURCE_FACTORIES` ein.
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
    TrackingReport,
    TrackingSource,
    TrackingState,
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
from .seventeentrack_account_source import SeventeenTrackAccountSource
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
    "SeventeenTrackAccountSource",
    "SeventeenTrackSource",
    "SourceAuthError",
    "SourceContext",
    "SourceError",
    "SourcePermanentError",
    "SourceQuotaError",
    "SourceRateLimitError",
    "TrackingReport",
    "TrackingSource",
    "TrackingState",
]
