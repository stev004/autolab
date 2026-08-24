# Protocol compilation and validation

The optimiser selects scientific parameter values. The registered assay compiler is
the only V0 component that translates them into operations. It emits typed
`DispenseCommand`, `MixCommand`, `IncubateCommand`, and
`MeasureAbsorbanceCommand` values with stable step IDs, required capability, timeout,
retry safety, preconditions, and expected postconditions.

## Deterministic validation

Validation checks:

1. proposal identity, exact source hash, parameter snapshot, bounds, units and
   duplicate-condition fingerprints;
2. trusted compiler and exact recipe binding, including concentration, volumes,
   incubation and wavelength;
3. permitted materials, vessels and command types;
4. controls, calibration levels, replicates and campaign layout;
5. incremental vessel volumes, transfer underflow, mix volumes and capacity;
6. legal per-vessel order from dispense through measurement;
7. measurement-plan coverage and agreement with the measurement command;
8. device type, command, vessel, range, resolution and calibration capability.

Every rejection is a `ValidationIssue` with a stable code, severity, schema path,
rule name/version, message and structured details. Warnings do not authorise unsafe
operations; any error makes the report rejected.

## Receipt integrity

The validation service canonicalizes the protocol and calculates:

```text
protocol_hash   = SHA256(canonical protocol excluding its circular hash field)
policy_hash     = SHA256(canonical trusted validation policy)
capability_hash = SHA256(canonical CapabilitySnapshot)
```

A passing `ExecutableProtocol` binds all three hashes and the report ID. The
orchestrator verifies the trusted report and recomputes the hashes immediately before
dispatch. Protocol edits, policy changes, device-range changes, calibration changes,
or an untrusted report invalidate the receipt.

The Python type is not the security boundary: constructing a look-alike object does
not make its report trusted. Physical deployments should add signed policy and
capability artifacts plus independent interlocks.
