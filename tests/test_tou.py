"""Pure unit tests for Powershop TOU helpers."""
from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "powershop_nz"
    / "tou.py"
)
SPEC = importlib.util.spec_from_file_location("powershop_tou_test_module", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
tou = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tou)

NZ_TZ = ZoneInfo("Pacific/Auckland")


def sample_agreement() -> dict:
    return {
        "property": {
            "meterPoints": [
                {
                    "activeAgreement": {
                        "displayName": "Example TOU plan",
                        "rates": [
                            {
                                "displayLabel": "Night",
                                "touBucketName": "N9",
                                "bandCategory": "CONSUMPTION_CHARGE",
                                "unitType": "Kilowatt-hours consumed",
                                "rateIncludingTax": "23.62",
                            },
                            {
                                "displayLabel": "Peak",
                                "touBucketName": "PK5",
                                "bandCategory": "CONSUMPTION_CHARGE",
                                "unitType": "Kilowatt-hours consumed",
                                "rateIncludingTax": "40.77",
                            },
                            {
                                "displayLabel": "Weekday Off Peak",
                                "touBucketName": "WDDOPK16",
                                "bandCategory": "CONSUMPTION_CHARGE",
                                "unitType": "Kilowatt-hours consumed",
                                "rateIncludingTax": "27.92",
                            },
                            {
                                "displayLabel": "All Weekend Off Peak",
                                "touBucketName": "WE24",
                                "bandCategory": "CONSUMPTION_CHARGE",
                                "unitType": "Kilowatt-hours consumed",
                                "rateIncludingTax": "27.92",
                            },
                            {
                                "displayLabel": "Daily",
                                "touBucketName": "",
                                "bandCategory": "STANDING_CHARGE",
                                "unitType": "Days on supply",
                                "rateIncludingTax": "414.0",
                            },
                        ],
                        "timeOfUseSchemes": [
                            {
                                "name": "ST06",
                                "timeslots": [
                                    {
                                        "timeslot": "N9",
                                        "activeFrom": "00:00:00",
                                        "activeTo": "07:00:00",
                                        "weekdays": False,
                                        "weekends": False,
                                    },
                                    {
                                        "timeslot": "N9",
                                        "activeFrom": "22:00:00",
                                        "activeTo": "00:00:00",
                                        "weekdays": False,
                                        "weekends": False,
                                    },
                                    {
                                        "timeslot": "PK5",
                                        "activeFrom": "07:00:00",
                                        "activeTo": "09:30:00",
                                        "weekdays": True,
                                        "weekends": False,
                                    },
                                    {
                                        "timeslot": "WDDOPK16",
                                        "activeFrom": "09:30:00",
                                        "activeTo": "17:30:00",
                                        "weekdays": True,
                                        "weekends": False,
                                    },
                                    {
                                        "timeslot": "PK5",
                                        "activeFrom": "17:30:00",
                                        "activeTo": "20:00:00",
                                        "weekdays": True,
                                        "weekends": False,
                                    },
                                    {
                                        "timeslot": "WDDOPK16",
                                        "activeFrom": "20:00:00",
                                        "activeTo": "22:00:00",
                                        "weekdays": True,
                                        "weekends": False,
                                    },
                                    {
                                        "timeslot": "WE24",
                                        "activeFrom": "00:00:00",
                                        "activeTo": "00:00:00",
                                        "weekdays": False,
                                        "weekends": True,
                                    },
                                ],
                            }
                        ],
                    }
                }
            ]
        }
    }


class TouHelpersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tou = tou.extract_agreement_tou(sample_agreement())

    def test_agreement_extracts_canonical_bands_and_standing_charge(self) -> None:
        self.assertEqual(
            set(self.tou["rate_bands"]),
            {"night", "peak", "off_peak"},
        )
        self.assertAlmostEqual(self.tou["standing_rate_nzd"], 4.14)
        self.assertAlmostEqual(
            self.tou["rate_bands"]["peak"]["rate_nzd_per_kwh"],
            0.4077,
        )
        self.assertEqual(
            self.tou["bucket_to_key"]["WDDOPK16"],
            "off_peak",
        )
        self.assertEqual(
            self.tou["bucket_to_key"]["WE24"],
            "off_peak",
        )
        self.assertEqual(
            set(self.tou["rate_bands"]["off_peak"]["buckets"]),
            {"WDDOPK16", "WE24"},
        )

    def test_schedule_classifies_weekday_and_weekend(self) -> None:
        monday_peak = datetime(2026, 9, 21, 8, 0, tzinfo=NZ_TZ)
        monday_offpeak = datetime(2026, 9, 21, 10, 0, tzinfo=NZ_TZ)
        saturday_day = datetime(2026, 9, 26, 8, 0, tzinfo=NZ_TZ)
        saturday_night = datetime(2026, 9, 26, 23, 0, tzinfo=NZ_TZ)

        self.assertEqual(tou.classify_by_schedule(monday_peak, self.tou), "peak")
        self.assertEqual(
            tou.classify_by_schedule(monday_offpeak, self.tou),
            "off_peak",
        )
        self.assertEqual(
            tou.classify_by_schedule(saturday_day, self.tou),
            "off_peak",
        )
        self.assertEqual(
            tou.classify_by_schedule(saturday_night, self.tou),
            "night",
        )

    def test_explicit_band_stat_is_not_doubled_by_generic_total(self) -> None:
        node = {
            "value": "0.5",
            "metaData": {
                "statistics": [
                    {
                        "label": "CONSUMPTION_CHARGE_TOU_hash",
                        "type": "TOU_BUCKET_COST",
                        "value": "0.5",
                        "costInclTax": {"estimatedAmount": "20.385"},
                    },
                    {
                        "label": "",
                        "type": "CONSUMPTION_COST",
                        "value": "0.5",
                        "costInclTax": {"estimatedAmount": "20.385"},
                    },
                ]
            },
        }
        local_dt = datetime(2026, 9, 21, 8, 0, tzinfo=NZ_TZ)
        entries = tou.extract_interval_band_entries(node, self.tou, local_dt)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0], "peak")
        self.assertAlmostEqual(entries[0][1], 0.5)
        self.assertAlmostEqual(entries[0][2], 0.20385)

    def test_generic_cost_falls_back_to_schedule(self) -> None:
        node = {
            "value": "1.0",
            "metaData": {
                "statistics": [
                    {
                        "type": "CONSUMPTION_COST",
                        "value": "1.0",
                        "costInclTax": {"estimatedAmount": "27.92"},
                    }
                ]
            },
        }
        local_dt = datetime(2026, 9, 21, 10, 0, tzinfo=NZ_TZ)
        entries = tou.extract_interval_band_entries(node, self.tou, local_dt)
        self.assertEqual(entries, [("off_peak", 1.0, 0.2792)])


if __name__ == "__main__":
    unittest.main()
