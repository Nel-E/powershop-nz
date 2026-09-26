<p align="center">
  <img src="logo.png" alt="Powershop NZ" height="120">
</p>

# Powershop New Zealand — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub Release](https://img.shields.io/github/release/Nel-E/powershop-nz.svg)](https://github.com/Nel-E/powershop-nz/releases)

A Home Assistant custom component for **Powershop New Zealand** customers. Monitor your account balance, electricity rates, usage, and Power Pack coverage — updated every 15 minutes.

## Features

- **Account Balance** — current balance in NZD
- **Time-of-Use Rates** — off-peak, peak, and shoulder rates in c/kWh
- **Usage Monitoring** — today's kWh and billing period kWh
- **Billing Period Sensors** — mirrors the Powershop app: used cost, estimated total, pack coverage %, and still-to-buy shortfall for the current period
- **Power Pack Tracking** — total redeemable pack balance and full pack list; upcoming 5 billing periods show estimated cost vs packs already purchased
- **Passwordless Auth** — uses Powershop's email OTP login (no password stored)
- **Automatic Token Refresh** — stays authenticated in the background
- **Regular Updates** — 15-minute refresh interval
- **Home Assistant Energy Dashboard** — imports Powershop kWh and total electricity cost as long-term statistics, including the daily standing charge
- **Time-of-Use Statistics** — creates separate consumption and cost statistics for the actual Powershop tariff bands returned by your agreement (for example Peak, Off-Peak, Night, or Controlled)
- **Automatic Historical Corrections** — 60-day initial/migration backfill and a rolling 30-day correction sync every 12 hours
- **DST-safe Interval Data** — handles New Zealand daylight-saving transitions without assuming every day has exactly 24 hours

## Requirements

- A Powershop NZ account at [app.powershop.nz](https://app.powershop.nz) - Your account must be migrated to Powershop's new platform. You can verify this by checking if you can log in at app.powershop.nz. Powershop is rolling this out gradually and you'll get an email from them letting you know your account is being migrated — if your account hasn't been migrated yet, the integration will not work. 
- Home Assistant 2024.1 or later
- HACS (for managed installation)

## Installation

### Via HACS (Recommended)

1. Open **HACS** in Home Assistant
2. Go to **Integrations**
3. Click the three dots (⋮) in the top right corner and select **Custom repositories**
4. Add `https://github.com/Nel-E/powershop-nz` and select **Integration**
5. Click **Add**, then find **Powershop NZ** in the list and install it
6. Restart Home Assistant

### Manual Installation

1. Download or clone this repository
2. Copy the `custom_components/powershop/` folder into your HA config's `custom_components/` directory
3. Restart Home Assistant

## Configuration

Authentication uses a one-time password (OTP) sent to your email — no password required:

1. Go to **Settings**, then **Devices & Services**, then **Add Integration**
2. Search for **Powershop**
3. Enter your Powershop account email address and click **Submit**
4. Check your email for the one-time code and enter it, then click **Submit**

Your account number and property ID are discovered automatically. Home Assistant will prompt you to re-authenticate if your session ever expires — just repeat the OTP process.

## Sensors

| Entity | Description | Unit |
|--------|-------------|------|
| `sensor.powershop_nz_balance` | Current account balance | NZD |
| `sensor.powershop_nz_off_peak_rate` | Off-peak electricity rate | c/kWh |
| `sensor.powershop_nz_peak_rate` | Peak electricity rate | c/kWh |
| `sensor.powershop_nz_shoulder_rate` | Shoulder electricity rate | c/kWh |
| `sensor.powershop_nz_usage_today` | kWh consumed today (last 24 h) | kWh |
| `sensor.powershop_nz_usage_billing_period` | kWh consumed this billing period | kWh |
| `sensor.powershop_nz_cost_billing_period` | Cost for the current billing period | NZD |
| `sensor.powershop_nz_period_used_cost` | Actual metered spend so far this billing period | NZD |
| `sensor.powershop_nz_period_estimated_cost` | Projected total cost for this billing period | NZD |
| `sensor.powershop_nz_period_still_to_buy` | How much more in packs you'd need to cover this billing period | NZD |
| `sensor.powershop_nz_period_coverage_pct` | % of projected bill covered by packs already purchased | % |
| `sensor.powershop_nz_voucher_balance` | Total redeemable Power Pack balance | NZD |
| `sensor.powershop_nz_daily_charge` | Daily fixed (standing/line) charge | NZD |

## Home Assistant Energy Dashboard

Version 2.3.0 keeps the existing Energy Dashboard statistic IDs from v2.2, so an existing dashboard does not need to be reconfigured.

After installing/updating and restarting Home Assistant, the integration automatically imports Powershop interval data. A new v2.3 installation or migration backfills the latest **60 days**; subsequent automatic runs re-read the latest **30 days** every 12 hours so estimated readings can be replaced by later actual meter data.

The primary statistics are:

- `powershop_nz:<account>_<property>_energy_consumption` — total imported electricity in kWh
- `powershop_nz:<account>_<property>_energy_cost` — total Powershop electricity cost in NZD, including consumption plus the daily standing charge
- `powershop_nz:<account>_<property>_standing_charge_cost` — the standing-charge component on its own

The account/property portions are normalised to valid Home Assistant statistic IDs.

### Add Powershop to the Energy Dashboard

1. Go to **Settings → Dashboards → Energy**
2. Under **Electricity grid**, add or edit **Grid consumption**
3. For imported energy, select the statistic whose name begins **Powershop NZ electricity consumption**
4. For cost, choose **Use an entity/statistic tracking total costs**
5. Select **Powershop NZ electricity cost**

The total cost uses Powershop's own interval consumption cost plus the historical standing charge returned by Powershop. For the current day, the active agreement's full daily standing charge is used because Powershop can return a prorated value while the day is still in progress.

### Time-of-use statistics

Powershop exposes the active agreement's TOU bucket names, rates and schedule. The integration uses that data — and, where available, each measurement's own per-bucket kWh/cost metadata — instead of hard-coding tariff hours.

For every tariff band detected on your account, v2.3 creates a pair of external statistics, for example:

- `..._energy_consumption_peak` and `..._energy_cost_peak`
- `..._energy_consumption_off_peak` and `..._energy_cost_off_peak`
- `..._energy_consumption_night` and `..._energy_cost_night`
- `..._energy_consumption_controlled` and `..._energy_cost_controlled`

The exact set depends on your Powershop plan. These are intended for tariff analysis/custom statistic graphs. Keep the existing total Powershop consumption statistic as the grid source in the Energy Dashboard unless you deliberately want to model the tariff bands as separate grid sources.

The importer prefers 30-minute Powershop measurements for TOU allocation, so boundaries such as 09:30 can be represented correctly. It falls back to hourly measurements if 30-minute data is unavailable.

### Manual history backfill

A service action is available at **Developer Tools → Actions**:

`powershop_nz.backfill_energy_statistics`

Fields:

- `days` — number of local calendar days to import, from 1 to 365 (default 60)
- `config_entry_id` — optional unless more than one Powershop NZ account is configured

The action returns the number of imported hourly rows, Recorder verification, the standing-charge statistic ID, and the tariff periods/statistic IDs detected from your Powershop agreement.

### Updating this fork with HACS

Add this fork as the HACS custom repository:

`https://github.com/Nel-E/powershop-nz`

HACS installs from the repository's default branch/release. After HACS downloads the update, restart Home Assistant.

### Sensor Attributes

**`sensor.powershop_nz_period_estimated_cost`** includes an `upcoming_periods` attribute — a list of the next 5 billing periods, each containing:

```yaml
- period_start: "2026-05-06"
  period_end: "2026-06-05"
  cost_estimated_nzd: 390.78     # projected cost based on your usage pattern
  voucher_bought_nzd: 0.00       # packs pre-purchased for that specific month
  cost_still_to_buy_nzd: 390.78  # shortfall
  coverage_pct: 0.0
```

**`sensor.powershop_nz_voucher_balance`** includes a `vouchers` attribute listing every active pack with its name, available-from date, remaining balance, and original value.

## Troubleshooting

**No OTP email?** Check spam, make sure you're using the right address, and try again, i found at some times of day the emails were slow to come through.
**"Email address not found" during setup** Even if your email is correct, this can happen if your account hasn't yet been migrated to Powershop's new platform. Powershop is doing a staged rollout — check if you can log in at app.powershop.nz first. If you can't, your account isn't on the new system yet and you'll need to wait or contact Powershop.

## 📝 Changelog

### v2.3.1 (2026-09-26)
- Run long Powershop statistics imports as Home Assistant background tasks so the 60-day migration no longer blocks startup
- Merge equivalent weekday/weekend tariff buckets when they represent the same semantic tariff at the same price (for example Weekday Off Peak + All Weekend Off Peak → one Off Peak statistic)
- Preserve all underlying Powershop bucket mappings while presenting cleaner Peak/Off Peak statistics

### v2.3.0 (2026-09-26)
- Total Energy Dashboard cost now includes Powershop's daily standing charge
- Added a separate long-term standing-charge cost statistic
- Added dynamic per-tariff-band consumption and cost statistics from the active Powershop agreement
- Added TOU agreement metadata query: bucket names, band categories, raw rates and time-of-use schedule
- TOU allocation prefers Powershop's per-measurement bucket metadata and falls back to the API schedule when needed
- Uses 30-minute measurements for tariff allocation when available, with hourly fallback
- Existing v2.2 total consumption/cost statistic IDs are unchanged, so existing Energy Dashboard configuration continues working
- First v2.3 sync automatically reprocesses 60 days so existing cost history gains the standing charge

### v2.2.1 (2026-09-26)
- Wait for Home Assistant Recorder to commit imported external statistics before the backfill action returns
- Read the consumption and cost statistics back from Recorder and return `recorder_verified`, `latest_consumption_statistic`, and `latest_cost_statistic` in the action response for direct diagnostics

### v2.2.0 (2026-09-26)
- Added Home Assistant Energy Dashboard long-term statistics for hourly Powershop electricity consumption and interval consumption cost
- Added automatic 60-day initial backfill and 30-day rolling correction sync every 12 hours
- Added `powershop_nz.backfill_energy_statistics` for manual 1–365 day imports
- Hourly measurement retrieval now uses explicit local-date ranges so NZ daylight-saving days with 23 or 25 intervals are handled correctly
- Powershop estimated hourly readings can be overwritten by later actual meter readings at their original timestamps
- Added Home Assistant `recorder` dependency for external statistics support

### v2.1.4 (2026-06-23)
- Fixed `sensor.powershop_nz_daily_charge` logging a HA validation warning on startup — the sensor was incorrectly using `state_class=MEASUREMENT` with `device_class=MONETARY`, which HA does not allow. The daily standing charge is a fixed tariff rate, not an accumulating total, so it now uses `state_class=None` with no device class — matching the pattern of the other rate sensors

### v2.1.3 (2026-06-14)
- Fixed `sensor.powershop_nz_peak_rate` returning the Off Peak rate value instead of the true Peak rate — rate label matching was doing a substring check so `"peak"` matched `"Off Peak"` first
- Added `powershop_nz.get_hourly_usage` service action — fetch 24 hourly usage entries for any selected date, useful for historical data without polluting sensor attributes. Thanks @gromitn!
- Added missing `config_entry_id` field to `services.yaml` so it appears in the HA UI for users with multiple config entries

### v2.1.2 (2026-06-07)
- Fixed `sensor.powershop_nz_usage_today` returning tomorrow's 24 estimated hourly entries instead of today's — the Powershop API treats `endOn` as inclusive, so the query now uses the Auckland-local current date rather than tomorrow
- `hourly_usage` entries now include `reading_quality` (e.g. `ESTIMATE`), matching the field already available on `daily_usage`
- Thanks @gromitn for the bug report and fix!

### v2.1.1 (2026-05-22)

> ⚠️ **Breaking Change — Manual Reinstall Required**
>
> The integration domain has been renamed from `powershop` to `powershop_nz`. **The HACS automatic update will fail** with the following error — this is expected:
>
> ```
> Downloading PMKA/powershop-nz with version v2.1.0 failed with:
> No manifest.json file found 'custom_components/powershop/manifest.json'
> ```
>
> You need to manually reinstall instead:
>
> 1. Go to **Settings**, then **Devices & Services**, and delete the existing Powershop integration
> 2. In **HACS**, remove the Powershop NZ integration
> 3. **Restart Home Assistant** (this is required — it clears the HACS domain cache)
> 4. In **HACS**, add `https://github.com/PMKA/powershop-nz` back as a custom repository
> 5. Install **Powershop NZ** from HACS (it will now install to the correct folder)
> 6. **Restart Home Assistant**
> 7. Go to **Settings**, then **Devices & Services**, then **Add Integration** and set up Powershop NZ
>
> Your sensors will be created fresh with the correct entity IDs (`sensor.powershop_nz_{key}`, e.g. `sensor.powershop_nz_balance`).
> Update any automations, dashboards, or scripts that reference the old IDs.
>
> Sorry for the hassle — the install base is still small so this felt like the right time to get the naming sorted properly rather than leaving it as `powershop` forever. It makes the integration easier to maintain and opens up submitting the icon to the official HA brands repo, so it's worth it in the long run. This is a one-time thing — future updates will install normally through HACS.

- Renamed integration domain from `powershop` to `powershop_nz` to prevent future conflicts with other Powershop country integrations — this will also allow the icon to be submitted to the HA brands repo 🥳
- Fixed entity ID generation: sensors now reliably produce `sensor.powershop_nz_{key}` (e.g. `sensor.powershop_nz_balance`)

### v2.0.9 (2026-05-21)
- Added `hourly_usage` attribute to `sensor.powershop_usage_today` — hourly kWh and cost for today
- Added `daily_usage` attribute to `sensor.powershop_usage_billing_period` — daily kWh, cost, and reading quality for the current billing period

### v2.0.8 (2026-05-01)
- Added `Daily Standing Charge` sensor — exposes the daily fixed/line charge in c/day

### v2.0.5 (2026-04-10)
- Updated integration icon — new transparent PNG, shown in HA integrations page and HACS store
- Documented Firebase API key as public project identifier (not a secret)
- Added GitHub secret scanning allowlist to suppress false-positive alerts
- Removed developer/debug scripts from the repository
- Repository URLs updated following rename to `powershop-nz`

### v2.0.4 (2026-04-09)
- Fixed upcoming billing period pack coverage showing $0 for periods without dedicated future packs
- The redeemable pack pool now cascades across future periods, matching how the Powershop app calculates coverage

### v2.0.3 (2026-04-09)
- Fixed all sensors showing unavailable after updating to v2.0.2
- Fixed Power Pack sensors failing to load on some accounts
- Fixed integration failing to initialise correctly on HA startup
- Various HACS compliance fixes

### v2.0.2 (2026-04-09)
- Added `Used This Billing Period` sensor — actual confirmed spend (USED in app)
- Added `Estimated Cost This Billing Period` sensor — full projected monthly cost (EST in app)
- Added `Still To Buy This Billing Period` sensor — pack shortfall warning
- Added `Billing Period Pack Coverage` sensor — % of estimated bill covered by packs
- `period_estimated_cost` sensor exposes `upcoming_periods` attribute with 5-month forward forecast (estimated cost + pre-purchased packs per period)
- Updated Power Pack query to use `availableBeforeDate`/`availableFromDate` filters matching the website's per-period breakdown
- Daily measurements now use a dedicated date-range query that includes `readingQuality` (ACTUAL vs ESTIMATED)

### v2.0.1
- Added usage sensors (today kWh, billing period kWh, billing period cost)
- Added Power Pack / voucher balance sensor

### v2.0.0 (2026-04-08)
- Full rewrite for the new Powershop app (`app.powershop.nz`)
- Replaced HTML scraping with Firebase OTP authentication + GraphQL API
- Added account balance sensor
- Passwordless login — no password ever stored
- Automatic session refresh with long-lived tokens
- Re-authentication support via HA config flow

### v1.0.0 (2025-11-10)
- Initial release
- Rate monitoring via the legacy `secure.powershop.co.nz` site

##  License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

##  Disclaimer

This integration is **not officially affiliated** with Powershop. Use at your own risk, I just wanted to have my rate data available in HA, reached out to Powershop to ask for API access but was nothing available so built my own.
