# Brunata München for Home Assistant

Imports your monthly heating, hot water and cold water consumption from the
[Brunata München Nutzerportal](https://nutzerportal.brunata-muenchen.de) into Home
Assistant, ready for the Energy dashboard.

The portal only publishes one value per month. This integration spreads each month
evenly over its hours and writes the result as long-term statistics, so the Energy
dashboard shows a smooth daily usage instead of one spike per month.

Unofficial project, not affiliated with BRUNATA-METRONA.

## What you get

| Meter | Statistic | Energy dashboard |
|---|---|---|
| Cold water (`KW..`) | `brunata:<account>_kw01`, m³ | Water source |
| Hot water (`WW..`) | `brunata:<account>_ww01`, m³ | Water source |
| Heating (`HZ..`) | `brunata:<account>_hz01`, kWh | Not supported there, use a Statistics graph card |

Plus one sensor per meter with the most recent published month (attribute `month`).
Those sensors are informational only and not used for statistics.

The integration polls every 12 hours. New months usually appear in the portal a few
days after the month ends.

## Installation

### Marketplace / HACS

1. Add this repository as a custom repository (category *Integration*).
2. Install **Brunata München** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Brunata München** and log
   in with your portal email and password.

### Manual

Copy `custom_components/brunata` into your Home Assistant `custom_components` folder
and restart.

## Energy dashboard

Under **Settings → Dashboards → Energy → Water consumption**, add
*Brunata cold water KW01* and *Brunata hot water WW01*.

## Credentials and privacy

- Your portal credentials are entered in the Home Assistant UI and stored in Home
  Assistant's own config entry storage. They are never part of this repository.
- The integration only talks to `nutzerportal.brunata-muenchen.de`.
- If the portal rejects the stored password, Home Assistant asks you to re-enter it.

## How it works

- `brunata_api/` is a vendored copy of
  [fjfricke/brunata-api](https://github.com/fjfricke/brunata-api) at commit
  `ffd54b519b7dee51e2d8535cf2e4f750e16f5504` (MIT, see its `LICENSE`), without the CLI.
  Vendoring pins exactly the reviewed code and avoids installing from Git at runtime.
- `fetch.py` collects the monthly values of all periods (calendar years) per meter.
- `series.py` spreads each month over its local-time hours (DST aware) and never writes
  hours in the future.
- `coordinator.py` imports the hourly sums as external statistics. Each run rewrites the
  months the portal still has, continuing from the stored sum before the oldest one, so
  corrections in the portal are picked up and older history stays consistent.

## Development

```bash
python3 -m pytest tests
```

`tools/import_test.py` runs the same fetch and spreading code against a live Home
Assistant and writes `brunata_test:*` statistics, so you can check the result without
installing the integration. It reads all credentials from environment variables
(`BRUNATA_USERNAME`, `BRUNATA_PASSWORD`, `HA_URL`, `HA_TOKEN`) or from a file passed
with `--env-file`. Keep that file outside the repository. `--delete` removes the test
statistics again.
