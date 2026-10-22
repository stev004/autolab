# Hardware abstraction and adapter seam

AutoLab routes commands by capabilities, not manufacturer. A device supplies:

```python
class Device(Protocol):
    @property
    def device_id(self) -> str: ...
    async def capability_manifest(self) -> CapabilityManifest: ...
    async def execute(
        self, command: ProtocolCommand, context: ExecutionContext
    ) -> DeviceResponse: ...
    async def enter_safe_state(self, reason: str) -> None: ...
```

A manifest declares supported command types, compatible vessel types, measurement or
actuation ranges, resolution, channels, adapter version, calibration identity/expiry,
and safety limits. A frozen `CapabilitySnapshot` is used for validation and execution.

## Adapter obligations

A physical adapter must:

- reject commands outside its own limits even when upstream validation passed;
- attach device, experiment, run and step identity to every response;
- distinguish timeout, retry-safe failure, non-retry-safe failure, and ambiguous
  physical state;
- make idempotency behavior explicit and never silently repeat a liquid movement;
- implement a best-effort safe state without preventing other devices from stopping;
- expose firmware/adapter version and current calibration metadata;
- return actual executed quantities when they can differ from requested quantities;
- contribute only the evidence fragment it can directly attest to: liquid handlers
  report material/resource deltas, incubators report time/temperature exposures, and
  sensors report acquisitions and instrument facts.

The orchestrator aggregates fragments in protocol order and passes them to an
`EvidenceFinalizer`. The finalizer rejects missing, conflicting, or incorrectly
provenanced facts and derives campaign costs above the device layer. A device must
not synthesize a whole-run `ExecutionEvidence` object from another adapter's private
state. No successful execution record is emitted until finalized evidence is valid.

Adapter-specific SDK objects and transport details do not cross this boundary. An
`OpenColorimeterAdapter`, `SerialPumpAdapter`, `PyLabRobotAdapter`, or
`OpentronsAdapter` should therefore require no changes to optimisation, QC, or
analysis.

## First read-only sensor

V0.5 also exposes the narrower `ReadOnlySensor` contract in
`autonomous_lab.hardware.sensors`. A future optical adapter implements connection
lifecycle, capability manifest, self-test, and one `read(MeasurementRequest)` call.
It returns identity-bound `RawSensorMeasurement` evidence including the sensor ID,
adapter/firmware versions, timestamp, wavelength where applicable, raw response
metadata, and calibration reference/hash. It must reject unsupported wavelengths,
invalid or expired calibration, malformed responses, disconnection, and timeout.

An `OpenColorimeterAdapter` belongs at this seam. It must not calculate QC, fit a
calibration curve, know campaign objectives, access optimiser state, control liquid
handling, or import simulation. `MockPhysicalSensor` is a fixture-backed contract
test - not an emulation of the hidden assay world and never proof that hardware ran.

Real sensor adapters are discovered through the
`autonomous_lab.sensor_adapters` Python entry-point group. An adapter distribution
exports a factory accepting `sensor_id` and adapter-owned `settings` and returning a
`ReadOnlySensor`; campaign configuration then uses `measurement_source:
physical_sensor`. This keeps vendor dependencies outside AutoLab and requires no
changes to campaign execution, QC, analysis, storage, or optimisation.

## Future actuation

`HumanExecutionBackend` currently provides truthful operator evidence. A later
`FixedFluidicsBackend`, and after that a Cartesian liquid-handler backend, can compile
the same validated protocol into device operations. The proposal, expected
measurement plan, ingestion, QC, analysis, optimiser, and provenance layers remain
unchanged. Physical actuation additionally requires inventory/deck state,
uncertain-action reconciliation, waste limits, independent interlocks, and safe-stop
validation; none is claimed by V0.5.

## Before unattended physical use

Add inventory and lot tracking, labware/deck state, startup self-tests, calibration
management, operator reconciliation after uncertain state, emergency-stop handling,
material compatibility checks, waste limits, and independent firmware or electrical
interlocks. The simulator's safe-stop behavior is a contract test, not evidence that
a physical device is safe.
