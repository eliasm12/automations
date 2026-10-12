# SPC line, PV, and battery energy helpers for Home Assistant

The inverter reports output watts, grid voltage, PV watts, and mode. Grid voltage cannot be multiplied by output watts: watts already represents power, and grid current/power factor are not available. When mode is L, PV is zero, and grid voltage is present, output watts approximate the load served by the line. Inverter losses are excluded. Solar charging only means utility charging need not be added. Periods with PV generation are excluded because solar and line can both serve the output.

## Create the power sensor in the Home Assistant UI

Settings > Devices & services > Helpers > Create helper > Template > Template a sensor.

Name: `Line-fed load estimate`  
Unit of measurement: `W`  
Device class: `Power`  
State class: `Measurement`

State:

```jinja2
{% set mode = states("sensor.alpha_outback_spc_iii_5000_48_operating_mode_code") %}
{% set pv = states("sensor.alpha_outback_spc_iii_5000_48_pv_power") | float(0) %}
{% set grid_v = states("sensor.alpha_outback_spc_iii_5000_48_grid_voltage") | float(0) %}
{% set output_w = states("sensor.alpha_outback_spc_iii_5000_48_output_power") | float(0) %}
{{ output_w if mode == "L" and pv == 0 and grid_v > 0 else 0 }}
```

Availability:

```jinja2
{{ states("sensor.alpha_outback_spc_iii_5000_48_operating_mode_code") in ["L", "B"]
   and is_number(states("sensor.alpha_outback_spc_iii_5000_48_pv_power"))
   and is_number(states("sensor.alpha_outback_spc_iii_5000_48_grid_voltage"))
   and is_number(states("sensor.alpha_outback_spc_iii_5000_48_output_power")) }}
```

Check the four source entity IDs in Developer tools > States before saving. Home Assistant may have assigned different IDs to your MQTT sensors.

## Accumulate the estimate in kWh

Create another helper: Integral. Set input sensor to `Line-fed load estimate`, name to `Line-fed load energy estimate`, integral method to `Left`, metric prefix to `k`, integration time to `h`, and maximum sub-interval to 5 minutes. The resulting sensor is in kWh and accumulates over time, including across Home Assistant restarts. It starts from zero when created; it cannot reconstruct older energy from existing history.

A 500 W output sustained for 2 hours under the qualifying conditions adds about 1 kWh. A zero value while PV is producing means the line share is unknown, not necessarily zero. This is an estimated load energy, not a meter of grid imports: it omits inverter losses and any line use while PV contributes. Avoid treating it as an exact grid-consumption source in the Energy dashboard. A line-side energy meter is required for actual imports.

## Create a separate PV production meter

The inverter already publishes `sensor.alpha_outback_spc_iii_5000_48_pv_power` in W, so no PV template sensor is needed. Check its exact entity ID in Developer tools > States.

Go to Settings > Devices & services > Helpers > Create helper > Integral and set:

- Name: `SPC solar production energy`
- Input sensor: the inverter `PV power` sensor
- Integral method: `Left`
- Metric prefix: `k`
- Integration time: `h`
- Maximum sub-interval: `5 minutes`

This gives a separate cumulative PV-production sensor in kWh. In Settings > Dashboards > Energy, add that new sensor under **Solar panels > Solar production**. It represents all measured PV output, including energy used to charge the battery; do not also add the battery charge energy as another solar source.

The line-fed load estimate remains separate. Its cumulative kWh is incomplete when PV is producing and does not measure actual grid imports, so it should not be treated as an exact grid-consumption meter. Both Integral helpers start accumulating when created and retain their totals across Home Assistant restarts.

## Create battery charge and discharge power sensors

The inverter publishes battery voltage and separate charge/discharge currents. Estimated DC battery power is `voltage × current` in watts. For example, 52.9 V × 12 A is about 635 W of battery charging. Check the exact source entity IDs in Developer tools > States before creating these helpers.

For each, go to Settings > Devices & services > Helpers > Create helper > Template > Template a sensor. Set unit to `W`, device class to `Power`, and state class to `Measurement`.

Name: `SPC battery charge power estimate`

State:

```jinja2
{% set volts = states("sensor.alpha_outback_spc_iii_5000_48_battery_voltage") | float(0) %}
{% set amps = states("sensor.alpha_outback_spc_iii_5000_48_battery_charge_current") | float(0) %}
{{ (volts * amps) | round(1) }}
```

Availability:

```jinja2
{{ is_number(states("sensor.alpha_outback_spc_iii_5000_48_battery_voltage"))
   and is_number(states("sensor.alpha_outback_spc_iii_5000_48_battery_charge_current")) }}
```

Name: `SPC battery discharge power estimate`

State:

```jinja2
{% set volts = states("sensor.alpha_outback_spc_iii_5000_48_battery_voltage") | float(0) %}
{% set amps = states("sensor.alpha_outback_spc_iii_5000_48_battery_discharge_current") | float(0) %}
{{ (volts * amps) | round(1) }}
```

Availability:

```jinja2
{{ is_number(states("sensor.alpha_outback_spc_iii_5000_48_battery_voltage"))
   and is_number(states("sensor.alpha_outback_spc_iii_5000_48_battery_discharge_current")) }}
```

## Accumulate each battery flow in kWh

Create **two separate Integral helpers**, one for each new power sensor. Use the following settings for both: integral method `Left`, metric prefix `k`, integration time `h`, maximum sub-interval `5 minutes`. Name them `SPC battery charge energy estimate` and `SPC battery discharge energy estimate`.

In Settings > Dashboards > Energy, add them under **Battery systems**: charge energy to the energy-going-into-battery field and discharge energy to the energy-coming-out-of-battery field. Keep the PV production meter under Solar panels; battery charging from solar is a transfer of some of that PV energy into storage, not extra solar generation.

These battery meters estimate energy at the battery terminals. They do not include battery chemistry, charger, or inverter losses, and may differ from AC-side energy. They accumulate only from the moment the helpers are created. If a source sensor becomes unavailable, the matching power helper also becomes unavailable instead of recording a false zero.

These battery power/energy helpers are optional. The supplied `spc_dashboard.yaml` shows battery voltage, charge current, and discharge current without requiring these helpers.

With app 0.2.5, selecting Solar and utility charging may add grid energy used for battery charging. The line-fed load estimate still measures only the estimated load power during qualifying periods; it excludes battery charging and inverter losses and must not be treated as total grid imports.
