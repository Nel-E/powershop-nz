"""Home Assistant long-term statistics import for Powershop NZ."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
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

from .api import (
    PowershopAPIClient,
    measurement_standing_charge_nzd,
    measurement_usage_cost_nzd,
)
from .const import CONF_ACCOUNT_NUMBER, CONF_PROPERTY_ID, DOMAIN
from .tou import extract_agreement_tou, extract_interval_band_entries, safe_stat_key

_LOGGER = logging.getLogger(__name__)

NZ_TZ = ZoneInfo("Pacific/Auckland")
INITIAL_BACKFILL_DAYS = 60
ROLLING_SYNC_DAYS = 30
SYNC_INTERVAL = timedelta(hours=12)
FETCH_CHUNK_DAYS = 7


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


def _format_latest(rows: list[Any]) -> dict[str, Any] | None:
    """Return a service-response-friendly representation of one stat row."""
    if not rows:
        return None
    row = rows[0]
    start = row.get("start")
    return {
        "start": (
            dt_util.utc_from_timestamp(start).isoformat()
            if isinstance(start, (int, float))
            else start.isoformat()
            if isinstance(start, datetime)
            else None
        ),
        "state": row.get("state"),
        "sum": row.get("sum"),
    }


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
        self._id_prefix = (
            f"{_safe_statistic_component(account_number)}_"
            f"{_safe_statistic_component(property_id)}"
        )
        self._name_suffix = f" ({account_number})"

        # Keep the v2.2 IDs stable so existing Energy Dashboard configuration
        # continues to work after upgrading.
        self.consumption_statistic_id = (
            f"{DOMAIN}:{self._id_prefix}_energy_consumption"
        )
        self.cost_statistic_id = (
            f"{DOMAIN}:{self._id_prefix}_energy_cost"
        )
        self.standing_charge_statistic_id = (
            f"{DOMAIN}:{self._id_prefix}_standing_charge_cost"
        )

        self._consumption_metadata = self._energy_metadata(
            self.consumption_statistic_id,
            f"Powershop NZ electricity consumption{self._name_suffix}",
        )
        self._cost_metadata = self._cost_metadata_for(
            self.cost_statistic_id,
            f"Powershop NZ electricity cost{self._name_suffix}",
        )
        self._standing_metadata = self._cost_metadata_for(
            self.standing_charge_statistic_id,
            f"Powershop NZ standing charge cost{self._name_suffix}",
        )

    def _energy_metadata(self, statistic_id: str, name: str) -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=name,
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

    def _cost_metadata_for(self, statistic_id: str, name: str) -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=name,
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=None,
            unit_of_measurement=None,
        )

    def _period_energy_id(self, period_key: str) -> str:
        return (
            f"{DOMAIN}:{self._id_prefix}_energy_consumption_"
            f"{safe_stat_key(period_key)}"
        )

    def _period_cost_id(self, period_key: str) -> str:
        return (
            f"{DOMAIN}:{self._id_prefix}_energy_cost_"
            f"{safe_stat_key(period_key)}"
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
        # This sync can take several minutes during the initial 60-day
        # migration. Mark it as a background task so Home Assistant startup is
        # not held open waiting for the import to finish.
        task = self.hass.async_create_background_task(
            self._async_safe_sync(reason),
            f"{DOMAIN}-statistics-{reason}",
        )
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

        _LOGGER.debug("Powershop statistics %s sync complete: %s", reason, result)

    async def async_sync(self, days: int | None = None) -> dict[str, Any]:
        """Import Powershop consumption, total cost, daily charge and TOU stats.

        Existing v2.2 total consumption/cost statistic IDs are retained. On the
        first v2.3 sync, absence of the standing-charge statistic triggers a
        60-day migration backfill so existing total-cost history is rewritten
        with the daily charge included. Later automatic syncs re-read 30 days
        so Powershop estimate-to-actual corrections can overwrite history.
        """
        async with self._lock:
            if days is None:
                has_consumption = await self._async_has_statistic(
                    self.consumption_statistic_id
                )
                has_standing = await self._async_has_statistic(
                    self.standing_charge_statistic_id
                )
                days = (
                    ROLLING_SYNC_DAYS
                    if has_consumption and has_standing
                    else INITIAL_BACKFILL_DAYS
                )

            days = max(1, int(days))
            local_today = datetime.now(NZ_TZ).date()
            start_date = local_today - timedelta(days=days - 1)

            account_number = self.config_entry.data[CONF_ACCOUNT_NUMBER]
            property_id = self.config_entry.data[CONF_PROPERTY_ID]

            agreement_data = await self.client.get_agreements(
                account_number, property_id
            )
            tou = extract_agreement_tou(agreement_data)

            # Half-hour data lets tariff boundaries such as 09:30 be allocated
            # accurately. Fall back to hourly if this account does not expose
            # THIRTY_MIN_INTERVAL.
            try:
                interval_nodes = await self._async_fetch_interval_nodes(
                    start_date, local_today, "THIRTY_MIN_INTERVAL"
                )
            except Exception as err:
                _LOGGER.warning(
                    "Powershop half-hour data unavailable; falling back to hourly: %s",
                    err,
                )
                interval_nodes = []

            source_frequency = "THIRTY_MIN_INTERVAL"
            if not interval_nodes:
                source_frequency = "HOUR_INTERVAL"
                interval_nodes = await self._async_fetch_interval_nodes(
                    start_date, local_today, source_frequency
                )

            daily_nodes = await self.client.get_measurements_date_range(
                account_number,
                property_id,
                start_date.isoformat(),
                local_today.isoformat(),
            )

            hourly, detected_periods = self._aggregate_interval_nodes(
                interval_nodes, tou
            )
            standing_charge_days = self._merge_daily_charges(
                hourly, daily_nodes, tou, local_today
            )

            if not hourly:
                return {
                    "days": days,
                    "hourly_rows": 0,
                    "source_frequency": source_frequency,
                    "consumption_statistic_id": self.consumption_statistic_id,
                    "cost_statistic_id": self.cost_statistic_id,
                    "standing_charge_statistic_id": self.standing_charge_statistic_id,
                }

            period_keys = set((tou.get("rate_bands") or {}).keys())
            period_keys.update(detected_periods)
            period_keys = {key for key in period_keys if key}

            timeline = sorted(hourly)
            first_start = timeline[0]

            period_ids: dict[str, dict[str, str]] = {
                key: {
                    "consumption": self._period_energy_id(key),
                    "cost": self._period_cost_id(key),
                }
                for key in sorted(period_keys)
            }

            all_statistic_ids = {
                self.consumption_statistic_id,
                self.cost_statistic_id,
                self.standing_charge_statistic_id,
            }
            for ids in period_ids.values():
                all_statistic_ids.update(ids.values())

            base_sums = await self._async_get_base_sums(
                first_start, all_statistic_ids
            )

            consumption_sum = base_sums[self.consumption_statistic_id]
            cost_sum = base_sums[self.cost_statistic_id]
            standing_sum = base_sums[self.standing_charge_statistic_id]

            period_energy_sums = {
                key: base_sums[ids["consumption"]]
                for key, ids in period_ids.items()
            }
            period_cost_sums = {
                key: base_sums[ids["cost"]]
                for key, ids in period_ids.items()
            }

            consumption_stats: list[StatisticData] = []
            cost_stats: list[StatisticData] = []
            standing_stats: list[StatisticData] = []
            period_energy_stats: dict[str, list[StatisticData]] = defaultdict(list)
            period_cost_stats: dict[str, list[StatisticData]] = defaultdict(list)

            for start in timeline:
                bucket = hourly[start]
                consumption_state = max(0.0, float(bucket["kwh"]))
                usage_cost_state = max(0.0, float(bucket["usage_cost_nzd"]))
                standing_state = max(0.0, float(bucket["standing_charge_nzd"]))
                total_cost_state = usage_cost_state + standing_state

                consumption_sum = round(consumption_sum + consumption_state, 6)
                cost_sum = round(cost_sum + total_cost_state, 6)
                standing_sum = round(standing_sum + standing_state, 6)

                consumption_stats.append(
                    StatisticData(
                        start=start,
                        state=consumption_state,
                        sum=consumption_sum,
                    )
                )
                cost_stats.append(
                    StatisticData(
                        start=start,
                        state=round(total_cost_state, 6),
                        sum=cost_sum,
                    )
                )
                standing_stats.append(
                    StatisticData(
                        start=start,
                        state=round(standing_state, 6),
                        sum=standing_sum,
                    )
                )

                for key in sorted(period_keys):
                    period_values = bucket["periods"].get(key, (0.0, 0.0))
                    period_kwh = max(0.0, float(period_values[0]))
                    period_cost = max(0.0, float(period_values[1]))

                    period_energy_sums[key] = round(
                        period_energy_sums[key] + period_kwh, 6
                    )
                    period_cost_sums[key] = round(
                        period_cost_sums[key] + period_cost, 6
                    )

                    period_energy_stats[key].append(
                        StatisticData(
                            start=start,
                            state=period_kwh,
                            sum=period_energy_sums[key],
                        )
                    )
                    period_cost_stats[key].append(
                        StatisticData(
                            start=start,
                            state=period_cost,
                            sum=period_cost_sums[key],
                        )
                    )

            async_add_external_statistics(
                self.hass, self._consumption_metadata, consumption_stats
            )
            async_add_external_statistics(
                self.hass, self._cost_metadata, cost_stats
            )
            async_add_external_statistics(
                self.hass, self._standing_metadata, standing_stats
            )

            rate_bands = tou.get("rate_bands") or {}
            for key in sorted(period_keys):
                band = rate_bands.get(key) or {}
                display_name = str(
                    band.get("name")
                    or key.replace("_", " ").title()
                )
                ids = period_ids[key]

                async_add_external_statistics(
                    self.hass,
                    self._energy_metadata(
                        ids["consumption"],
                        (
                            f"Powershop NZ {display_name} consumption"
                            f"{self._name_suffix}"
                        ),
                    ),
                    period_energy_stats[key],
                )
                async_add_external_statistics(
                    self.hass,
                    self._cost_metadata_for(
                        ids["cost"],
                        f"Powershop NZ {display_name} cost{self._name_suffix}",
                    ),
                    period_cost_stats[key],
                )

            recorder = get_instance(self.hass)
            await recorder.async_block_till_done()
            persisted = await self._async_get_persisted_status()

            tariff_periods = []
            for key in sorted(period_keys):
                band = rate_bands.get(key) or {}
                tariff_periods.append(
                    {
                        "key": key,
                        "name": band.get("name")
                        or key.replace("_", " ").title(),
                        "bucket": band.get("bucket"),
                        "rate_nzd_per_kwh": band.get("rate_nzd_per_kwh"),
                        "consumption_statistic_id": period_ids[key]["consumption"],
                        "cost_statistic_id": period_ids[key]["cost"],
                    }
                )

            return {
                "days": days,
                "hourly_rows": len(timeline),
                "source_frequency": source_frequency,
                "first_hour": timeline[0].isoformat(),
                "last_hour": timeline[-1].isoformat(),
                "standing_charge_days": standing_charge_days,
                "consumption_statistic_id": self.consumption_statistic_id,
                "cost_statistic_id": self.cost_statistic_id,
                "standing_charge_statistic_id": self.standing_charge_statistic_id,
                "tariff_periods": tariff_periods,
                **persisted,
            }

    async def _async_fetch_interval_nodes(
        self,
        start_date: date,
        end_date: date,
        frequency: str,
    ) -> list[dict[str, Any]]:
        """Fetch interval nodes in date chunks safe for GraphQL limits."""
        account_number = self.config_entry.data[CONF_ACCOUNT_NUMBER]
        property_id = self.config_entry.data[CONF_PROPERTY_ID]

        nodes: list[dict[str, Any]] = []
        chunk_start = start_date
        while chunk_start <= end_date:
            chunk_end = min(
                chunk_start + timedelta(days=FETCH_CHUNK_DAYS - 1),
                end_date,
            )
            nodes.extend(
                await self.client.get_measurements_range(
                    account_number,
                    property_id,
                    chunk_start.isoformat(),
                    chunk_end.isoformat(),
                    frequency,
                )
            )
            chunk_start = chunk_end + timedelta(days=1)
        return nodes

    def _empty_hour_bucket(self) -> dict[str, Any]:
        return {
            "kwh": 0.0,
            "usage_cost_nzd": 0.0,
            "standing_charge_nzd": 0.0,
            "periods": defaultdict(lambda: [0.0, 0.0]),
        }

    def _aggregate_interval_nodes(
        self,
        nodes: list[dict[str, Any]],
        tou: dict[str, Any],
    ) -> tuple[dict[datetime, dict[str, Any]], set[str]]:
        """Aggregate raw interval nodes into UTC-aligned hourly buckets."""
        now_utc = datetime.now(timezone.utc)
        hourly: dict[datetime, dict[str, Any]] = {}
        detected_periods: set[str] = set()

        for node in nodes:
            start_utc = _parse_datetime(node.get("startAt"))
            if start_utc is None:
                continue

            end_utc = _parse_datetime(node.get("endAt"))
            if end_utc is not None and end_utc > now_utc:
                continue
            if end_utc is None and start_utc >= now_utc:
                continue

            local_start = start_utc.astimezone(NZ_TZ)
            local_hour = local_start.replace(
                minute=0, second=0, microsecond=0
            )
            hour_start = local_hour.astimezone(timezone.utc)

            bucket = hourly.setdefault(hour_start, self._empty_hour_bucket())

            try:
                kwh = max(0.0, float(node.get("value") or 0))
            except (TypeError, ValueError):
                kwh = 0.0

            bucket["kwh"] += kwh
            bucket["usage_cost_nzd"] += measurement_usage_cost_nzd(node)

            for period_key, period_kwh, period_cost in extract_interval_band_entries(
                node, tou, local_start
            ):
                if not period_key:
                    continue
                detected_periods.add(period_key)
                bucket["periods"][period_key][0] += period_kwh
                bucket["periods"][period_key][1] += period_cost

        return hourly, detected_periods

    def _merge_daily_charges(
        self,
        hourly: dict[datetime, dict[str, Any]],
        daily_nodes: list[dict[str, Any]],
        tou: dict[str, Any],
        local_today: date,
    ) -> int:
        """Merge one authoritative standing charge into each local day."""
        charge_by_date: dict[date, float] = {}
        current_rate = float(tou.get("standing_rate_nzd") or 0.0)

        for node in daily_nodes:
            start_utc = _parse_datetime(node.get("startAt"))
            if start_utc is None:
                continue
            local_date = start_utc.astimezone(NZ_TZ).date()
            if local_date > local_today:
                continue

            charge = measurement_standing_charge_nzd(node)
            if local_date == local_today and current_rate > 0:
                # The current daily node may be prorated while the day is still
                # in progress. The contract standing charge is billed per day.
                charge = current_rate
            if charge > 0:
                charge_by_date[local_date] = charge

        # If today's daily measurement has not arrived yet, the active
        # agreement still tells us the full standing charge for today.
        if local_today not in charge_by_date and current_rate > 0:
            charge_by_date[local_today] = current_rate

        for local_date, charge in charge_by_date.items():
            local_midnight = datetime.combine(
                local_date, time.min, tzinfo=NZ_TZ
            )
            hour_start = local_midnight.astimezone(timezone.utc)
            bucket = hourly.setdefault(hour_start, self._empty_hour_bucket())
            bucket["standing_charge_nzd"] = charge

        return len(charge_by_date)

    async def _async_get_persisted_status(self) -> dict[str, Any]:
        """Read back the latest main statistics from Recorder."""
        recorder = get_instance(self.hass)

        async def latest(statistic_id: str) -> dict[str, Any] | None:
            rows = await recorder.async_add_executor_job(
                get_last_statistics,
                self.hass,
                1,
                statistic_id,
                True,
                {"state", "sum"},
            )
            return _format_latest(rows.get(statistic_id, []))

        consumption_latest = await latest(self.consumption_statistic_id)
        cost_latest = await latest(self.cost_statistic_id)
        standing_latest = await latest(self.standing_charge_statistic_id)

        return {
            "recorder_verified": bool(
                consumption_latest and cost_latest and standing_latest
            ),
            "latest_consumption_statistic": consumption_latest,
            "latest_cost_statistic": cost_latest,
            "latest_standing_charge_statistic": standing_latest,
        }

    async def _async_has_statistic(self, statistic_id: str) -> bool:
        """Return whether a statistic already has data."""
        stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            statistic_id,
            True,
            {"sum"},
        )
        return bool(stats.get(statistic_id))

    async def _async_get_base_sums(
        self,
        first_start: datetime,
        statistic_ids: set[str],
    ) -> dict[str, float]:
        """Find cumulative sums immediately before the first imported hour."""
        bases = {statistic_id: 0.0 for statistic_id in statistic_ids}
        unresolved = set(statistic_ids)

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

        previous = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            first_start - timedelta(days=70),
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
