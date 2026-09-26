"""Time-of-use rate helpers for Powershop NZ."""
from __future__ import annotations

from datetime import datetime
import re
from typing import Any


def safe_stat_key(value: str) -> str:
    """Return a stable statistic-id component."""
    cleaned = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return cleaned or "rate"


def canonical_band_key(bucket: str, label: str) -> str:
    """Return a stable human-friendly key for a Powershop rate band."""
    upper_bucket = bucket.upper().strip()
    lower_label = label.lower().strip()

    # Kraken bucket names commonly use these prefixes.
    if upper_bucket.startswith("OPK"):
        return "off_peak"
    if upper_bucket.startswith("PK"):
        return "peak"
    if upper_bucket.startswith("N"):
        return "night"
    if upper_bucket.startswith("CON"):
        return "controlled"

    if "off peak" in lower_label or "off-peak" in lower_label or "offpeak" in lower_label:
        return "off_peak"
    if "peak" in lower_label:
        return "peak"
    if "night" in lower_label:
        return "night"
    if "controlled" in lower_label:
        return "controlled"
    if "standard" in lower_label or "normal" in lower_label:
        return "standard"

    return safe_stat_key(label or bucket or "rate")


def _parse_rate_cents(rate: dict[str, Any]) -> float | None:
    """Return the API rate in cents per kWh/day where available."""
    raw = rate.get("rateIncludingTax")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass

    formatted = rate.get("formattedRateIncludingTax") or ""
    match = re.search(r"(\d+(?:\.\d+)?)", str(formatted))
    if not match:
        return None

    value = float(match.group(1))
    # Formatted values are commonly "$0.x/kWh"; convert dollars to cents.
    if "$" in str(formatted):
        value *= 100
    return value


def extract_agreement_tou(account_data: dict[str, Any]) -> dict[str, Any]:
    """Extract rate bands and TOU schedule from the first active agreement."""
    property_node = account_data.get("property") or {}
    agreement: dict[str, Any] = {}
    for meter_point in property_node.get("meterPoints") or []:
        candidate = meter_point.get("activeAgreement") or {}
        if candidate:
            agreement = candidate
            break

    rate_bands: dict[str, dict[str, Any]] = {}
    bucket_to_key: dict[str, str] = {}
    standing_rate_nzd: float | None = None

    for rate in agreement.get("rates") or []:
        band_category = str(rate.get("bandCategory") or "").upper()
        unit_type = str(rate.get("unitType") or "")
        bucket = str(rate.get("touBucketName") or "").strip()
        label = (
            rate.get("displayLabel")
            or rate.get("label")
            or bucket
            or ("Controlled" if "CONSUMPTION" in band_category else "Rate")
        )
        rate_cents = _parse_rate_cents(rate)

        if (
            "STANDING" in band_category
            or "days on supply" in unit_type.lower()
            or "daily" in str(label).lower()
            or "standing" in str(label).lower()
        ):
            if rate_cents is not None:
                standing_rate_nzd = round(rate_cents / 100, 6)
            continue

        is_consumption = (
            "CONSUMPTION" in band_category
            or "kilowatt" in unit_type.lower()
            or "kwh" in unit_type.lower()
        )
        if not is_consumption:
            continue

        base_key = canonical_band_key(bucket, str(label))
        existing = rate_bands.get(base_key)
        rounded_rate = round(rate_cents, 6) if rate_cents is not None else None

        # Powershop can expose separate weekday/weekend bucket names for the
        # same semantic tariff at the same price (e.g. WDDOPK16 and WE24, both
        # 25c/kWh). Collapse those into one Off Peak statistic.
        if existing is not None:
            existing_rate = existing.get("rate_c_per_kwh")
            same_rate = (
                existing_rate is None
                or rounded_rate is None
                or abs(float(existing_rate) - float(rounded_rate)) < 0.0001
            )
            if same_rate:
                if bucket:
                    bucket_to_key[bucket] = base_key
                    buckets = existing.setdefault("buckets", [])
                    if bucket not in buckets:
                        buckets.append(bucket)
                labels = existing.setdefault("source_labels", [])
                if str(label) not in labels:
                    labels.append(str(label))
                continue

        key = base_key
        if key in rate_bands:
            suffix_source = safe_stat_key(bucket or str(label))
            key = f"{base_key}_{suffix_source}"
            suffix = 2
            candidate = key
            while candidate in rate_bands:
                candidate = f"{key}_{suffix}"
                suffix += 1
            key = candidate

        canonical_name = {
            "off_peak": "Off Peak",
            "peak": "Peak",
            "night": "Night",
            "controlled": "Controlled",
            "standard": "Standard",
        }.get(base_key, str(label))

        band = {
            "key": key,
            "name": canonical_name if key == base_key else str(label),
            "bucket": bucket,
            "buckets": [bucket] if bucket else [],
            "source_labels": [str(label)],
            "rate_c_per_kwh": rounded_rate,
            "rate_nzd_per_kwh": (
                round(rate_cents / 100, 6) if rate_cents is not None else None
            ),
            "band_category": band_category or None,
            "unit_type": unit_type or None,
        }
        rate_bands[key] = band
        if bucket:
            bucket_to_key[bucket] = key

    return {
        "agreement_name": agreement.get("displayName"),
        "agreement_description": agreement.get("description"),
        "valid_from": agreement.get("validFrom"),
        "valid_to": agreement.get("validTo"),
        "rate_bands": rate_bands,
        "bucket_to_key": bucket_to_key,
        "standing_rate_nzd": standing_rate_nzd,
        "time_of_use_schemes": agreement.get("timeOfUseSchemes") or [],
    }


def _minutes(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parts = str(value).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return None


def _season_applies(local_dt: datetime, season: dict[str, Any] | None) -> bool:
    """Return whether a seasonal timeslot applies on this local date."""
    if not season:
        return True
    try:
        start = (int(season["startMonth"]), int(season["startDay"]))
        end = (int(season["endMonth"]), int(season["endDay"]))
    except (KeyError, TypeError, ValueError):
        return True

    current = (local_dt.month, local_dt.day)
    if start <= end:
        return start <= current <= end
    # Season wraps over New Year.
    return current >= start or current <= end


def _day_applies(local_dt: datetime, slot: dict[str, Any]) -> bool:
    """Apply Powershop weekday/weekend/Sat/Sun flags."""
    weekdays = bool(slot.get("weekdays", False))
    weekends = bool(slot.get("weekends", False))
    saturdays = bool(slot.get("saturdays", False))
    sundays = bool(slot.get("sundays", False))

    # Kraken uses all-false to mean every day for general slots.
    if not any((weekdays, weekends, saturdays, sundays)):
        return True

    weekday = local_dt.weekday()
    if weekday < 5:
        return weekdays
    if weekday == 5:
        return weekends or saturdays
    return weekends or sundays


def classify_by_schedule(local_dt: datetime, tou: dict[str, Any]) -> str | None:
    """Return the rate-band key active at *local_dt* according to the API schedule."""
    bucket_to_key = tou.get("bucket_to_key") or {}
    minute = local_dt.hour * 60 + local_dt.minute

    for scheme in tou.get("time_of_use_schemes") or []:
        for slot in scheme.get("timeslots") or []:
            bucket = str(slot.get("timeslot") or "").strip()
            key = bucket_to_key.get(bucket)
            if not key:
                continue
            if not _day_applies(local_dt, slot):
                continue
            if not _season_applies(local_dt, slot.get("season")):
                continue

            start = _minutes(slot.get("activeFrom"))
            end = _minutes(slot.get("activeTo"))
            if start is None or end is None:
                continue

            if start == end:
                in_range = True
            elif start < end:
                in_range = start <= minute < end
            else:
                in_range = minute >= start or minute < end

            if in_range:
                return key

    return None


def _stat_value_cost(stat: dict[str, Any]) -> tuple[float, float]:
    try:
        kwh = float(stat.get("value") or 0)
    except (TypeError, ValueError):
        kwh = 0.0
    try:
        cost_cents = float(
            ((stat.get("costInclTax") or {}).get("estimatedAmount")) or 0
        )
    except (TypeError, ValueError):
        cost_cents = 0.0
    return kwh, cost_cents


def classify_stat_band(stat: dict[str, Any], tou: dict[str, Any]) -> str | None:
    """Identify the API rate band for one measurement statistic entry.

    New Powershop labels are often opaque hashes, so the reliable discriminator
    is the effective cents/kWh compared with the active agreement's rates.
    """
    label = str(stat.get("label") or "").strip()
    stat_type = str(stat.get("type") or "").upper()
    upper_label = label.upper()

    if "STANDING_CHARGE" in upper_label or stat_type == "STANDING_CHARGE_COST":
        return None

    rate_bands: dict[str, dict[str, Any]] = tou.get("rate_bands") or {}
    if not rate_bands:
        return None

    # Legacy responses may use the TOU bucket name directly as the label.
    for bucket, key in (tou.get("bucket_to_key") or {}).items():
        if label.upper() == str(bucket).upper():
            return key

    kwh, cost_cents = _stat_value_cost(stat)
    if kwh <= 0 or cost_cents <= 0:
        return None

    candidates = list(rate_bands.items())

    # Opaque TOU labels should only be compared with TOU buckets; controlled/
    # non-TOU labels should prefer bands without a bucket.
    if upper_label.startswith("CONSUMPTION_CHARGE_TOU_"):
        tou_candidates = [(k, b) for k, b in candidates if b.get("bucket")]
        if tou_candidates:
            candidates = tou_candidates
    elif upper_label.startswith("CONSUMPTION_CHARGE_"):
        non_tou = [(k, b) for k, b in candidates if not b.get("bucket")]
        if non_tou:
            candidates = non_tou

    effective_rate = cost_cents / kwh
    best_key: str | None = None
    best_diff = float("inf")

    for key, band in candidates:
        rate = band.get("rate_c_per_kwh")
        if rate is None:
            continue
        diff = abs(effective_rate - float(rate))
        if diff < best_diff:
            best_diff = diff
            best_key = key

    if best_key is None:
        return None

    # Allow normal billing/rounding differences, but avoid silently mapping a
    # genuinely different rate to the wrong band.
    if effective_rate and best_diff / effective_rate <= 0.05:
        return best_key
    return None


def extract_interval_band_entries(
    node: dict[str, Any],
    tou: dict[str, Any],
    local_dt: datetime,
) -> list[tuple[str, float, float]]:
    """Return (band_key, kWh, NZD cost) entries for one interval node."""
    result: list[tuple[str, float, float]] = []
    stats = ((node.get("metaData") or {}).get("statistics") or [])

    # Prefer explicit per-band statistics. A generic CONSUMPTION_COST entry
    # may represent the same interval total and must not be added alongside
    # TOU bucket rows, otherwise the interval would be double-counted.
    explicit_stats: list[dict[str, Any]] = []
    bucket_names = {
        str(bucket).upper()
        for bucket in (tou.get("bucket_to_key") or {})
        if bucket
    }
    for stat in stats:
        label = str(stat.get("label") or "").strip()
        stat_type = str(stat.get("type") or "").upper()
        upper_label = label.upper()
        if (
            stat_type == "TOU_BUCKET_COST"
            or upper_label.startswith("CONSUMPTION_CHARGE_")
            or upper_label in bucket_names
        ):
            explicit_stats.append(stat)

    for stat in explicit_stats:
        key = classify_stat_band(stat, tou)
        if not key:
            continue
        kwh, cost_cents = _stat_value_cost(stat)
        result.append((key, max(0.0, kwh), max(0.0, cost_cents / 100)))

    if result:
        return result

    # Fallback for older responses without per-band metadata.
    key = classify_by_schedule(local_dt, tou)
    if not key:
        rate_bands = tou.get("rate_bands") or {}
        if len(rate_bands) == 1:
            key = next(iter(rate_bands))
    if not key:
        return []

    try:
        kwh = max(0.0, float(node.get("value") or 0))
    except (TypeError, ValueError):
        kwh = 0.0

    consumption_cost_cents = 0.0
    for stat in stats:
        if str(stat.get("type") or "").upper() != "CONSUMPTION_COST":
            continue
        _, cents = _stat_value_cost(stat)
        consumption_cost_cents += cents

    if consumption_cost_cents:
        cost_nzd = consumption_cost_cents / 100
    else:
        band = (tou.get("rate_bands") or {}).get(key) or {}
        rate_nzd = float(band.get("rate_nzd_per_kwh") or 0)
        cost_nzd = kwh * rate_nzd

    return [(key, kwh, max(0.0, cost_nzd))]
