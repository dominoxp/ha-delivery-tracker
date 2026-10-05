"""Ein Verzeichnis `.eml` durch die echte Pipeline schicken und zeigen, was herauskommt.

    docker run --rm -v "%CD%:/app" -w /app -e PYTHONPATH=/app ha-delivery-tracker-test \
        python tools/inspect_mails.py tools/emails

Braucht Home Assistant und laeuft deshalb im Test-Image. Das Skript aendert
nichts — es ist die schnellste Art zu sehen, welche Mailtypen ein Plugin noch
nicht versteht.
"""

from __future__ import annotations

import sys
from pathlib import Path

from custom_components.delivery_tracker.carriers import CarrierRegistry, discover_plugins
from custom_components.delivery_tracker.mail import parse_mail

CARRIERS = Path("custom_components/delivery_tracker/carriers")
CARRIERS_PACKAGE = "custom_components.delivery_tracker.carriers"

FIELDS = (
    "tracking_number",
    "status",
    "status_text",
    "eta_start",
    "eta_end",
    "delivered_at",
    "sender",
    "description",
    "order_number",
    "pickup_location",
    "pickup_code",
    "destination_raw",
    "tracking_url",
)


def main(argv: list[str]) -> int:
    """Alle Mails unter dem uebergebenen Pfad auswerten."""
    source = Path(argv[1] if len(argv) > 1 else "tools/emails")
    registry = CarrierRegistry(discover_plugins(CARRIERS, package=CARRIERS_PACKAGE))
    print(f"Plugins: {', '.join(registry.carrier_ids)}\n")

    for path in sorted(source.rglob("*.eml")):
        mail = parse_mail(path.read_bytes())
        print("=" * 100)
        print(f"{path.name}\n  Von: {mail.from_address}\n  Betreff: {mail.subject}")

        candidates = registry.candidates(mail)
        if not candidates:
            print("  -> KEIN PLUGIN")
            continue

        ranking = ", ".join(f"{plugin.carrier_id} {score}" for plugin, score in candidates)
        resolution = registry.resolve(mail)
        plugin = resolution.plugin
        assert plugin is not None
        print(f"  Kandidaten: {ranking}")
        print(f"  -> {plugin.carrier_id}, {len(resolution.shipments)} Sendung(en)")
        for shipment in resolution.shipments:
            print(f"     id: {shipment.id}")
            for name in FIELDS:
                value = getattr(shipment, name)
                if value is not None:
                    print(f"       {name}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
