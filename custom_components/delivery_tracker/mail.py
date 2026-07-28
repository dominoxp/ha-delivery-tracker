"""Datencontainer fuer eine aufbereitete E-Mail.

Hier steht bewusst nur der Container: Das Zerlegen echter MIME-Nachrichten,
HTML-nach-Text und Encoding-Reparatur folgen in Step 1.2. Carrier-Plugins sehen
immer nur diesen fertig aufbereiteten Blick auf eine Mail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import ensure_aware


@dataclass(frozen=True, slots=True)
class MailMessage:
    """Eine Mail, so wie Carrier-Plugins sie zu sehen bekommen."""

    message_id: str
    subject: str = ""
    text: str = ""
    """Body als reiner Text — HTML ist bereits konvertiert."""
    html: str | None = None
    """Original-HTML, falls vorhanden (fuer Plugins, die Links brauchen)."""
    from_address: str = ""
    from_name: str | None = None
    to_addresses: tuple[str, ...] = ()
    date: datetime | None = None

    def __post_init__(self) -> None:
        """Zeitstempel normalisieren (das Objekt ist frozen, daher ueber `object`)."""
        if self.date is not None:
            object.__setattr__(self, "date", ensure_aware(self.date, field_name="MailMessage.date"))

    @property
    def from_domain(self) -> str:
        """Domain des Absenders in Kleinbuchstaben, leer wenn unbekannt."""
        _, separator, domain = self.from_address.rpartition("@")
        if not separator:
            return ""
        return domain.strip().strip(">").lower()

    @property
    def searchable_text(self) -> str:
        """Betreff und Body als ein Text — die Grundlage aller Regex-Regeln."""
        return f"{self.subject}\n{self.text}"
