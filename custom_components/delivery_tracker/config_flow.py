"""Config-Flow der Delivery-Tracker-Integration.

Minimalvariante: Es wird nur ein Name abgefragt. Die eigentlichen Subentries
(`mailbox`, `destination`, `api_source`) kommen in Step 2.1 dazu.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_NAME, DEFAULT_NAME, DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
    }
)


class DeliveryTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Legt den Hub-Entry der Integration an."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Vom Benutzer gestarteter Schritt."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)

        # Pro HA-Instanz gibt es genau einen Hub (`single_config_entry` im Manifest);
        # Postfaecher, Zieladressen und API-Quellen werden spaeter Subentries.
        return self.async_create_entry(title=user_input[CONF_NAME], data={})
