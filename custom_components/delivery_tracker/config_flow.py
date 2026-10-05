"""Config-Flow der Delivery-Tracker-Integration.

Der Hub-Entry traegt nur einen Namen. Die eigentliche Konfiguration liegt in
Subentries — HA liefert dafuer Tabelle, Hinzufuegen, Bearbeiten und Loeschen
mit, eine eigene Listenverwaltung gibt es deshalb nicht:

* `mailbox`: ein IMAP-Postfach, mit Verbindungstest und Ordnerauswahl,
* `destination`: ein Zielort mit seinen Match-Regeln (bekommt ein eigenes Device),
* `api_source`: eine Tracking-API; das Geruest steht, Leben kommt in Step 3.1.

Die globalen Einstellungen stehen in den Options des Hub-Entries. Jede Aenderung
an Subentries oder Options laedt den Entry ueber den Update-Listener aus
`__init__` genau einmal neu.

Passwoerter und API-Schluessel stehen in `data` des Subentrys, in keinem Log und
in keiner Fehlermeldung. Beim Bearbeiten bleibt ein leeres Feld beim alten Wert,
damit sie nie ins Formular zurueckgeschickt werden muessen.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import slugify

from .carriers import async_get_registry
from .const import (
    API_SOURCE_TYPES,
    CARRIER_GENERIC,
    CONF_API_KEY,
    CONF_DESTINATION_ID,
    CONF_DESTINATION_RULES,
    CONF_DISABLED_CARRIERS,
    CONF_ENABLED_CARRIERS,
    CONF_FOLDER,
    CONF_GENERIC_FALLBACK,
    CONF_LOOKBACK_DAYS,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    CONF_PORT,
    CONF_RETENTION_DAYS,
    CONF_SCAN_INTERVAL,
    CONF_SECURITY,
    CONF_SERVER,
    CONF_SOURCE_TYPE,
    CONF_STALE_DAYS,
    CONF_USE_IDLE,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    CONNECTION_TEST_TIMEOUT,
    DEFAULT_API_SCAN_INTERVAL,
    DEFAULT_GENERIC_FALLBACK,
    DEFAULT_IMAP_FOLDER,
    DEFAULT_IMAP_LOOKBACK_DAYS,
    DEFAULT_IMAP_POLL_INTERVAL,
    DEFAULT_NAME,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_STALE_DAYS,
    DESTINATION_UNKNOWN,
    DESTINATION_UNKNOWN_NAME,
    DOMAIN,
    SOURCE_SEVENTEENTRACK,
    SUBENTRY_API_SOURCE,
    SUBENTRY_DESTINATION,
    SUBENTRY_MAILBOX,
)
from .sources.imap_source import (
    ImapAuthError,
    ImapConfig,
    ImapConnectionError,
    ImapFolderError,
    ImapSecurity,
    ImapSource,
    MailFolder,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
    }
)

# Fehlerschluessel (Uebersetzungen unter `error` des jeweiligen Subentry-Typs).
ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_INVALID_AUTH = "invalid_auth"
ERROR_FOLDER_NOT_FOUND = "folder_not_found"
ERROR_UNKNOWN = "unknown"
ERROR_INVALID_NAME = "invalid_name"
ERROR_NAME_EXISTS = "name_exists"
ERROR_NO_RULES = "no_rules"
ERROR_INVALID_API_KEY = "invalid_api_key"

ABORT_ALREADY_CONFIGURED = "already_configured"

# Grenzen der Zahlenfelder.
MIN_POLL_INTERVAL = 30
MAX_POLL_INTERVAL = 86400
MAX_LOOKBACK_DAYS = 90
MIN_API_SCAN_INTERVAL = 300
MAX_DAYS = 365

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_RULE_SELECTOR = TextSelector(TextSelectorConfig(multiple=True))


def _days_selector() -> NumberSelector:
    """Zahlenfeld fuer eine Dauer in Tagen; 0 schaltet die Regel ab."""
    return NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=MAX_DAYS,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement=UnitOfTime.DAYS,
        )
    )


def _seconds_selector(minimum: int, maximum: int) -> NumberSelector:
    """Zahlenfeld fuer ein Intervall in Sekunden."""
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement=UnitOfTime.SECONDS,
        )
    )


def _other_subentries(flow: ConfigSubentryFlow, subentry_type: str) -> list[Any]:
    """Alle Subentries dieses Typs ausser dem gerade bearbeiteten."""
    entry = flow._get_entry()
    own_id = flow.context.get("subentry_id") if flow.source == SOURCE_RECONFIGURE else None
    return [
        subentry
        for subentry in entry.get_subentries_of_type(subentry_type)
        if subentry.subentry_id != own_id
    ]


# ----------------------------------------------------------------------
# Hub-Entry
# ----------------------------------------------------------------------


class DeliveryTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Legt den Hub-Entry der Integration an."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DeliveryTrackerOptionsFlow:
        """Options-Flow fuer die globalen Einstellungen."""
        return DeliveryTrackerOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Die drei Subentry-Typen, die HA in der Oberflaeche anbietet."""
        return {
            SUBENTRY_MAILBOX: MailboxSubentryFlow,
            SUBENTRY_DESTINATION: DestinationSubentryFlow,
            SUBENTRY_API_SOURCE: ApiSourceSubentryFlow,
        }

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Vom Benutzer gestarteter Schritt."""
        # `single_config_entry` im Manifest faengt das schon vorher ab; die eigene
        # Pruefung schuetzt Aufrufer, die den Flow direkt starten.
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)

        return self.async_create_entry(
            title=user_input[CONF_NAME].strip() or DEFAULT_NAME,
            data={},
            options={
                CONF_RETENTION_DAYS: DEFAULT_RETENTION_DAYS,
                CONF_STALE_DAYS: DEFAULT_STALE_DAYS,
                CONF_DISABLED_CARRIERS: [],
                CONF_GENERIC_FALLBACK: DEFAULT_GENERIC_FALLBACK,
            },
        )


# ----------------------------------------------------------------------
# Subentry: Postfach
# ----------------------------------------------------------------------


async def async_test_login(data: Mapping[str, Any]) -> tuple[list[MailFolder], str | None]:
    """Anmelden und die Ordner holen. Liefert Ordner und ggf. einen Fehlerschluessel."""
    source = ImapSource(ImapConfig.from_mapping(data))
    try:
        async with asyncio.timeout(CONNECTION_TEST_TIMEOUT):
            folders = await source.async_list_folders()
    except ImapAuthError:
        return [], ERROR_INVALID_AUTH
    except ImapConnectionError, TimeoutError:
        return [], ERROR_CANNOT_CONNECT
    except Exception:
        # Ohne die Daten im Log: Sie enthalten das Passwort.
        _LOGGER.exception("Unerwarteter Fehler beim Verbindungstest zu %s", data.get(CONF_SERVER))
        return [], ERROR_UNKNOWN
    return folders, None


async def async_test_folder(data: Mapping[str, Any]) -> str | None:
    """Den gewaehlten Ordner oeffnen. Liefert ggf. einen Fehlerschluessel."""
    source = ImapSource(ImapConfig.from_mapping(data))
    try:
        async with asyncio.timeout(CONNECTION_TEST_TIMEOUT):
            await source.async_setup()
    except ImapFolderError:
        return ERROR_FOLDER_NOT_FOUND
    except ImapAuthError:
        return ERROR_INVALID_AUTH
    except ImapConnectionError, TimeoutError:
        return ERROR_CANNOT_CONNECT
    except Exception:
        _LOGGER.exception(
            "Unerwarteter Fehler beim Oeffnen eines Ordners auf %s", data.get(CONF_SERVER)
        )
        return ERROR_UNKNOWN
    finally:
        await source.async_close()
    return None


def _mailbox_unique_id(data: Mapping[str, Any]) -> str:
    """Ein Postfach ist Benutzer, Server und Ordner — dasselbe Tripel nur einmal."""
    return f"{data[CONF_USERNAME]}@{data[CONF_SERVER]}/{data[CONF_FOLDER]}".casefold()


def _mailbox_title(data: Mapping[str, Any]) -> str:
    """Anzeigename in der Subentry-Liste."""
    title = str(data[CONF_USERNAME])
    if "@" not in title:
        title = f"{title}@{data[CONF_SERVER]}"
    if str(data[CONF_FOLDER]).upper() != DEFAULT_IMAP_FOLDER:
        title = f"{title} ({data[CONF_FOLDER]})"
    return title


class MailboxSubentryFlow(ConfigSubentryFlow):
    """Ein IMAP-Postfach anlegen oder bearbeiten.

    Zwei Schritte: Zugangsdaten mit Verbindungstest, danach Ordner und Abrufart.
    Der Ordner kommt erst nach erfolgreicher Anmeldung, weil nur dann die Liste
    der vorhandenen Ordner bekannt ist.
    """

    def __init__(self) -> None:
        """Leeren Flow anlegen."""
        self._data: dict[str, Any] = {}
        self._folders: list[MailFolder] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Neues Postfach: Zugangsdaten."""
        return await self._async_step_connection("user", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Bestehendes Postfach: Zugangsdaten, vorbelegt bis auf das Passwort."""
        if not self._data:
            self._data = dict(self._get_reconfigure_subentry().data)
        return await self._async_step_connection("reconfigure", user_input)

    async def _async_step_connection(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        """Formular fuer Server und Anmeldung, mit Verbindungstest."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**self._data, **user_input}
            if not user_input.get(CONF_PASSWORD):
                # Beim Bearbeiten heisst ein leeres Passwortfeld „unveraendert".
                data[CONF_PASSWORD] = self._data.get(CONF_PASSWORD, "")
            if not user_input.get(CONF_PORT):
                data[CONF_PORT] = None
            else:
                data[CONF_PORT] = int(user_input[CONF_PORT])
            data[CONF_SERVER] = str(data[CONF_SERVER]).strip()
            data[CONF_USERNAME] = str(data[CONF_USERNAME]).strip()

            folders, error = await async_test_login(data)
            if error is None:
                self._data = data
                self._folders = folders
                return await self.async_step_folder()
            errors["base"] = error

        reconfigure = step_id == "reconfigure"
        schema = vol.Schema(
            {
                vol.Required(CONF_SERVER): str,
                vol.Optional(CONF_PORT): NumberSelector(
                    NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_SECURITY, default=ImapSecurity.SSL.value): SelectSelector(
                    SelectSelectorConfig(
                        options=[security.value for security in ImapSecurity],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_SECURITY,
                    )
                ),
                vol.Required(CONF_VERIFY_SSL, default=True): BooleanSelector(),
                vol.Required(CONF_USERNAME): str,
                (
                    vol.Optional(CONF_PASSWORD) if reconfigure else vol.Required(CONF_PASSWORD)
                ): _PASSWORD_SELECTOR,
            }
        )
        # Das Passwort wird nie vorbelegt — es soll den Server nicht wieder verlassen.
        suggested = {
            key: value
            for key, value in {**self._data, **(user_input or {})}.items()
            if key != CONF_PASSWORD and value is not None
        }
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_folder(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ordner und Abrufart; der Ordner wird vor dem Speichern geoeffnet."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                **self._data,
                **user_input,
                CONF_FOLDER: str(user_input[CONF_FOLDER]).strip(),
                CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL]),
                CONF_LOOKBACK_DAYS: int(user_input[CONF_LOOKBACK_DAYS]),
            }
            error = await async_test_folder(data)
            if error is None:
                return self._async_finish(data)
            errors["base"] = error

        if self._folders:
            # Freie Eingabe bleibt moeglich: Manche Server listen nicht alle Ordner.
            folder_selector: Any = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=folder.name, label=folder.label)
                        for folder in self._folders
                    ],
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        else:
            folder_selector = str

        schema = vol.Schema(
            {
                vol.Required(CONF_FOLDER, default=DEFAULT_IMAP_FOLDER): folder_selector,
                vol.Required(CONF_USE_IDLE, default=True): BooleanSelector(),
                vol.Required(
                    CONF_POLL_INTERVAL, default=DEFAULT_IMAP_POLL_INTERVAL
                ): _seconds_selector(MIN_POLL_INTERVAL, MAX_POLL_INTERVAL),
                vol.Required(
                    CONF_LOOKBACK_DAYS, default=DEFAULT_IMAP_LOOKBACK_DAYS
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=MAX_LOOKBACK_DAYS,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement=UnitOfTime.DAYS,
                    )
                ),
            }
        )
        suggested = {**self._data, **(user_input or {})}
        return self.async_show_form(
            step_id="folder",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={"server": str(self._data.get(CONF_SERVER, ""))},
        )

    @callback
    def _async_finish(self, data: dict[str, Any]) -> SubentryFlowResult:
        """Anlegen bzw. aktualisieren — dasselbe Postfach gibt es nur einmal."""
        unique_id = _mailbox_unique_id(data)
        if any(
            subentry.unique_id == unique_id
            for subentry in _other_subentries(self, SUBENTRY_MAILBOX)
        ):
            return self.async_abort(reason=ABORT_ALREADY_CONFIGURED)

        title = _mailbox_title(data)
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=title,
                data=data,
                unique_id=unique_id,
            )
        return self.async_create_entry(title=title, data=data, unique_id=unique_id)


# ----------------------------------------------------------------------
# Subentry: Zielort
# ----------------------------------------------------------------------


def _clean_rules(values: Any) -> list[str]:
    """Leere und doppelte Eintraege einer Regel-Liste verwerfen."""
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


class DestinationSubentryFlow(ConfigSubentryFlow):
    """Einen Zielort mit seinen Match-Regeln anlegen oder bearbeiten.

    Die ID entsteht beim Anlegen aus dem Namen und bleibt danach stabil — auch
    bei einer Umbenennung, sonst verloere jede Automation ihren Filter.
    """

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Neuer Zielort."""
        return await self._async_step_rules("user", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Bestehenden Zielort bearbeiten."""
        return await self._async_step_rules("reconfigure", user_input)

    async def _async_step_rules(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        """Formular mit Name und allen Regelarten."""
        reconfigure = step_id == "reconfigure"
        current: Mapping[str, Any] = self._get_reconfigure_subentry().data if reconfigure else {}
        errors: dict[str, str] = {}

        if user_input is not None:
            name = str(user_input.get(CONF_NAME, "")).strip()
            rules = {key: _clean_rules(user_input.get(key)) for key in CONF_DESTINATION_RULES}
            destination_id = current.get(CONF_DESTINATION_ID) or slugify(name)
            others = _other_subentries(self, SUBENTRY_DESTINATION)

            if (
                not name
                or not destination_id
                or destination_id == DESTINATION_UNKNOWN
                or name.casefold() == DESTINATION_UNKNOWN_NAME.casefold()
            ):
                errors[CONF_NAME] = ERROR_INVALID_NAME
            elif any(
                subentry.title.casefold() == name.casefold()
                or subentry.data.get(CONF_DESTINATION_ID) == destination_id
                for subentry in others
            ):
                errors[CONF_NAME] = ERROR_NAME_EXISTS
            elif not any(rules.values()):
                errors["base"] = ERROR_NO_RULES
            else:
                data = {CONF_DESTINATION_ID: destination_id, CONF_NAME: name, **rules}
                if reconfigure:
                    return self.async_update_and_abort(
                        self._get_entry(),
                        self._get_reconfigure_subentry(),
                        title=name,
                        data=data,
                    )
                return self.async_create_entry(title=name, data=data, unique_id=destination_id)

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): str,
                **{vol.Optional(key): _RULE_SELECTOR for key in CONF_DESTINATION_RULES},
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, user_input or current),
            errors=errors,
        )


# ----------------------------------------------------------------------
# Subentry: API-Quelle
# ----------------------------------------------------------------------


async def _async_validate_seventeentrack_key(hass: HomeAssistant, api_key: str) -> bool:
    """Formale Pruefung eines 17track-Schluessels.

    Step 3.1 ersetzt das durch eine echte Anfrage gegen die API; bis dahin
    faengt die Pruefung wenigstens Tippfehler wie Leerzeichen ab.
    """
    return len(api_key) >= 16 and not any(char.isspace() for char in api_key)


# Pruefung des API-Schluessels je Quellentyp. Neue Quellen tragen sich hier ein.
API_KEY_VALIDATORS = {
    SOURCE_SEVENTEENTRACK: _async_validate_seventeentrack_key,
}

API_SOURCE_TITLES = {
    SOURCE_SEVENTEENTRACK: "17TRACK",
}


async def async_validate_api_key(hass: HomeAssistant, source_type: str, api_key: str) -> str | None:
    """Den Schluessel fuer den Quellentyp pruefen. Liefert ggf. einen Fehlerschluessel."""
    validator = API_KEY_VALIDATORS.get(source_type)
    if validator is None:
        return ERROR_UNKNOWN
    try:
        valid = await validator(hass, api_key)
    except Exception:
        _LOGGER.exception("Unerwarteter Fehler bei der Pruefung des %s-Schluessels", source_type)
        return ERROR_UNKNOWN
    return None if valid else ERROR_INVALID_API_KEY


class ApiSourceSubentryFlow(ConfigSubentryFlow):
    """Eine Tracking-API anlegen oder bearbeiten; je Quellentyp nur einmal."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Neue API-Quelle."""
        errors: dict[str, str] = {}
        if user_input is not None:
            source_type = user_input[CONF_SOURCE_TYPE]
            if any(
                subentry.unique_id == source_type
                for subentry in _other_subentries(self, SUBENTRY_API_SOURCE)
            ):
                return self.async_abort(reason=ABORT_ALREADY_CONFIGURED)
            data = {
                CONF_SOURCE_TYPE: source_type,
                CONF_API_KEY: str(user_input[CONF_API_KEY]).strip(),
                CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
            }
            error = await async_validate_api_key(self.hass, source_type, data[CONF_API_KEY])
            if error is None:
                return self.async_create_entry(
                    title=API_SOURCE_TITLES.get(source_type, source_type),
                    data=data,
                    unique_id=source_type,
                )
            errors["base"] = error

        schema = vol.Schema(
            {
                vol.Required(CONF_SOURCE_TYPE, default=API_SOURCE_TYPES[0]): SelectSelector(
                    SelectSelectorConfig(
                        options=list(API_SOURCE_TYPES),
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_SOURCE_TYPE,
                    )
                ),
                vol.Required(CONF_API_KEY): _PASSWORD_SELECTOR,
                vol.Required(
                    CONF_SCAN_INTERVAL, default=DEFAULT_API_SCAN_INTERVAL
                ): _seconds_selector(MIN_API_SCAN_INTERVAL, MAX_POLL_INTERVAL),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                schema, {k: v for k, v in (user_input or {}).items() if k != CONF_API_KEY}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Schluessel und Intervall aendern; der Quellentyp steht fest."""
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                **subentry.data,
                CONF_API_KEY: str(user_input.get(CONF_API_KEY) or "").strip()
                or subentry.data[CONF_API_KEY],
                CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
            }
            error = await async_validate_api_key(
                self.hass, data[CONF_SOURCE_TYPE], data[CONF_API_KEY]
            )
            if error is None:
                return self.async_update_and_abort(self._get_entry(), subentry, data=data)
            errors["base"] = error

        schema = vol.Schema(
            {
                vol.Optional(CONF_API_KEY): _PASSWORD_SELECTOR,
                vol.Required(CONF_SCAN_INTERVAL): _seconds_selector(
                    MIN_API_SCAN_INTERVAL, MAX_POLL_INTERVAL
                ),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_SCAN_INTERVAL: subentry.data.get(CONF_SCAN_INTERVAL)}
            ),
            errors=errors,
            description_placeholders={"source": subentry.title},
        )


# ----------------------------------------------------------------------
# Options: globale Einstellungen
# ----------------------------------------------------------------------


class DeliveryTrackerOptionsFlow(OptionsFlow):
    """Retention und aktive Zusteller.

    Bewusst kein `OptionsFlowWithReload`: Den Reload uebernimmt der
    Update-Listener, der auch bei Subentry-Aenderungen greift.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Das einzige Formular."""
        registry = await async_get_registry(self.hass)
        carriers = [plugin for plugin in registry.plugins if plugin.carrier_id != CARRIER_GENERIC]
        carrier_ids = {plugin.carrier_id for plugin in carriers}
        options = self.config_entry.options
        disabled = set(options.get(CONF_DISABLED_CARRIERS, ()))

        if user_input is not None:
            enabled = set(user_input.get(CONF_ENABLED_CARRIERS, ()))
            # Gespeichert wird, was abgeschaltet ist: Ein neues Plugin ist nach
            # einem Update damit automatisch aktiv. Abgeschaltete IDs eines
            # gerade nicht geladenen Plugins bleiben erhalten.
            new_disabled = (carrier_ids - enabled) | (disabled - carrier_ids)
            return self.async_create_entry(
                data={
                    CONF_RETENTION_DAYS: int(user_input[CONF_RETENTION_DAYS]),
                    CONF_STALE_DAYS: int(user_input[CONF_STALE_DAYS]),
                    CONF_DISABLED_CARRIERS: sorted(new_disabled),
                    CONF_GENERIC_FALLBACK: bool(user_input[CONF_GENERIC_FALLBACK]),
                }
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_RETENTION_DAYS,
                    default=options.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS),
                ): _days_selector(),
                vol.Required(
                    CONF_STALE_DAYS, default=options.get(CONF_STALE_DAYS, DEFAULT_STALE_DAYS)
                ): _days_selector(),
                vol.Required(
                    CONF_ENABLED_CARRIERS,
                    default=sorted(carrier_ids - disabled),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=plugin.carrier_id, label=plugin.name)
                            for plugin in carriers
                        ],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_GENERIC_FALLBACK,
                    default=options.get(CONF_GENERIC_FALLBACK, DEFAULT_GENERIC_FALLBACK),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
