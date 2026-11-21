# SEM adapter contract

**Status:** Baseline contract derived from the published Huawei resolver and the current SEM YAML migration branch.

## 1. Purpose

Every energy-system adapter must present the same SEM-level data and control interface. The YAML package and dashboard must use only the public SEM entities and SEM services. They must not refer to a manufacturer's integration domain, entity IDs, or service names.

Huawei is the reference implementation. New adapters must reproduce the semantics below, even when their source entities, units, device structure, and control APIs differ.

This document describes the existing interface. It does not authorize changes to the YAML package or to existing entity IDs.

## 2. Stable public entities

Keep the existing SEM entity unique-ID namespace, `sem_huawei_<entity_key>`, and the current public entity IDs. Changing these can create replacement Home Assistant entities and break YAML references.

| SEM key | Public entity ID | Contract |
|---|---|---|
| `battery_state_of_capacity` | `sensor.sem_energy_system_sem_battery_soc` | Battery-bank SOC, percent (0–100). For multiple batteries, calculate a rated-capacity-weighted average. Do not use a simple arithmetic average. |
| `battery_rated_capacity` | `sensor.sem_energy_system_sem_battery_rated_capacity` | Total rated battery capacity in Wh. Sum the individual battery capacities. This is also the weighting basis for the SOC calculation. |
| `battery_charge_discharge_power` | `sensor.sem_energy_system_sem_battery_power` | Total battery power in W. Positive means charging; negative means discharging. Sum the physical battery readings. |
| `battery_day_charge` | `sensor.sem_energy_system_sem_battery_day_charge` | Daily charged energy in kWh. Sum the physical battery readings. |
| `battery_day_discharge` | `sensor.sem_energy_system_sem_battery_day_discharge` | Daily discharged energy in kWh. Sum the physical battery readings. |
| `inverter_input_power` | `sensor.sem_energy_system_sem_inverter_input_power` | Total inverter input power in W. Sum the relevant inverter readings. |
| `power_meter_active_power` | `sensor.sem_energy_system_sem_power_meter_active_power` | Plant grid power in W. Positive means export; negative means import. This is a plant-level measurement, not an additive per-inverter value. |
| `battery_working_mode` | `select.sem_energy_system_sem_battery_working_mode` | Common SEM working mode, using the normalized options below. |
| `grid_charge_maximum_power` | `number.sem_energy_system_sem_grid_charge_maximum_power` | Maximum grid charging power in W. |
| `maximum_discharging_power` | `number.sem_energy_system_sem_maximum_discharging_power` | Maximum battery discharge power in W. |
| `end_of_discharge_soc` | `number.sem_energy_system_sem_end_of_discharge_soc` | Minimum battery SOC before discharge is stopped, in percent. |

The adapter must expose the same measurement meaning and units for all these entities. It must convert a provider's source values where required; simply copying a source entity is not sufficient when its unit or sign convention differs.

## 3. Aggregation rules

### Battery measurements

- SOC is weighted by each physical battery's rated capacity in Wh.
- SOC and rated capacity must map one-to-one to the same physical batteries. If a battery or its capacity is missing or invalid, do not publish a partial weighted SOC.
- Rated capacity, battery power, daily charge, and daily discharge are summed across the relevant physical batteries.
- Do not publish partial sums when one expected source is unavailable or invalid.

### Inverter input power

Sum the relevant inverter input-power readings. The aggregate must include every inverter that contributes to the plant's solar input, not just the inverter that owns the master battery.

### Grid meter

The grid meter represents the whole plant. Never add duplicate plant-meter readings together. When several meter entities are exposed for the same plant, validate that they agree and use one representative value (the current Huawei implementation averages readings only when they are within 100 W absolute or 2% relative tolerance). If readings conflict, return no value and report a diagnostic rather than publishing a potentially double-counted result.

## 4. Battery working-mode options

The SEM select exposes normalized values. Huawei's source values are translated as follows:

| Huawei source option | SEM option |
|---|---|
| `maximise_self_consumption` | `self_consumption` |
| `time_of_use_luna2000` | `time_of_use` |
| `fully_fed_to_grid` | `feed_in_priority` |
| `fixed_charge_discharge` | `fixed_charge_discharge` |
| `adaptive` | `adaptive` |

Only options supported by every physical battery should be exposed. The current selection is available only when all source batteries resolve to the same normalized mode. A selection must be validated for every battery before it is sent to any battery.

## 5. Number controls

A number entity is usable only when all expected physical source entities are available and their current values agree.

The public minimum and maximum must represent the intersection of the supported ranges across all physical devices. A proposed value must satisfy every device's range and step size before writing. A valid change is mirrored to every relevant physical source.

This prevents SEM from displaying one shared setting when the physical batteries are actually configured differently.

## 6. SEM services

The following service names and payloads are already used by the YAML package and must remain stable while the existing YAML is retained.

| SEM service | Payload | Current Huawei dispatch |
|---|---|---|
| `sem.set_battery_time_of_use_periods` | Required string `periods`. Current YAML sends `00:00-23:59/1234567/+`. | `huawei_solar.set_tou_periods` for each relevant battery. |
| `sem.set_grid_export_power_limit` | Required numeric/string `power`, expressed in W. Current YAML sends `300`. | `huawei_solar.set_maximum_feed_grid_power` for each relevant inverter. |
| `sem.reset_grid_export_power_limit` | No payload. | `huawei_solar.reset_maximum_feed_grid_power` for each relevant inverter. |

New adapters must translate these existing SEM commands into their provider-specific operations. Provider service names and device IDs must not escape into the YAML package.

The `periods` string is the format currently emitted by YAML and accepted by the Huawei implementation. Treat it as a compatibility constraint for this stage; any future change to a provider-neutral schedule structure requires a coordinated YAML migration.

## 7. Multi-device model

The current meaning of multi-support is multiple physical inverters and/or batteries forming **one energy plant**. The adapter is responsible for determining which entities belong together, mapping child devices to their owning inverter, avoiding duplicate module-level measurements, and applying controls to the correct physical devices.

The current manager activates an adapter only when exactly one energy system is configured. This is not permission to combine unrelated plants or different manufacturers into one aggregate. Multi-vendor support should first mean that one supported system can be represented through the same SEM interface.

## 8. Availability, diagnostics, and capabilities

An unavailable or invalid source must not silently contribute zero or produce a partial aggregate.

Each adapter declares a `SystemCapabilities` object:

- Every logical entity has an `EntityCapability`.
- `required` determines whether a missing or unresolved entity makes the system status fail.
- `readable` declares whether the entity provides data to SEM.
- `writable` declares whether SEM may send writes through that entity.
- `commands` lists the generic SEM services implemented by the adapter.

Only missing required entities affect the overall system status. Missing optional entities are listed separately as `optional_missing_entities` in the entity-status data. Entity-level capability declarations and supported commands are also exposed in the status sensor attributes.

The Huawei adapter deliberately marks all 11 current logical entities as required. This preserves the published Huawei resolver's existing setup requirements. It marks the working-mode select and three number controls as writable, and declares the three SEM commands used by the current YAML. Future adapters can classify measurements that are not essential to their own operation as optional and can independently declare read, write, and command support.

The manager checks these declarations before returning sensor values, exposing number/select controls for use, accepting writes, or executing a SEM command. A service that is not declared by the active adapter is rejected.

Provider-neutral capability reporting is now in place. Some detailed setup/runtime error text in the status sensor is still Huawei-specific and should be moved behind the adapter diagnostic interface before another provider is integrated.

## 9. Implementation notes for the shared layer

The shared adapter boundary is `SemSystemAdapter`, coordinated by `SemSystemManager`, with `SemEntityRegistry` tracking physical sources and the sensor/select/number proxy entities exposing the stable SEM interface.

Provider-independent aggregation and validation helpers now live in `systems/aggregation.py`; the shared number entity no longer imports a Huawei-named helper module. The capability definitions live in `systems/capabilities.py`.

The Huawei adapter remains the regression reference. The next implementation step is to make setup/runtime diagnostics provider-neutral, then use the common contract to introduce and test another adapter without changing the YAML-facing entities or services.
