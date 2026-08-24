from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from autonomous_lab.campaigns.loader import (
    LoadedCampaign,
    load_campaign,
    load_validation_policy,
)
from autonomous_lab.campaigns.service import CampaignExecutionError, CampaignService
from autonomous_lab.domain.enums import CampaignState, ExperimentState
from autonomous_lab.domain.parameters import ParameterSpace
from autonomous_lab.domain.protocols import CapabilityManifest, ProtocolCommand
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.commands import DeviceResponse, ExecutionContext
from autonomous_lab.hardware.interfaces import Device
from autonomous_lab.hardware.registry import DeviceRegistry
from autonomous_lab.optimization import OptunaStrategy, RandomSearchStrategy
from autonomous_lab.simulation.assay_model import FailureInjection
from autonomous_lab.storage import Database, Storage, UnitOfWork
from autonomous_lab.storage.orm import (
    ActionEventRecord,
    ExecutionRunRecord,
    ExperimentRecord,
    MeasurementRecord,
    OptimizerObservationRecord,
    OptimizerRunRecord,
    ProposalRecord,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN_PATH = PROJECT_ROOT / "configs" / "assay-reagent-reduction.yaml"
POLICY_PATH = PROJECT_ROOT / "configs" / "validation-policy.yaml"


class _SpectrophotometerWrapper:
    """Alternate sensor adapter with no access to simulated run state."""

    def __init__(self, delegate: Device, *, fault: str | None = None) -> None:
        self._delegate = delegate
        self._fault = fault

    @property
    def device_id(self) -> str:
        return self._delegate.device_id

    async def capability_manifest(self) -> CapabilityManifest:
        return await self._delegate.capability_manifest()

    async def execute(
        self,
        command: ProtocolCommand,
        context: ExecutionContext,
    ) -> DeviceResponse:
        response = await self._delegate.execute(command, context)
        if self._fault == "missing":
            return response.model_copy(update={"evidence_fragments": ()})
        if self._fault == "malformed":
            fragment = response.evidence_fragments[0]
            first = fragment.acquisitions[0]
            metadata = dict(first.metadata)
            metadata["wavelength_nanometer"] = 521
            acquisitions = (
                first.model_copy(update={"metadata": metadata}),
                *fragment.acquisitions[1:],
            )
            return response.model_copy(
                update={
                    "evidence_fragments": (
                        fragment.model_copy(update={"acquisitions": acquisitions}),
                    )
                }
            )
        return response

    async def enter_safe_state(self, reason: str) -> None:
        await self._delegate.enter_safe_state(reason)


def _campaign(*, budget: int, strategy: str = "random") -> LoadedCampaign:
    loaded = load_campaign(CAMPAIGN_PATH)
    config = loaded.config.model_copy(
        update={
            "campaign": loaded.config.campaign.model_copy(update={"max_experiments": budget}),
            "optimization": loaded.config.optimization.model_copy(
                update={"strategy": strategy, "startup_experiments": min(2, budget)}
            ),
            "simulation": loaded.config.simulation.model_copy(
                update={"outlier_probability": Decimal("0")}
            ),
        }
    )
    return replace(loaded, config=config)


def _service(
    database_path: Path,
    *,
    failure_provider=None,
) -> tuple[Storage, CampaignService]:
    storage = Storage(Database(database_path))
    storage.initialize()
    return storage, CampaignService(
        storage,
        validation_policy=load_validation_policy(POLICY_PATH),
        optimizer_root=database_path.parent / "optimizer",
        prefer_gp=False,
        failure_provider=failure_provider,
    )


def _observations(storage: Storage, campaign_id: str) -> list[tuple[int, dict, dict, bool]]:
    with UnitOfWork(storage.database) as uow:
        assert uow.session is not None
        rows = uow.session.execute(
            select(
                ExperimentRecord.ordinal,
                OptimizerObservationRecord.objective_values,
                OptimizerObservationRecord.constraint_residuals,
                OptimizerObservationRecord.feasible,
            )
            .join(
                OptimizerObservationRecord,
                OptimizerObservationRecord.experiment_id == ExperimentRecord.id,
            )
            .where(ExperimentRecord.campaign_id == campaign_id)
            .order_by(ExperimentRecord.ordinal)
        ).all()
        return [
            (ordinal, objectives, residuals, feasible)
            for ordinal, objectives, residuals, feasible in rows
        ]


def _proposal_parameter_snapshots(storage: Storage, campaign_id: str) -> list[dict]:
    with UnitOfWork(storage.database) as uow:
        assert uow.session is not None
        rows = uow.session.execute(
            select(ProposalRecord.snapshot)
            .join(ExperimentRecord, ProposalRecord.experiment_id == ExperimentRecord.id)
            .where(ExperimentRecord.campaign_id == campaign_id)
            .order_by(ExperimentRecord.ordinal)
        ).scalars()
        return [row["parameter_values"] for row in rows]


def _raw_absorbance_values(
    storage: Storage, campaign_id: str
) -> list[tuple[int, str | None, float]]:
    with UnitOfWork(storage.database) as uow:
        assert uow.session is not None
        return [
            (ordinal, well_id, value)
            for ordinal, well_id, value in uow.session.execute(
                select(
                    ExperimentRecord.ordinal,
                    MeasurementRecord.well_id,
                    MeasurementRecord.value,
                )
                .join(MeasurementRecord, MeasurementRecord.experiment_id == ExperimentRecord.id)
                .where(ExperimentRecord.campaign_id == campaign_id)
                .order_by(ExperimentRecord.ordinal, MeasurementRecord.well_id)
            ).all()
        ]


def test_closed_loop_persists_valid_infeasible_science(tmp_path: Path) -> None:
    storage, service = _service(tmp_path / "campaign.sqlite3")
    campaign_id = service.create(_campaign(budget=3))

    status = asyncio.run(service.run(campaign_id))

    assert status.state is CampaignState.COMPLETED
    assert status.experiment_count == 3
    assert status.valid_observation_count >= 1
    assert status.valid_observation_count + status.rejected_count + status.qc_failed_count == 3
    observations = _observations(storage, str(campaign_id))
    assert any(not feasible for _ordinal, _objectives, _residuals, feasible in observations)
    with UnitOfWork(storage.database) as uow:
        assert uow.session is not None
        measurement_count = uow.session.scalar(select(func.count(MeasurementRecord.id)))
        assert measurement_count is not None and measurement_count > 0


def test_resume_matches_uninterrupted_continuation_from_same_boundary(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.sqlite3"
    clone_path = tmp_path / "clone.sqlite3"
    source_storage, first_service = _service(source_path)
    campaign_id = first_service.create(_campaign(budget=4))
    paused = asyncio.run(first_service.run(campaign_id, max_new_experiments=2))
    assert paused.state is CampaignState.PAUSED

    source_connection = sqlite3.connect(source_path)
    clone_connection = sqlite3.connect(clone_path)
    try:
        source_connection.backup(clone_connection)
    finally:
        clone_connection.close()
        source_connection.close()

    source_final = asyncio.run(first_service.run(campaign_id))
    clone_storage, clone_service = _service(clone_path)
    clone_final = asyncio.run(clone_service.run(campaign_id))

    assert source_final.state is clone_final.state is CampaignState.COMPLETED
    assert _observations(source_storage, str(campaign_id)) == _observations(
        clone_storage, str(campaign_id)
    )
    assert len(_observations(source_storage, str(campaign_id))) == len(
        {row[0] for row in _observations(source_storage, str(campaign_id))}
    )


def test_bayesian_native_store_resumes_without_duplicate_trials(tmp_path: Path) -> None:
    storage, service = _service(tmp_path / "bayesian.sqlite3")
    campaign_id = service.create(_campaign(budget=3, strategy="bayesian"))
    assert (
        asyncio.run(service.run(campaign_id, max_new_experiments=1)).state is CampaignState.PAUSED
    )

    _, resumed_service = _service(tmp_path / "bayesian.sqlite3")
    final = asyncio.run(resumed_service.run(campaign_id))

    assert final.state is CampaignState.COMPLETED
    assert final.experiment_count == 3
    with UnitOfWork(storage.database) as uow:
        experiments = uow.experiments.list_for_campaign(str(campaign_id))
        assert [item.ordinal for item in experiments] == [0, 1, 2]


def test_bayesian_recovers_durable_observation_after_crash_before_tell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, service = _service(tmp_path / "outbox.sqlite3")
    campaign_id = service.create(_campaign(budget=1, strategy="bayesian"))
    original_tell = OptunaStrategy.tell

    def crash_before_tell(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("injected process crash before optimizer tell")

    monkeypatch.setattr(OptunaStrategy, "tell", crash_before_tell)
    with pytest.raises(RuntimeError, match="injected process crash"):
        asyncio.run(service.run(campaign_id))

    with UnitOfWork(storage.database) as uow:
        experiments = uow.experiments.list_for_campaign(str(campaign_id))
        assert experiments[0].state == ExperimentState.ANALYSED.value
        assert uow.optimization.has_observation(
            str(_optimizer_run(uow, str(campaign_id)).id), experiments[0].id
        )

    monkeypatch.setattr(OptunaStrategy, "tell", original_tell)
    _, resumed_service = _service(tmp_path / "outbox.sqlite3")
    final = asyncio.run(resumed_service.run(campaign_id))

    assert final.state is CampaignState.COMPLETED
    assert final.valid_observation_count == 1
    with UnitOfWork(storage.database) as uow:
        experiments = uow.experiments.list_for_campaign(str(campaign_id))
        assert experiments[0].state == ExperimentState.OBSERVED.value
        assert len(_observations(storage, str(campaign_id))) == 1


def test_resume_refuses_analysed_experiment_with_incomplete_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "incomplete-outbox.sqlite3"
    _, service = _service(database_path)
    campaign_id = service.create(_campaign(budget=1))

    def crash_before_tell(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("injected process crash before optimizer tell")

    monkeypatch.setattr(RandomSearchStrategy, "tell", crash_before_tell)
    with pytest.raises(RuntimeError, match="injected process crash"):
        asyncio.run(service.run(campaign_id))
    monkeypatch.undo()

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("DELETE FROM optimizer_observations")
        connection.commit()
    finally:
        connection.close()

    _, resumed_service = _service(database_path)
    with pytest.raises(CampaignExecutionError, match="complete durable observation evidence"):
        asyncio.run(resumed_service.run(campaign_id))


def test_integrity_qc_failure_is_persisted_but_not_observed(tmp_path: Path) -> None:
    storage, service = _service(
        tmp_path / "qc.sqlite3",
        failure_provider=lambda ordinal, _proposal: FailureInjection(
            contaminated_blank=ordinal == 0
        ),
    )
    campaign_id = service.create(_campaign(budget=2))

    status = asyncio.run(service.run(campaign_id))

    assert status.qc_failed_count == 1
    assert status.valid_observation_count == 1
    with UnitOfWork(storage.database) as uow:
        experiments = uow.experiments.list_for_campaign(str(campaign_id))
        assert experiments[0].state == ExperimentState.QC_FAILED.value
        assert not uow.optimization.has_observation(
            str(_optimizer_run(uow, str(campaign_id)).id), experiments[0].id
        )


def test_ambiguous_dispense_failure_stops_and_is_not_observed(tmp_path: Path) -> None:
    storage, service = _service(
        tmp_path / "failure.sqlite3",
        failure_provider=lambda _ordinal, _proposal: FailureInjection(
            ambiguous_dispense_failure=True
        ),
    )
    campaign_id = service.create(_campaign(budget=3))

    status = asyncio.run(service.run(campaign_id))

    assert status.state is CampaignState.PAUSED
    assert status.experiment_count == 1
    assert status.execution_failed_count == 1
    assert status.valid_observation_count == 0
    with UnitOfWork(storage.database) as uow:
        assert uow.session is not None
        run = uow.session.scalar(select(ExecutionRunRecord))
        assert run is not None and run.safe_state_reached is True
        actions = list(uow.session.scalars(select(ActionEventRecord)))
        assert [item.status for item in actions] == ["running", "failed"]
        optimizer_run = _optimizer_run(uow, str(campaign_id))
        assert optimizer_run.status == "paused"
        assert optimizer_run.status_reason is not None
        assert "ambiguous" in optimizer_run.status_reason


def test_injected_backend_factory_is_used_without_optimizer_changes(tmp_path: Path) -> None:
    database_path = tmp_path / "factory.sqlite3"
    storage = Storage(Database(database_path))
    storage.initialize()
    policy = load_validation_policy(POLICY_PATH)
    builder = CampaignService(
        storage,
        validation_policy=policy,
        optimizer_root=tmp_path / "builder-optimizer",
        prefer_gp=False,
    )
    calls: list[int] = []
    wrappers: list[_SpectrophotometerWrapper] = []

    async def factory(config, proposal):
        calls.append(proposal.suggestion_index)
        backend = await builder._build_simulated_backend(config, proposal)
        wrapper = _SpectrophotometerWrapper(backend.spectrophotometer)
        wrappers.append(wrapper)
        backend.registry = await DeviceRegistry.create(
            (backend.liquid_handler, backend.incubator, wrapper)
        )
        return backend

    service = CampaignService(
        storage,
        validation_policy=policy,
        optimizer_root=tmp_path / "optimizer",
        prefer_gp=False,
        backend_factory=factory,
    )
    campaign_id = service.create(_campaign(budget=1))

    status = asyncio.run(service.run(campaign_id))

    assert calls == [0]
    assert wrappers and not hasattr(wrappers[0], "_state")
    assert status.state is CampaignState.COMPLETED
    assert status.valid_observation_count == 1


@pytest.mark.parametrize("fault", ["missing", "malformed"])
def test_invalid_sensor_evidence_is_terminal_and_resumes_at_boundary(
    tmp_path: Path,
    fault: str,
) -> None:
    database_path = tmp_path / f"{fault}.sqlite3"
    storage = Storage(Database(database_path))
    storage.initialize()
    policy = load_validation_policy(POLICY_PATH)
    builder = CampaignService(
        storage,
        validation_policy=policy,
        optimizer_root=tmp_path / f"{fault}-builder-optimizer",
        prefer_gp=False,
    )

    async def factory(config, proposal):
        backend = await builder._build_simulated_backend(config, proposal)
        wrapper = _SpectrophotometerWrapper(backend.spectrophotometer, fault=fault)
        backend.registry = await DeviceRegistry.create(
            (backend.liquid_handler, backend.incubator, wrapper)
        )
        return backend

    service = CampaignService(
        storage,
        validation_policy=policy,
        optimizer_root=tmp_path / f"{fault}-optimizer",
        prefer_gp=False,
        backend_factory=factory,
    )
    campaign_id = service.create(_campaign(budget=3))

    first = asyncio.run(service.run(campaign_id))
    resumed = asyncio.run(service.run(campaign_id))

    assert first.state is resumed.state is CampaignState.PAUSED
    assert first.experiment_count == 1
    assert resumed.experiment_count == 2
    assert first.execution_failed_count == 1
    assert resumed.execution_failed_count == 2
    with UnitOfWork(storage.database) as uow:
        experiments = uow.experiments.list_for_campaign(str(campaign_id))
        assert all(item.state == ExperimentState.FAILED.value for item in experiments)
        assert all(item.failure_category == "invalid_execution_evidence" for item in experiments)


def test_fixed_seed_reproduces_proposals_and_raw_absorbance_across_campaign_ids(
    tmp_path: Path,
) -> None:
    first_storage, first_service = _service(tmp_path / "repro-first.sqlite3")
    second_storage, second_service = _service(tmp_path / "repro-second.sqlite3")
    first_id = first_service.create(_campaign(budget=4))
    second_id = second_service.create(_campaign(budget=4))

    asyncio.run(first_service.run(first_id))
    asyncio.run(second_service.run(second_id))

    assert first_id != second_id
    assert _proposal_parameter_snapshots(first_storage, str(first_id)) == (
        _proposal_parameter_snapshots(second_storage, str(second_id))
    )
    assert _raw_absorbance_values(first_storage, str(first_id)) == _raw_absorbance_values(
        second_storage, str(second_id)
    )


def test_rejected_protocol_causes_zero_execution_calls(tmp_path: Path) -> None:
    loaded = _campaign(budget=1)
    parameters = dict(loaded.config.parameter_space.parameters)
    parameters["reagent_volume"] = parameters["reagent_volume"].model_copy(
        update={"minimum": Decimal("199.9"), "maximum": Decimal("200")}
    )
    parameters["sample_volume"] = parameters["sample_volume"].model_copy(
        update={"minimum": Decimal("99.9"), "maximum": Decimal("100")}
    )
    config = loaded.config.model_copy(
        update={"parameter_space": ParameterSpace(parameters=parameters)}
    )
    loaded = replace(loaded, config=config)
    database_path = tmp_path / "rejected.sqlite3"
    storage = Storage(Database(database_path))
    storage.initialize()
    policy = load_validation_policy(POLICY_PATH).model_copy(
        update={"maximum_well_volume": QuantityValue(value=Decimal("299.7"), unit="microliter")}
    )
    service = CampaignService(
        storage,
        validation_policy=policy,
        optimizer_root=tmp_path / "optimizer",
        prefer_gp=False,
    )
    campaign_id = service.create(loaded)

    status = asyncio.run(service.run(campaign_id))

    assert status.rejected_count == 1
    with UnitOfWork(storage.database) as uow:
        assert uow.session is not None
        assert uow.session.scalar(select(func.count(ExecutionRunRecord.id))) == 0
        assert uow.session.scalar(select(func.count(ActionEventRecord.id))) == 0


def _optimizer_run(uow: UnitOfWork, campaign_id: str) -> OptimizerRunRecord:
    assert uow.session is not None
    record = uow.session.scalar(
        select(OptimizerRunRecord).where(OptimizerRunRecord.campaign_id == campaign_id)
    )
    assert record is not None
    return record
