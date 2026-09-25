"""Home Assistant long-term statistics import for Powershop NZ."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import PowershopAPIClient, normalise_hourly_usage
from .const import CONF_ACCOUNT_NUMBER, CONF_PROPERTY_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

NZ_TZ = ZoneInfo("Pacific/Auckland")
INITIAL_BACKFILL_DAYS = 60
ROLLING_SYNC_DAYS = 30
SYNC_INTERVAL = timedelta(hours=12)
FETCH_CHUNK_DAYS = 4


def _safe_statistic_component(value: str) -> str:
    """Return a valid object-id component for an external statistic id."""
    cleaned = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return cleaned or "unknown"


def _parse_datetime(value: Any) -> datetime | None:
    """Parse a Powershop datetime and return it in UTC."""
    if not value or not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=NZ_TZ)
    return dt_util.as_utc(parsed)


class PowershopStatisticsManager:
    """Import Powershop interval data into Home Assistant long-term statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PowershopAPIClient,
        config_entry: ConfigEntry,
    ) -> None:
        self.hass = hass
        self.client = client
        self.config_entry = config_entry
        self._lock = asyncio.Lock()
        self._unsub_interval = None
        self._tasks: set[asyncio.Task[Any]] = set()

        account_number = str(config_entry.data[CONF_ACCOUNT_NUMBER])
        property_id = str(config_entry.data[CONF_PROPERTY_ID])
        id_prefix = (
            f"{_safe_statistic_component(account_number)}_"
            f"{_safe_statistic_component(property_id)}"
        )
        self.consumption_statistic_id = (
            f"{DOMAIN}:{id_prefix}_energy_consumption"
        )
        self.cost_statistic_id = f"{DOMAIN}:{id_prefix}_energy_cost"

        name_suffix = f" ({account_number})"
        self._consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Powershop NZ electricity consumption{name_suffix}",
            source=DOMAIN,
            statistic_id=self.consumption_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        self._cost_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Powershop NZ electricity cost{name_suffix}",
            source=DOMAIN,
            statistic_id=self.cost_statistic_id,
            unit_class=None,
            unit_of_measurement=None,
        )

    def start(self) -> None:
        """Start initial and periodic statistics synchronisation."""
        if self._unsub_interval is not None:
            return

        self._schedule_sync("initial")
        self._unsub_interval = async_track_time_interval(
            self.hass,
            self._handle_interval,
            SYNC_INTERVAL,
        )

    async def async_shutdown(self) -> None:
        """Stop periodic work and cancel in-flight background imports."""
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    @callback
    def _handle_interval(self, _now: datetime) -> None:
        """Schedule a normal rolling import."""
        self._schedule_sync("scheduled")

    @callback
    def _schedule_sync(self, reason: str) -> None:
        """Schedule a protected background import."""
        task = self.hass.async_create_task(self._async_safe_sync(reason))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _async_safe_sync(self, reason: str) -> None:
        """Run a background sync without taking the integration offline on errors."""
        try:
            result = await self.async_sync()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Powershop statistics %s sync failed", reason)
            return

        _LOGGER.debug(
            "Powershop statistics %s sync complete: %s",
            reason,
            result,
        )

    async def async_sync(self, days: int | None = None) -> dict[str, Any]:
        """Import hourly Powershop usage and cost into long-term statistics.

        If *days* is omitted, the first import backfills 60 days. Subsequent
        automatic imports re-read the latest 30 days so Powershop can replace
        estimated readings with actual meter data.
        """
        async with self._lock:
            if days is None:
                has_statistics = await self._async_has_statistics()
                days = ROLLING_SYNC_DAYS if has_statistics else INITIAL_BACKFILL_DAYS

            days = max(1, int(days))
            local_today = datetime.now(NZ_TZ).date()
            start_date = local_today - timedelta(days=days - 1)

            rows = await self._async_fetch_hourly_rows(start_date, local_today)
            rows = self._prepare_rows(rows)

            if not rows:
                return {
                    "days": days,
                    "hourly_rows": 0,
                    "consumption_statistic_id": self.consumption_statistic_id,
                    "cost_statistic_id": self.cost_statistic_id,
                }

            first_start = rows[0]["_start"]
            base_sums = await self._async_get_base_sums(first_start)
            consumption_sum = base_sums[self.consumption_statistic_id]
            cost_sum = base_sums[self.cost_statistic_id]

            consumption_statistics: list[StatisticData] = []
            cost_statistics: list[StatisticData] = []

            for row in rows:
                start = row["_start"]
                consumption_state = max(0.0, float(row.get("kwh") or 0.0))
                cost_state = max(
                    0.0,
                    float(row.get("cost_incl_tax_estimated_nzd") or 0.0),
                )

                consumption_sum = round(consumption_sum + consumption_state, 6)
                cost_sum = round(cost_sum + cost_state, 6)

                consumption_statistics.append(
                    StatisticData(
                        start=start,
                        state=consumption_state,
                        sum=consumption_sum,
                    )
                )
                cost_statistics.append(
                    StatisticData(
                        start=start,
                        state=cost_state,
                        sum=cost_sum,
                    )
                )

            async_add_external_statistics(
                self.hass,
                self._consumption_metadata,
                consumption_statistics,
            )
            async_add_external_statistics(
                self.hass,
                self._cost_metadata,
                cost_statistics,
            )

            return {
                "days": days,
                "hourly_rows": len(rows),
                "first_hour": rows[0]["_start"].isoformat(),
                "last_hour": rows[-1]["_start"].isoformat(),
                "consumption_statistic_id": self.consumption_statistic_id,
                "cost_statistic_id": self.cost_statistic_id,
            }

    async def _async_has_statistics(self) -> bool:
        """Return whether the consumption statistic already has data."""
        stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            self.consumption_statistic_id,
            True,
            {"sum"},
        )
        return bool(stats.get(self.consumption_statistic_id))

    async def _async_fetch_hourly_rows(
        self,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        """Fetch hourly data in small date chunks safe for GraphQL connection limits."""
        account_number = self.config_entry.data[CONF_ACCOUNT_NUMBER]
        property_id = self.config_entry.data[CONF_PROPERTY_ID]

        rows: list[dict[str, Any]] = []
        chunk_start = start_date
        while chunk_start <= end_date:
            chunk_end = min(
                chunk_start + timedelta(days=FETCH_CHUNK_DAYS - 1),
                end_date,
            )
            nodes = await self.client.get_measurements_range(
                account_number,
                property_id,
                chunk_start.isoformat(),
                chunk_end.isoformat(),
                "HOUR_INTERVAL",
            )
            rows.extend(normalise_hourly_usage(nodes))
            chunk_start = chunk_end + timedelta(days=1)

        return rows

    def _prepare_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Parse, de-duplicate, sort, and remove future/incomplete intervals."""
        now_utc = datetime.now(timezone.utc)
        prepared: dict[datetime, dict[str, Any]] = {}

        for row in rows:
            start = _parse_datetime(row.get("start_at"))
            if start is None:
                continue

            end = _parse_datetime(row.get("end_at"))
            if end is not None and end > now_utc:
                continue
            if end is None and start >= now_utc:
                continue

            prepared[start] = {**row, "_start": start}

        return [prepared[key] for key in sorted(prepared)]

    async def _async_get_base_sums(
        self,
        first_start: datetime,
    ) -> dict[str, float]:
        """Find cumulative sums immediately before the first imported hour."""
        statistic_ids = {
            self.consumption_statistic_id,
            self.cost_statistic_id,
        }
        bases = {statistic_id: 0.0 for statistic_id in statistic_ids}
        unresolved = set(statistic_ids)

        # Normal rolling-update case: the first hour already exists. Its
        # previous cumulative value is exactly sum - state.
        exact = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            first_start,
            first_start + timedelta(seconds=1),
            statistic_ids,
            "hour",
            None,
            {"state", "sum"},
        )
        for statistic_id in tuple(unresolved):
            records = exact.get(statistic_id, [])
            if not records:
                continue
            record = records[0]
            if record.get("sum") is None or record.get("state") is None:
                continue
            bases[statistic_id] = float(record["sum"]) - float(record["state"])
            unresolved.remove(statistic_id)

        if not unresolved:
            return bases

        # If the first imported hour is new, use the latest stored sum before
        # it. A 35-day lookback covers the normal 30-day correction window.
        previous = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            first_start - timedelta(days=35),
            first_start,
            unresolved,
            "hour",
            None,
            {"sum"},
        )
        for statistic_id in tuple(unresolved):
            records = previous.get(statistic_id, [])
            if records and records[-1].get("sum") is not None:
                bases[statistic_id] = float(records[-1]["sum"])
                unresolved.remove(statistic_id)

        if not unresolved:
            return bases

        # Rare recovery path for a long data gap or a manual import of an older
        # range: search all earlier statistics for the previous cumulative sum.
        history = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            datetime(1970, 1, 1, tzinfo=timezone.utc),
            first_start,
            unresolved,
            "hour",
            None,
            {"sum"},
        )
        for statistic_id in unresolved:
            records = history.get(statistic_id, [])
            if records and records[-1].get("sum") is not None:
                bases[statistic_id] = float(records[-1]["sum"])

        return bases
