# E.ON Next Energy Data for Home Assistant

A narrow, read-only Home Assistant custom integration for importing E.ON Next
smart-meter consumption into long-term statistics and the Energy dashboard.

> [!CAUTION]
> This project was created entirely through AI-assisted “vibe coding”. Its code
> and reviews were produced by AI agents under human direction. It has not had
> an independent professional security audit and has not been reviewed by E.ON
> or the Home Assistant project. It uses an undocumented private E.ON API that
> may change or stop working without notice. Use it at your own risk, verify all
> imported consumption and cost data against official E.ON records, and do not
> rely on it for billing, financial, safety, or regulatory decisions.

## What it does

- Imports half-hour electricity and gas readings, aggregated into Home
  Assistant's hourly external-statistics format.
- Creates cumulative consumption statistics for each supported active meter, and cost statistics for meters measured in kWh.
- Applies the configured VAT-inclusive tariff, including daily standing charges.
- Polls every six hours and rewrites a rolling 14-day correction window so
  delayed or revised readings are repaired.
- Exposes only redacted data-freshness sensors. Account numbers, MPANs, MPRNs,
  meter serials and API tokens are not logged or shown as entity attributes.

The default tariff values match E.ON Next Drive Smart V7.3:

- Electricity 00:00–07:00 Europe/London: £0.069/kWh
- Electricity 07:00–00:00: £0.3367/kWh
- Electricity standing charge: £0.60/day
- Gas: £0.0812/kWh
- Gas standing charge: £0.2916/day

The initial import defaults to 30 days. Tariff changes recalculate the latest
14 days and apply to future imports; older cost history is left unchanged.

## Security model

The config flow sends the supplied email and password directly to E.ON's Kraken
GraphQL endpoint. It stores only the returned refresh token and discards the
password. Subsequent updates use the refresh token. If E.ON invalidates it,
Home Assistant starts a reauthentication flow.

The client contains no account-balance, statement, payment, meter-reading
submission, tariff switching, EV control or other mutation code. The only
GraphQL mutation is the token exchange required for authentication.

Meter units are read from E.ON. Electricity must be `kWh`; gas can be `kWh`,
`m3`, or `m³`. Gas volume is imported as native cubic metres using a separate
volume statistic ID by default. To enable estimated gas energy and costs, set
**Gas calorific value** in integration options to the value on your bill (37–43).
The conversion is `m³ × 1.02264 × calorific value ÷ 3.6`. Converted consumption
and costs use separate statistic IDs labelled Estimated. Replace the dashboard's
gas source with the estimated consumption and estimated cost statistics; do not
add them alongside the native-volume source. Setting 0 restores volume-only mode.
Converted gas recalculates the configured history window on every update; older
records outside that window retain their earlier conversion and tariff.
Missing or unsupported units are skipped.
Export electricity meter points are ignored.

This uses an undocumented private E.ON API and may require maintenance if E.ON
changes it. It is not affiliated with or endorsed by E.ON.

The integration is an experimental personal project. It is supplied without
warranty or support commitments. Review the source and start with a limited
history window before allowing it to write long-term statistics.

## Installation with HACS

1. In HACS, open the menu and select **Custom repositories**.
2. Add `https://github.com/crofts32/home-assistant-eon-next-energy` with
   category **Integration**.
3. Install **E.ON Next Energy Data** and restart Home Assistant.
4. Open **Settings → Devices & services → Add integration**, then select
   **E.ON Next Energy Data**.
5. Enter the current E.ON Next email and password. The password is exchanged
   for a refresh token and is not stored.

After the first import, select the external consumption and cost statistics in
the Energy dashboard. The integration's diagnostic entities list the redacted
statistic IDs created for each fuel.

Start with the default 30-day import and compare daily electricity, gas, and
cost totals against the E.ON Next portal before relying on the statistics.

## Energy summary card

Add `/eon-next-energy/energy-summary.js?v=0.3.0` as a JavaScript module resource
in dashboard resources, then add a manual card:

```yaml
type: custom:eon-energy-summary
```

The card discovers redacted statistic IDs from the integration's freshness
entities. It shows a daily stacked chart with peak and off-peak electricity,
estimated gas, and EV-charge-day markers, plus selected-period consumption, an
estimated Hypervolt charging cost using the configured time-of-use rates, and
total cost and independent month-to-date metrics. Dates use
Europe/London including DST. Costs include the standing charges already written
by the importer. Gas conversion must be enabled for combined totals; missing
fuel data displays a dash instead of an invented zero. Delayed or incomplete
imports make totals partial. Hypervolt is already included in grid electricity
and is never added to the combined total. A live-home section shows Nest
temperature, target, humidity and heating state alongside Hypervolt connection,
charging readiness, power and current-session energy. Data is read through Home
Assistant's authenticated connection; no data is sent to any third party.
