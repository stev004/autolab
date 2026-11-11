# ADR 0004: Culture devices declare named controlled variables; pH is held via CO2

- Status: accepted
- Date: 2026-11-10
- Agreed by: Steven; the AutoLab session; the algae-sim session (device owner)
- Implemented: `e9f8421` (culture contract v2)

## Decision

A culture controller's manifest declares:

- `controlled_variables`: name → unit, range (optionally with resolution), and
  `tracked` (whether the device reports a measured value);
- `protection_limits`: trip points the device enforces locally;
- `resources`: cumulative consumption counters reported in telemetry.

Setpoint commands, acknowledgements and telemetry carry maps keyed by variable or
resource name instead of fixed temperature and light fields.

- A command names **exactly** the device's variables; unknown and missing ones are
  rejected. Campaign parameter names are variable names, and non-optimised variables
  are held with `culture_design.fixed_setpoints`.
- The device manifest is the source of truth for ranges. The installation policy
  (`setpoint_limits`, `minimum_protection_margins`) bounds what AutoLab allows. The
  effective range is policy ∩ device, checked per experiment and once, fail fast, when
  a campaign starts.
- QC checks tracking (`control:<var>`, `telemetry_coverage:<var>`) for every tracked
  variable, each after its own warm-up (`tracking_warmups`).
- pH is a **setpoint the device holds by dosing CO2**. CO2 used is a measured resource
  (`co2_g`), not a setpoint. Canonical names: `temperature` (degC), `light_intensity`
  (µmol/m²/s), `ph` (dimensionless).

## Rationale

- **Hard-coded fields didn't scale.** v1 hard-coded temperature and light across the
  protocol, command, acknowledgement, telemetry, policy and QC, so each new variable
  meant code changes everywhere. Declared variables make the next one a config change.
- **pH and CO2 aren't independent.** In a photobioreactor, CO2 lowers pH, photosynthetic
  carbon uptake raises it, and there is no base dosing. Treating them as two knobs would
  let an optimiser ask for impossible combinations.
- **Ranges are device facts.** algae-sim's device can heat but not cool (minimum 20 °C)
  and trips at 39 °C, not the 42 °C first assumed. Reading ranges from the manifest
  avoids encoding wrong assumptions in AutoLab.
- **pH needs its own warm-up.** pH is pulled up only by growth, so it settles later
  than temperature.

## Consequences

- Breaking change for adapters and culture campaign configs (v1 `temperature_setpoint`
  → `temperature`). algae-sim must migrate before using AutoLab ≥ `e9f8421`.
- Nitrate/medium composition, dissolved O2 and stirring are out of scope. Medium
  composition needs a separate inoculation/medium command type.
- Agent draft review checks culture ranges against `setpoint_limits` by variable name.
