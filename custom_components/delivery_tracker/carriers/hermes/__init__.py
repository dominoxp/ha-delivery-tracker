"""Hermes — vollstaendig ueber `rules.yaml` beschrieben."""

from __future__ import annotations

from pathlib import Path

from ..base import RulesCarrier

PLUGIN = RulesCarrier.from_directory(Path(__file__).parent)
