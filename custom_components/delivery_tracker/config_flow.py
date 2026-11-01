"""Config-Flow der Delivery-Tracker-Integration.

Der Hub-Entry traegt nur einen Namen. Die eigentliche Konfiguration liegt in
Subentries — HA liefert dafuer Tabelle, Hinzufuegen, Bearbeiten und Loeschen
mit, eine eigene Listenverwaltung gibt es deshalb nicht:

* `mailbox`: ein IMAP-Postfach, mit Verbindungstest und Ordnerauswahl,
* `destination`: ein Zielort mit seinen Match-Regeln (bekommt ein eigenes Device),
* `api_source`: eine Tracking-Quelle — das kostenlose 17TRACK-Konto (E-Mail und
  Passwort) oder die kostenpflichtige 17TRACK-API (Schluessel); die Zugangsdaten
  werden beim Anlegen gegen den Dienst geprueft.

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
from collections.abc import Awaitable, Callable, Iterable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
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
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .addresses import AddressMatcher, suggest_destination
from .carriers import async_get_registry
from .const import (
    API_SOURCE_TYPES,
    CARRIER_GENERIC,
    CONF_API_KEY,
    CONF_DESTINATION_ID,
    CONF_DESTINATION_RULES,
    CONF_DISABLED_CARRIERS,
    CONF_ENABLED_CARRIERS,
    CONF_EXCLUDED_CARRIERS,
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
    CONF_TRACKED_CARRIERS,
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
    REAUTH_SUBENTRY_ID,
    SOURCE_SEVENTEENTRACK,
    SOURCE_SEVENTEENTRACK_ACCOUNT,
    SUBENTRY_API_SOURCE,
    SUBENTRY_DESTINATION,
    SUBENTRY_MAILBOX,
)
from .manage import (
    EMPTY,
    Texts,
    async_load_texts,
    mail_log,
    md,
    overview,
    shipment_details,
    shipment_label,
    tracking_log,
)
from .models import Destination, Shipment
from .sources.imap_source import (
    ImapAuthError,
    ImapConfig,
    ImapConnectionError,
    ImapFolderError,
    ImapSecurity,
    ImapSource,
    MailFolder,
)
from .sources.seventeentrack_account_api import (
    SeventeenTrackAccountAuthError,
    SeventeenTrackAccountClient,
    SeventeenTrackAccountError,
)
from .sources.seventeentrack_api import (
    SeventeenTrackAuthError,
    SeventeenTrackClient,
    SeventeenTrackError,
)

if TYPE_CHECKING:
    from .entry import DeliveryTrackerData

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
ABORT_REAUTH_SUCCESSFUL = "reauth_successful"
ABORT_REAUTH_UNKNOWN_SOURCE = "reauth_unknown_source"

# Sendungsverwaltung im Options-Flow.
OPTIONS_MENU = ("settings", "shipments", "mails", "tracking")
FIELD_SHIPMENTS = "shipments"
FIELD_ACTION = "action"
FIELD_CONFIRM = "confirm"
FIELD_TARGET = "target"
FIELD_ASSIGN = "assign"
FIELD_FILTER = "filter"
FIELD_RESCAN = "rescan"
ACTION_DETAILS = "details"
ACTION_BACK = "back"
ACTION_DELETE = "delete"
ACTION_MARK_DELIVERED = "mark_delivered"
ACTION_DESTINATION_NEW = "destination_new"
ACTION_DESTINATION_EXTEND = "destination_extend"
FILTER_PROBLEMS = "problems"
FILTER_ALL = "all"
ERROR_SELECT_NONE = "select_none"
ERROR_SELECT_ONE = "select_one"
ERROR_CONFIRM_REQUIRED = "confirm_required"
ABORT_NOT_LOADED = "not_loaded"
ABORT_NO_SHIPMENTS = "no_shipments"
ABORT_NO_MAILBOXES = "no_mailboxes"
ABORT_NO_TRACKING_SOURCES = "no_tracking_sources"
FIELD_REFRESH = "refresh"
ABORT_SHIPMENT_GONE = "shipment_gone"
ABORT_SHIPMENTS_REMOVED = "shipments_removed"
ABORT_SHIPMENTS_DELIVERED = "shipments_delivered"
ABORT_DESTINATION_SAVED = "destination_saved"

# Grenzen der Zahlenfelder.
MIN_POLL_INTERVAL = 30
MAX_POLL_INTERVAL = 86400
MAX_LOOKBACK_DAYS = 365
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

    _reauth_subentry_id: str | None = None

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

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Eine Quelle meldet abgelehnte Zugangsdaten (angestossen vom Coordinator).

        Die Zugangsdaten liegen im Subentry, nicht am Hub-Entry; welcher es ist,
        steht in den Reauth-Daten.
        """
        self._reauth_subentry_id = entry_data.get(REAUTH_SUBENTRY_ID)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Neues Passwort bzw. neuen API-Schluessel abfragen und pruefen."""
        entry = self._get_reauth_entry()
        subentry = entry.subentries.get(self._reauth_subentry_id or "")
        if subentry is None or subentry.subentry_type not in (
            SUBENTRY_MAILBOX,
            SUBENTRY_API_SOURCE,
        ):
            return self.async_abort(reason=ABORT_REAUTH_UNKNOWN_SOURCE)

        is_mailbox = subentry.subentry_type == SUBENTRY_MAILBOX
        secret = (
            CONF_PASSWORD
            if is_mailbox
            else api_source_secret(str(subentry.data.get(CONF_SOURCE_TYPE, "")))
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**subentry.data, secret: _clean_secret(secret, user_input[secret])}
            if is_mailbox:
                error = await async_test_folder(data)
            else:
                error = await async_validate_api_source(self.hass, data)
            if error is None:
                # Der Update-Listener laedt den Entry danach neu.
                self.hass.config_entries.async_update_subentry(entry, subentry, data=data)
                return self.async_abort(reason=ABORT_REAUTH_SUCCESSFUL)
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(secret): _PASSWORD_SELECTOR}),
            errors=errors,
            description_placeholders={"source": subentry.title},
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


def destination_name_error(name: str, destination_id: str, others: Iterable[Any]) -> str | None:
    """Fehlerschluessel, wenn Name oder ID eines Zielorts nicht taugen.

    `others` sind die uebrigen Zielort-Subentries. Gilt fuer den Subentry-Flow
    und fuer „Zielort aus Sendung anlegen" in der Sendungsverwaltung.
    """
    if (
        not name
        or not destination_id
        or destination_id == DESTINATION_UNKNOWN
        or name.casefold() == DESTINATION_UNKNOWN_NAME.casefold()
    ):
        return ERROR_INVALID_NAME
    if any(
        subentry.title.casefold() == name.casefold()
        or subentry.data.get(CONF_DESTINATION_ID) == destination_id
        for subentry in others
    ):
        return ERROR_NAME_EXISTS
    return None


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

            if name_error := destination_name_error(name, destination_id, others):
                errors[CONF_NAME] = name_error
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


async def _async_validate_seventeentrack_key(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> str | None:
    """Einen 17TRACK-Schluessel pruefen. Liefert ggf. einen Fehlerschluessel.

    Tippfehler wie Leerzeichen fallen ohne Anfrage durch; sonst fragt die
    Pruefung das Kontingent ab — das kostet nichts und scheitert an einem
    ungueltigen Schluessel.
    """
    api_key = str(data[CONF_API_KEY])
    if len(api_key) < 16 or any(char.isspace() for char in api_key):
        return ERROR_INVALID_API_KEY
    client = SeventeenTrackClient(async_get_clientsession(hass), api_key)
    try:
        async with asyncio.timeout(CONNECTION_TEST_TIMEOUT):
            await client.async_get_quota()
    except SeventeenTrackAuthError:
        return ERROR_INVALID_API_KEY
    except SeventeenTrackError, TimeoutError:
        return ERROR_CANNOT_CONNECT
    return None


async def _async_validate_seventeentrack_account(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> str | None:
    """Mit dem 17TRACK-Benutzerkonto anmelden. Liefert ggf. einen Fehlerschluessel."""
    if not str(data[CONF_USERNAME]).strip() or not str(data[CONF_PASSWORD]):
        return ERROR_INVALID_AUTH
    client = SeventeenTrackAccountClient(
        async_create_clientsession(hass, auto_cleanup=False),
        str(data[CONF_USERNAME]),
        str(data[CONF_PASSWORD]),
        request_interval=0,
    )
    try:
        async with asyncio.timeout(CONNECTION_TEST_TIMEOUT):
            await client.async_login()
    except SeventeenTrackAccountAuthError:
        return ERROR_INVALID_AUTH
    except SeventeenTrackAccountError, TimeoutError:
        return ERROR_CANNOT_CONNECT
    finally:
        await client.async_close()
    return None


# Pruefung der Zugangsdaten je Quellentyp. Neue Quellen tragen sich hier ein.
API_SOURCE_VALIDATORS: dict[
    str, Callable[[HomeAssistant, Mapping[str, Any]], Awaitable[str | None]]
] = {
    SOURCE_SEVENTEENTRACK: _async_validate_seventeentrack_key,
    SOURCE_SEVENTEENTRACK_ACCOUNT: _async_validate_seventeentrack_account,
}

API_SOURCE_TITLES = {
    SOURCE_SEVENTEENTRACK: "17TRACK API",
    SOURCE_SEVENTEENTRACK_ACCOUNT: "17TRACK-Konto",
}

# Das Geheimnis je Quellentyp: Es wird nie ins Formular zurueckgeschickt, bleibt
# beim Bearbeiten leer und ist das einzige Feld der Neuanmeldung.
API_SOURCE_SECRETS = {
    SOURCE_SEVENTEENTRACK: CONF_API_KEY,
    SOURCE_SEVENTEENTRACK_ACCOUNT: CONF_PASSWORD,
}

# Schritt mit den Zugangsdaten je Quellentyp.
STEP_API_KEY = "api_key"
STEP_ACCOUNT = "account"
API_SOURCE_STEPS = {
    SOURCE_SEVENTEENTRACK: STEP_API_KEY,
    SOURCE_SEVENTEENTRACK_ACCOUNT: STEP_ACCOUNT,
}


def api_source_secret(source_type: str) -> str:
    """Das Feld mit dem Geheimnis eines Quellentyps."""
    return API_SOURCE_SECRETS.get(source_type, CONF_API_KEY)


def _clean_secret(field: str, value: Any) -> str:
    """API-Schluessel ohne Leerraum am Rand; Passwoerter bleiben, wie sie sind."""
    text = str(value or "")
    return text.strip() if field == CONF_API_KEY else text


async def async_validate_api_source(hass: HomeAssistant, data: Mapping[str, Any]) -> str | None:
    """Die Zugangsdaten fuer den Quellentyp pruefen. Liefert ggf. einen Fehlerschluessel."""
    source_type = str(data.get(CONF_SOURCE_TYPE, ""))
    validator = API_SOURCE_VALIDATORS.get(source_type)
    if validator is None:
        return ERROR_UNKNOWN
    try:
        return await validator(hass, data)
    except Exception:
        _LOGGER.exception("Unerwarteter Fehler bei der Pruefung der Quelle %s", source_type)
        return ERROR_UNKNOWN


async def _async_carrier_filter(
    hass: HomeAssistant, excluded: Iterable[str]
) -> tuple[set[str], dict[Any, Any]]:
    """Alle Zusteller und das Feld fuer ihre Auswahl; vorausgewaehlt ist das Nicht-Ausgeschlossene.

    Gespeichert wird die Gegenmenge (`CONF_EXCLUDED_CARRIERS`): Ein neues
    Carrier-Plugin wird damit nach einem Update automatisch mit abgefragt.
    """
    registry = await async_get_registry(hass)
    carrier_ids = {plugin.carrier_id for plugin in registry.plugins}
    field = {
        vol.Required(
            CONF_TRACKED_CARRIERS, default=sorted(carrier_ids - set(excluded))
        ): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=plugin.carrier_id, label=plugin.name)
                    for plugin in registry.plugins
                ],
                multiple=True,
                mode=SelectSelectorMode.LIST,
            )
        )
    }
    return carrier_ids, field


def _excluded_carriers(
    carrier_ids: set[str], tracked: Iterable[str], previous: Iterable[str] = ()
) -> list[str]:
    """Abgewaehlte Zusteller; Ausschluesse gerade nicht geladener Plugins bleiben erhalten."""
    previous = set(previous)
    return sorted((carrier_ids - set(tracked)) | (previous - carrier_ids))


def _credential_schema(source_type: str, *, reconfigure: bool) -> dict[Any, Any]:
    """Die Felder fuer die Zugangsdaten; beim Bearbeiten ist das Geheimnis optional."""
    secret = api_source_secret(source_type)
    fields: dict[Any, Any] = {}
    if source_type == SOURCE_SEVENTEENTRACK_ACCOUNT:
        fields[vol.Required(CONF_USERNAME)] = TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        )
    marker = vol.Optional if reconfigure else vol.Required
    fields[marker(secret)] = _PASSWORD_SELECTOR
    return fields


class ApiSourceSubentryFlow(ConfigSubentryFlow):
    """Eine Tracking-Quelle anlegen oder bearbeiten; je Quellentyp nur einmal.

    Erst die Quelle waehlen — kostenloses 17TRACK-Konto oder kostenpflichtige
    17TRACK-API —, dann deren Zugangsdaten.
    """

    _source_type: str = API_SOURCE_TYPES[0]

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Neue Quelle: den Typ waehlen."""
        if user_input is not None:
            self._source_type = str(user_input[CONF_SOURCE_TYPE])
            if any(
                subentry.unique_id == self._source_type
                for subentry in _other_subentries(self, SUBENTRY_API_SOURCE)
            ):
                return self.async_abort(reason=ABORT_ALREADY_CONFIGURED)
            if self._source_type == SOURCE_SEVENTEENTRACK_ACCOUNT:
                return await self.async_step_account()
            return await self.async_step_api_key()

        schema = vol.Schema(
            {
                vol.Required(CONF_SOURCE_TYPE, default=API_SOURCE_TYPES[0]): SelectSelector(
                    SelectSelectorConfig(
                        options=list(API_SOURCE_TYPES),
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_SOURCE_TYPE,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Kostenpflichtige 17TRACK-API: Schluessel und Intervall."""
        return await self._async_step_credentials(STEP_API_KEY, user_input)

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Kostenloses 17TRACK-Konto: E-Mail, Passwort und Intervall."""
        return await self._async_step_credentials(STEP_ACCOUNT, user_input)

    async def _async_step_credentials(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        """Zugangsdaten abfragen, pruefen und die Quelle anlegen."""
        source_type = self._source_type
        secret = api_source_secret(source_type)
        carrier_ids, carrier_field = await _async_carrier_filter(self.hass, ())
        errors: dict[str, str] = {}
        if user_input is not None:
            data: dict[str, Any] = {CONF_SOURCE_TYPE: source_type}
            if CONF_USERNAME in user_input:
                data[CONF_USERNAME] = str(user_input[CONF_USERNAME]).strip()
            data[secret] = _clean_secret(secret, user_input[secret])
            data[CONF_SCAN_INTERVAL] = int(user_input[CONF_SCAN_INTERVAL])
            data[CONF_EXCLUDED_CARRIERS] = _excluded_carriers(
                carrier_ids, user_input.get(CONF_TRACKED_CARRIERS, carrier_ids)
            )
            error = await async_validate_api_source(self.hass, data)
            if error is None:
                return self.async_create_entry(
                    title=API_SOURCE_TITLES.get(source_type, source_type),
                    data=data,
                    unique_id=source_type,
                )
            errors["base"] = error

        schema = vol.Schema(
            {
                **_credential_schema(source_type, reconfigure=False),
                vol.Required(
                    CONF_SCAN_INTERVAL, default=DEFAULT_API_SCAN_INTERVAL
                ): _seconds_selector(MIN_API_SCAN_INTERVAL, MAX_POLL_INTERVAL),
                **carrier_field,
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                schema, {k: v for k, v in (user_input or {}).items() if k != secret}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Zugangsdaten und Intervall aendern; der Quellentyp steht fest."""
        subentry = self._get_reconfigure_subentry()
        source_type = str(subentry.data.get(CONF_SOURCE_TYPE, ""))
        secret = api_source_secret(source_type)
        previous = subentry.data.get(CONF_EXCLUDED_CARRIERS, [])
        carrier_ids, carrier_field = await _async_carrier_filter(self.hass, previous)
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                **subentry.data,
                secret: _clean_secret(secret, user_input.get(secret)) or subentry.data[secret],
                CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                CONF_EXCLUDED_CARRIERS: _excluded_carriers(
                    carrier_ids, user_input.get(CONF_TRACKED_CARRIERS, carrier_ids), previous
                ),
            }
            if CONF_USERNAME in user_input:
                data[CONF_USERNAME] = str(user_input[CONF_USERNAME]).strip()
            error = await async_validate_api_source(self.hass, data)
            if error is None:
                return self.async_update_and_abort(self._get_entry(), subentry, data=data)
            errors["base"] = error

        schema = vol.Schema(
            {
                **_credential_schema(source_type, reconfigure=True),
                vol.Required(CONF_SCAN_INTERVAL): _seconds_selector(
                    MIN_API_SCAN_INTERVAL, MAX_POLL_INTERVAL
                ),
                **carrier_field,
            }
        )
        suggested = {
            CONF_SCAN_INTERVAL: subentry.data.get(CONF_SCAN_INTERVAL),
            CONF_USERNAME: subentry.data.get(CONF_USERNAME),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={"source": subentry.title},
        )


# ----------------------------------------------------------------------
# Options: globale Einstellungen
# ----------------------------------------------------------------------


class DeliveryTrackerOptionsFlow(OptionsFlow):
    """Einstellungen und Sendungsverwaltung.

    Das Menue fuehrt zu den globalen Einstellungen (Retention, aktive
    Zusteller), zur Liste der Sendungen und zum Protokoll der letzten Mails.

    Bewusst kein `OptionsFlowWithReload`: Den Reload uebernimmt der
    Update-Listener, der auch bei Subentry-Aenderungen greift. Die Schritte der
    Sendungsverwaltung enden mit `async_abort` statt `async_create_entry` —
    sonst wuerden die Optionen ueberschrieben und der Entry neu geladen.
    """

    def __init__(self) -> None:
        """Zustand zwischen den Schritten."""
        self._shipment_id: str | None = None
        self._target: str | None = None
        """`None` = neuer Zielort, sonst die Subentry-ID des zu ergaenzenden."""
        self._pending: Destination | None = None
        self._texts: Texts | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Das Menue."""
        return self.async_show_menu(step_id="init", menu_options=list(OPTIONS_MENU))

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Globale Einstellungen."""
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
        return self.async_show_form(step_id="settings", data_schema=schema)

    # ------------------------------------------------------------------
    # Sendungsverwaltung
    # ------------------------------------------------------------------

    def _runtime(self) -> DeliveryTrackerData | None:
        """Laufzeitdaten des Entries; `None`, solange er nicht geladen ist."""
        entry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return None
        return cast("DeliveryTrackerData", entry.runtime_data)

    async def _async_texts(self) -> Texts:
        if self._texts is None:
            self._texts = await async_load_texts(self.hass)
        return self._texts

    @staticmethod
    def _names(runtime: DeliveryTrackerData) -> tuple[dict[str, str], dict[str, str]]:
        """Anzeigenamen der Zusteller und Zielorte."""
        carriers = {plugin.carrier_id: plugin.name for plugin in runtime.registry.plugins}
        destinations = {destination.id: destination.name for destination in runtime.destinations}
        return carriers, destinations

    async def _async_shipment_options(
        self, runtime: DeliveryTrackerData, shipments: list[Shipment]
    ) -> list[SelectOptionDict]:
        """Eine Auswahlzeile je Sendung."""
        texts = await self._async_texts()
        carriers, destinations = self._names(runtime)
        now = dt_util.utcnow()
        return [
            SelectOptionDict(
                value=shipment.id,
                label=shipment_label(
                    texts,
                    shipment,
                    runtime.store.expiry(shipment),
                    carriers=carriers,
                    destinations=destinations,
                    now=now,
                ),
            )
            for shipment in shipments
        ]

    @staticmethod
    def _sorted_shipments(runtime: DeliveryTrackerData) -> list[Shipment]:
        """Laengste Zeit ohne neue Mail zuerst — die Kandidaten zum Aufraeumen."""
        return sorted(runtime.store.shipments, key=lambda item: (item.last_updated, item.id))

    async def async_step_shipments(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Alle Sendungen; markierte loeschen, abhaken oder im Detail ansehen."""
        if (runtime := self._runtime()) is None:
            return self.async_abort(reason=ABORT_NOT_LOADED)
        shipments = self._sorted_shipments(runtime)
        if not shipments:
            return self.async_abort(reason=ABORT_NO_SHIPMENTS)

        errors: dict[str, str] = {}
        if user_input is not None:
            known = {shipment.id for shipment in shipments}
            selected = [value for value in user_input.get(FIELD_SHIPMENTS) or [] if value in known]
            action = user_input.get(FIELD_ACTION, ACTION_DETAILS)
            if not selected:
                errors["base"] = ERROR_SELECT_NONE
            elif action == ACTION_DETAILS:
                if len(selected) != 1:
                    errors["base"] = ERROR_SELECT_ONE
                else:
                    self._shipment_id = selected[0]
                    return await self.async_step_shipment()
            elif result := self._apply_action(runtime, action, selected, user_input, errors):
                return result

        texts = await self._async_texts()
        schema = vol.Schema(
            {
                vol.Optional(FIELD_SHIPMENTS, default=[]): SelectSelector(
                    SelectSelectorConfig(
                        options=await self._async_shipment_options(runtime, shipments),
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(FIELD_ACTION, default=ACTION_DETAILS): _action_selector(
                    [ACTION_DETAILS, ACTION_MARK_DELIVERED, ACTION_DELETE]
                ),
                vol.Optional(FIELD_CONFIRM, default=False): BooleanSelector(),
            }
        )
        expiries = {shipment.id: runtime.store.expiry(shipment) for shipment in shipments}
        return self.async_show_form(
            step_id="shipments",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
            description_placeholders={
                "overview": overview(texts, shipments, expiries, now=dt_util.utcnow()),
                "stale_days": str(runtime.store.stale_days),
                "retention_days": str(runtime.store.retention_days),
            },
        )

    def _apply_action(
        self,
        runtime: DeliveryTrackerData,
        action: str,
        selected: list[str],
        user_input: Mapping[str, Any],
        errors: dict[str, str],
    ) -> ConfigFlowResult | None:
        """Loeschen oder als zugestellt markieren; `None` heisst: Formular erneut zeigen."""
        coordinator = runtime.coordinator
        if action == ACTION_DELETE:
            if not user_input.get(FIELD_CONFIRM):
                errors["base"] = ERROR_CONFIRM_REQUIRED
                return None
            removed = coordinator.async_remove_shipments(selected)
            return self.async_abort(
                reason=ABORT_SHIPMENTS_REMOVED,
                description_placeholders={"count": str(len(removed))},
            )
        if action == ACTION_MARK_DELIVERED:
            marked = [
                shipment_id
                for shipment_id in selected
                if coordinator.async_mark_delivered(shipment_id) is not None
            ]
            return self.async_abort(
                reason=ABORT_SHIPMENTS_DELIVERED,
                description_placeholders={"count": str(len(marked))},
            )
        errors["base"] = ERROR_UNKNOWN
        return None

    async def async_step_shipment(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Eine Sendung mit allem, was ueber sie bekannt ist."""
        if (runtime := self._runtime()) is None:
            return self.async_abort(reason=ABORT_NOT_LOADED)
        if self._shipment_id is None or (shipment := runtime.store.get(self._shipment_id)) is None:
            return self.async_abort(reason=ABORT_SHIPMENT_GONE)

        destinations = self.config_entry.get_subentries_of_type(SUBENTRY_DESTINATION)
        actions = [ACTION_BACK, ACTION_MARK_DELIVERED, ACTION_DELETE, ACTION_DESTINATION_NEW]
        if destinations:
            actions.append(ACTION_DESTINATION_EXTEND)

        errors: dict[str, str] = {}
        if user_input is not None:
            action = user_input.get(FIELD_ACTION, ACTION_BACK)
            if action == ACTION_BACK:
                return await self.async_step_shipments()
            if action in (ACTION_DESTINATION_NEW, ACTION_DESTINATION_EXTEND):
                self._target = None if action == ACTION_DESTINATION_NEW else ""
                return await self.async_step_destination()
            if result := self._apply_action(runtime, action, [shipment.id], user_input, errors):
                return result

        texts = await self._async_texts()
        carriers, names = self._names(runtime)
        schema = vol.Schema(
            {
                vol.Required(FIELD_ACTION, default=ACTION_BACK): _action_selector(actions),
                vol.Optional(FIELD_CONFIRM, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="shipment",
            data_schema=schema,
            errors=errors,
            description_placeholders=shipment_details(
                texts,
                shipment,
                runtime.store.expiry(shipment),
                carriers=carriers,
                destinations=names,
                now=dt_util.utcnow(),
            ),
        )

    async def async_step_destination(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Zielort aus der Adresse der Sendung anlegen oder einen bestehenden ergaenzen."""
        if (runtime := self._runtime()) is None:
            return self.async_abort(reason=ABORT_NOT_LOADED)
        if self._shipment_id is None or (shipment := runtime.store.get(self._shipment_id)) is None:
            return self.async_abort(reason=ABORT_SHIPMENT_GONE)

        extend = self._target is not None
        subentries = self.config_entry.get_subentries_of_type(SUBENTRY_DESTINATION)
        errors: dict[str, str] = {}

        if user_input is not None:
            rules = {key: _clean_rules(user_input.get(key)) for key in CONF_DESTINATION_RULES}
            if extend:
                chosen = user_input.get(FIELD_TARGET)
                target = next((item for item in subentries if item.subentry_id == chosen), None)
                if target is None:
                    errors[FIELD_TARGET] = ERROR_UNKNOWN
                elif not any(rules.values()):
                    errors["base"] = ERROR_NO_RULES
                else:
                    self._target = target.subentry_id
                    current = Destination.from_dict(target.data)
                    self._pending = Destination.from_dict(
                        {
                            **target.data,
                            **{
                                key: list(dict.fromkeys([*getattr(current, key), *rules[key]]))
                                for key in CONF_DESTINATION_RULES
                            },
                        }
                    )
                    return await self.async_step_destination_confirm()
            else:
                name = str(user_input.get(CONF_NAME, "")).strip()
                destination_id = slugify(name)
                if name_error := destination_name_error(name, destination_id, subentries):
                    errors[CONF_NAME] = name_error
                elif not any(rules.values()):
                    errors["base"] = ERROR_NO_RULES
                else:
                    self._pending = Destination(id=destination_id, name=name, **rules)
                    return await self.async_step_destination_confirm()

        address = shipment.destination_raw or shipment.pickup_location
        suggestion = suggest_destination(address)
        fields: dict[Any, Any] = {}
        if extend:
            fields[vol.Required(FIELD_TARGET)] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=item.subentry_id, label=item.title)
                        for item in subentries
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        else:
            fields[vol.Required(CONF_NAME)] = str
        fields.update({vol.Optional(key): _RULE_SELECTOR for key in CONF_DESTINATION_RULES})
        suggested: dict[str, Any] = {CONF_NAME: suggestion.name, **suggestion.rules}
        return self.async_show_form(
            step_id="destination_extend" if extend else "destination",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(fields), user_input or suggested
            ),
            errors=errors,
            description_placeholders={
                "destination_raw": md(shipment.destination_raw or EMPTY),
                "pickup_location": md(shipment.pickup_location or EMPTY),
            },
        )

    async def async_step_destination_extend(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Formular-Schritt „Zielort ergaenzen" — gleiche Logik wie `destination`."""
        return await self.async_step_destination(user_input)

    async def async_step_destination_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Vorschau: Welche Sendungen der Zielort bekommt; dann speichern."""
        if (runtime := self._runtime()) is None:
            return self.async_abort(reason=ABORT_NOT_LOADED)
        destination = self._pending
        if destination is None or self._shipment_id is None:
            return self.async_abort(reason=ABORT_SHIPMENT_GONE)
        shipments = self._sorted_shipments(runtime)
        known = {shipment.id for shipment in shipments}

        if user_input is not None:
            selected = [value for value in user_input.get(FIELD_ASSIGN) or [] if value in known]
            runtime.store.async_set_destination_override(selected, destination.id)
            data = {
                CONF_DESTINATION_ID: destination.id,
                CONF_NAME: destination.name,
                **{key: list(getattr(destination, key)) for key in CONF_DESTINATION_RULES},
            }
            target = self.config_entry.subentries.get(self._target) if self._target else None
            # Beides loest ueber den Update-Listener einen Reload aus; der
            # Store wird beim Entladen geschrieben, die Zuordnung bleibt also.
            if target is not None:
                self.hass.config_entries.async_update_subentry(self.config_entry, target, data=data)
            else:
                self.hass.config_entries.async_add_subentry(
                    self.config_entry,
                    ConfigSubentry(
                        data=MappingProxyType(data),
                        subentry_type=SUBENTRY_DESTINATION,
                        title=destination.name,
                        unique_id=destination.id,
                    ),
                )
            return self.async_abort(
                reason=ABORT_DESTINATION_SAVED,
                description_placeholders={
                    "name": destination.name,
                    "count": str(len(selected)),
                },
            )

        matcher = AddressMatcher([destination])
        matched = [
            shipment.id
            for shipment in shipments
            if matcher.match(shipment.destination_raw, shipment.recipient_emails).destination_id
            == destination.id
        ]
        default = list(dict.fromkeys([self._shipment_id, *matched]))
        schema = vol.Schema(
            {
                vol.Optional(FIELD_ASSIGN, default=default): SelectSelector(
                    SelectSelectorConfig(
                        options=await self._async_shipment_options(runtime, shipments),
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="destination_confirm",
            data_schema=schema,
            description_placeholders={
                "name": md(destination.name),
                "matched": str(len(matched)),
            },
        )

    async def async_step_mails(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Die letzten verarbeiteten Mails je Postfach mit ihrem Ergebnis."""
        if (runtime := self._runtime()) is None:
            return self.async_abort(reason=ABORT_NOT_LOADED)
        logs = [
            (handle.title, handle.pipeline.log)
            for handle in runtime.sources.values()
            if handle.pipeline is not None
        ]
        if not logs:
            return self.async_abort(reason=ABORT_NO_MAILBOXES)

        mail_filter = (user_input or {}).get(FIELD_FILTER, FILTER_PROBLEMS)
        texts = await self._async_texts()
        carriers, _ = self._names(runtime)
        notice = ""
        if user_input is not None and user_input.get(FIELD_RESCAN):
            started = await runtime.coordinator.async_rescan_mailboxes()
            notice = texts.get("rescan_started", mailboxes=", ".join(started) or "-")
            logs = [
                (handle.title, handle.pipeline.log)
                for handle in runtime.sources.values()
                if handle.pipeline is not None
            ]
        elif any(
            isinstance(handle.source, ImapSource) and handle.source.rescanning
            for handle in runtime.sources.values()
        ):
            notice = texts.get("rescan_running")
        schema = vol.Schema(
            {
                vol.Required(FIELD_FILTER, default=mail_filter): SelectSelector(
                    SelectSelectorConfig(
                        options=[FILTER_PROBLEMS, FILTER_ALL],
                        translation_key="mail_filter",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(FIELD_RESCAN, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="mails",
            data_schema=schema,
            description_placeholders={
                "notice": notice,
                "mails": mail_log(
                    texts,
                    logs,
                    only_problems=mail_filter == FILTER_PROBLEMS,
                    carriers=carriers,
                ),
            },
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Der Abgleich der Tracking-Quellen: was 17TRACK mit jeder Nummer gemacht hat."""
        if (runtime := self._runtime()) is None:
            return self.async_abort(reason=ABORT_NOT_LOADED)
        handles = [
            handle
            for handle in runtime.sources.values()
            if handle.kind == SUBENTRY_API_SOURCE and handle.source is not None
        ]
        if not handles:
            return self.async_abort(reason=ABORT_NO_TRACKING_SOURCES)

        if user_input is not None and user_input.get(FIELD_REFRESH):
            await runtime.coordinator.async_refresh_sources([handle.key for handle in handles])
        texts = await self._async_texts()
        carriers, _ = self._names(runtime)
        sections = [
            (handle.title, handle.source.tracking_report())
            for handle in handles
            if handle.source is not None
        ]
        schema = vol.Schema({vol.Optional(FIELD_REFRESH, default=False): BooleanSelector()})
        return self.async_show_form(
            step_id="tracking",
            data_schema=schema,
            description_placeholders={
                "tracking": tracking_log(texts, sections, carriers=carriers),
            },
        )


def _action_selector(actions: list[str]) -> SelectSelector:
    """Auswahl der Aktion; die Texte stehen unter `selector.manage_action`."""
    return SelectSelector(
        SelectSelectorConfig(
            options=actions, translation_key="manage_action", mode=SelectSelectorMode.LIST
        )
    )
