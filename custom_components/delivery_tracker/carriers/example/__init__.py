"""Beispiel-Zusteller — lebende Vorlage fuer eigene Carrier-Plugins.

Ein Plugin besteht aus genau zwei Dingen:

1. diesem `__init__.py`, das unter `PLUGIN` eine `CarrierPlugin`-Instanz bereitstellt,
2. der `rules.yaml` daneben, in der die deklarativen Regeln stehen.

Wer einen Zusteller ergaenzen will, kopiert dieses Verzeichnis, passt die
`rules.yaml` an und ist fertig. Erst wenn die Mails etwas verlangen, das sich
nicht als Regex ausdruecken laesst, wird von `RulesCarrier` abgeleitet:

```python
class MyCarrier(RulesCarrier):
    def parse(self, mail: MailMessage) -> Shipment | None:
        shipment = super().parse(mail)
        if shipment is not None:
            shipment.description = _read_table(mail.html)
        return shipment


PLUGIN = MyCarrier(load_rules(Path(__file__).parent / RULES_FILENAME))
```
"""

from __future__ import annotations

from pathlib import Path

from ..base import RulesCarrier

# Die Registry sucht genau diese Konstante.
PLUGIN = RulesCarrier.from_directory(Path(__file__).parent)
