"""Support for OPNsense."""

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import awesomeversion
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import (
    CONF_DEVICE_TRACKER_ENABLED,
    CONF_DEVICE_TRACKER_SCAN_INTERVAL,
    CONF_TLS_INSECURE,
    COORDINATOR,
    DEFAULT_DEVICE_TRACKER_ENABLED,
    DEFAULT_DEVICE_TRACKER_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TLS_INSECURE,
    DEFAULT_VERIFY_SSL,
    DEVICE_TRACKER_COORDINATOR,
    DOMAIN,
    LOADED_PLATFORMS,
    OPNSENSE_CLIENT,
    OPNSENSE_MIN_FIRMWARE,
    PLATFORMS,
    SHOULD_RELOAD,
    UNDO_UPDATE_LISTENER,
    VERSION,
)
from .coordinator import OPNsenseDataUpdateCoordinator
from .helpers import dict_get
from .pyopnsense import OPNsenseClient
from .services import async_setup_services

_LOGGER: logging.Logger = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    if hass.data[DOMAIN][entry.entry_id].get(SHOULD_RELOAD, True):
        hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
    else:
        hass.data[DOMAIN][entry.entry_id][SHOULD_RELOAD] = True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    await async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OPNsense from a config entry."""
    config = entry.data
    options = entry.options

    url: str = config[CONF_URL]
    username: str = config[CONF_USERNAME]
    password: str = config[CONF_PASSWORD]
    verify_ssl: bool = config.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
    device_tracker_enabled: bool = options.get(
        CONF_DEVICE_TRACKER_ENABLED, DEFAULT_DEVICE_TRACKER_ENABLED
    )
    client = OPNsenseClient(
        url=url,
        username=username,
        password=password,
        session=async_create_clientsession(hass, raise_for_status=False),
        opts={"verify_ssl": verify_ssl},
    )

    scan_interval: int = options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    _LOGGER.info(f"Starting hass-opnsense {VERSION}")

    coordinator = OPNsenseDataUpdateCoordinator(
        hass=hass,
        name=f"{entry.title} state",
        update_interval=timedelta(seconds=scan_interval),
        client=client,
    )

    platforms: list = PLATFORMS.copy()
    device_tracker_coordinator = None
    if not device_tracker_enabled and "device_tracker" in platforms:
        platforms.remove("device_tracker")
    else:
        device_tracker_scan_interval = options.get(
            CONF_DEVICE_TRACKER_SCAN_INTERVAL, DEFAULT_DEVICE_TRACKER_SCAN_INTERVAL
        )

        device_tracker_coordinator = OPNsenseDataUpdateCoordinator(
            hass=hass,
            name=f"{entry.title} Device Tracker state",
            update_interval=timedelta(seconds=device_tracker_scan_interval),
            client=client,
            device_tracker_coordinator=True,
        )

    undo_listener = entry.add_update_listener(_async_update_listener)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        COORDINATOR: coordinator,
        DEVICE_TRACKER_COORDINATOR: device_tracker_coordinator,
        OPNSENSE_CLIENT: client,
        UNDO_UPDATE_LISTENER: [undo_listener],
        LOADED_PLATFORMS: platforms,
    }

    # Fetch initial data so we have data when entities subscribe
    await coordinator.async_config_entry_first_refresh()
    if device_tracker_enabled:
        # Fetch initial data so we have data when entities subscribe
        await device_tracker_coordinator.async_config_entry_first_refresh()

    firmware: str | None = coordinator.data.get("host_firmware_version", None)
    _LOGGER.info(f"OPNsense Firmware {firmware}")
    try:
        if awesomeversion.AwesomeVersion(firmware) < awesomeversion.AwesomeVersion(
            OPNSENSE_MIN_FIRMWARE
        ):
            async_create_issue(
                hass,
                DOMAIN,
                f"opnsense_{firmware}_below_min_firmware_{OPNSENSE_MIN_FIRMWARE}",
                is_fixable=False,
                is_persistent=False,
                issue_domain=DOMAIN,
                severity=IssueSeverity.WARNING,
                translation_key="below_min_firmware",
                translation_placeholders={
                    "version": VERSION,
                    "min_firmware": OPNSENSE_MIN_FIRMWARE,
                    "firmware": firmware,
                },
            )
    except awesomeversion.exceptions.AwesomeVersionCompareException:
        pass

    await hass.config_entries.async_forward_entry_setups(entry, platforms)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    platforms = hass.data[DOMAIN][entry.entry_id][LOADED_PLATFORMS]
    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)

    for listener in hass.data[DOMAIN][entry.entry_id][UNDO_UPDATE_LISTENER]:
        listener()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate an old config entry."""
    version = config_entry.version

    _LOGGER.debug("Migrating from version %s", version)

    # 1 -> 2: tls_insecure to verify_ssl
    if version == 1:
        version = config_entry.version = 2
        tls_insecure = config_entry.data.get(CONF_TLS_INSECURE, DEFAULT_TLS_INSECURE)
        data = dict(config_entry.data)

        # remove tls_insecure
        if CONF_TLS_INSECURE in data.keys():
            del data[CONF_TLS_INSECURE]

        # add verify_ssl
        if CONF_VERIFY_SSL not in data.keys():
            data[CONF_VERIFY_SSL] = not tls_insecure

        hass.config_entries.async_update_entry(
            config_entry,
            data=data,
        )

        _LOGGER.info("Migration to version %s successful", version)

    return True


class OPNsenseEntity(CoordinatorEntity[OPNsenseDataUpdateCoordinator]):
    """Base entity for OPNsense"""

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: OPNsenseDataUpdateCoordinator,
        unique_id_suffix: str | None = None,
        name_suffix: str | None = None,
    ) -> None:
        self.config_entry: ConfigEntry = config_entry
        self.coordinator: OPNsenseDataUpdateCoordinator = coordinator
        if unique_id_suffix:
            self._attr_unique_id: str = slugify(
                f"{self.opnsense_device_unique_id}_{unique_id_suffix}"
            )
        if name_suffix:
            self._attr_name: str = (
                f"{self.opnsense_device_name or 'OPNsense'} {name_suffix}"
            )
        self._client: OPNsenseClient | None = None
        self._attr_extra_state_attributes: Mapping[str, Any] = {}
        self._available: bool = False
        super().__init__(self.coordinator, self._attr_unique_id)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def device_info(self) -> Mapping[str, Any]:
        """Device info for the firewall."""
        state: Mapping[str, Any] = self.coordinator.data
        model: str = "OPNsense"
        manufacturer: str = "Deciso B.V."
        if state is None:
            firmware: str | None = None
        else:
            firmware: str | None = state.get("host_firmware_version", None)

        device_info: Mapping[str, Any] = {
            "identifiers": {(DOMAIN, self.opnsense_device_unique_id)},
            "name": self.opnsense_device_name,
            "configuration_url": self.config_entry.data.get("url", None),
        }

        device_info["model"] = model
        device_info["manufacturer"] = manufacturer
        device_info["sw_version"] = firmware

        return device_info

    @property
    def opnsense_device_name(self) -> str:
        if self.config_entry.title and len(self.config_entry.title) > 0:
            return self.config_entry.title
        return self._get_opnsense_state_value("system_info.name")

    @property
    def opnsense_device_unique_id(self) -> str | None:
        return self._get_opnsense_state_value("system_info.device_id")

    def _get_opnsense_state_value(self, path, default=None):
        state = self.coordinator.data
        value = dict_get(state, path, default)

        return value

    def _get_opnsense_client(self) -> OPNsenseClient | None:
        if self.hass is None:
            return None
        return self.hass.data[DOMAIN][self.config_entry.entry_id][OPNSENSE_CLIENT]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._client is None:
            self._client: OPNsenseClient = self._get_opnsense_client()
        if self._client is None:
            _LOGGER.error("Unable to get client in async_added_to_hass.")
        self._handle_coordinator_update()
