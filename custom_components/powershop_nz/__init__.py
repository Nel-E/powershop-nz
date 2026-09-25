"""Powershop integration for Home Assistant."""
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .api import PowershopAPIClient, normalise_hourly_usage
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DATE,
    ATTR_DAYS,
    CONF_ACCOUNT_NUMBER,
    CONF_PROPERTY_ID,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    SERVICE_BACKFILL_ENERGY_STATISTICS,
    SERVICE_GET_HOURLY_USAGE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

_SERVICE_GET_HOURLY_USAGE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
        vol.Required(ATTR_DATE): cv.date,
    }
)

_SERVICE_BACKFILL_ENERGY_STATISTICS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
        vol.Optional(ATTR_DAYS, default=60): vol.All(
            vol.Coerce(int),
            vol.Range(min=1, max=365),
        ),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Powershop service actions."""
    hass.data.setdefault(DOMAIN, {})

    async def async_get_hourly_usage(call: ServiceCall) -> ServiceResponse:
        """Return hourly usage rows for a selected date."""
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        requested_date = call.data[ATTR_DATE]

        if entry_id is None:
            loaded_entries = [
                entry
                for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.state is ConfigEntryState.LOADED
            ]
            if not loaded_entries:
                raise ServiceValidationError("No loaded Powershop NZ config entry found")
            if len(loaded_entries) > 1:
                raise ServiceValidationError(
                    "Multiple loaded Powershop NZ config entries found; "
                    "specify config_entry_id in YAML mode"
                )
            entry = loaded_entries[0]
            entry_id = entry.entry_id
        else:
            entry = hass.config_entries.async_get_entry(entry_id)

        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError("Powershop NZ config entry not found")
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError("Powershop NZ config entry is not loaded")

        coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is None:
            raise ServiceValidationError("Powershop NZ coordinator is not available")

        account_number = entry.data.get(CONF_ACCOUNT_NUMBER)
        property_id = entry.data.get(CONF_PROPERTY_ID)
        if not account_number or not property_id:
            raise ServiceValidationError(
                "Powershop NZ account or property is not configured"
            )

        nodes = await coordinator.client.get_measurements_range(
            account_number,
            property_id,
            requested_date.isoformat(),
            requested_date.isoformat(),
            "HOUR_INTERVAL",
        )
        hourly_usage = normalise_hourly_usage(nodes)

        return {
            "date": requested_date.isoformat(),
            "hourly_usage_count": len(hourly_usage),
            "total_kwh": round(sum(row["kwh"] for row in hourly_usage), 3),
            "total_cost_incl_tax_estimated_nzd": round(
                sum(row["cost_incl_tax_estimated_nzd"] for row in hourly_usage),
                2,
            ),
            "hourly_usage": hourly_usage,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_HOURLY_USAGE,
        async_get_hourly_usage,
        schema=_SERVICE_GET_HOURLY_USAGE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    async def async_backfill_energy_statistics(call: ServiceCall) -> ServiceResponse:
        """Backfill Powershop hourly energy/cost statistics."""
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)

        if entry_id is None:
            loaded_entries = [
                entry
                for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.state is ConfigEntryState.LOADED
            ]
            if not loaded_entries:
                raise ServiceValidationError("No loaded Powershop NZ config entry found")
            if len(loaded_entries) > 1:
                raise ServiceValidationError(
                    "Multiple loaded Powershop NZ config entries found; "
                    "specify config_entry_id in YAML mode"
                )
            entry = loaded_entries[0]
            entry_id = entry.entry_id
        else:
            entry = hass.config_entries.async_get_entry(entry_id)

        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError("Powershop NZ config entry not found")
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError("Powershop NZ config entry is not loaded")

        coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is None:
            raise ServiceValidationError("Powershop NZ coordinator is not available")

        statistics_manager = getattr(coordinator, "statistics_manager", None)
        if statistics_manager is None:
            raise ServiceValidationError(
                "Powershop NZ statistics manager is not available"
            )

        return await statistics_manager.async_sync(days=call.data[ATTR_DAYS])

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL_ENERGY_STATISTICS,
        async_backfill_energy_statistics,
        schema=_SERVICE_BACKFILL_ENERGY_STATISTICS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Powershop from a config entry."""
    from .sensor import PowershopDataUpdateCoordinator
    from .statistics import PowershopStatisticsManager

    hass.data.setdefault(DOMAIN, {})

    client = PowershopAPIClient(refresh_token=entry.data[CONF_REFRESH_TOKEN])
    coordinator = PowershopDataUpdateCoordinator(hass, client, entry)

    # Raises ConfigEntryNotReady here (before platform forwarding) as HA requires
    await coordinator.async_config_entry_first_refresh()

    statistics_manager = PowershopStatisticsManager(hass, client, entry)
    coordinator.statistics_manager = statistics_manager

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    statistics_manager.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator:
            statistics_manager = getattr(coordinator, "statistics_manager", None)
            if statistics_manager is not None:
                await statistics_manager.async_shutdown()
            await coordinator.client.close()
    return unload_ok
