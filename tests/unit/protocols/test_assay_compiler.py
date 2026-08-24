from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from autonomous_lab.campaigns.loader import load_campaign
from autonomous_lab.domain.enums import CommandType, ControlType
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.protocols import MixCommand
from autonomous_lab.protocols.assay_compiler import AssayProtocolCompiler

ROOT = Path(__file__).resolve().parents[3]


def _proposal() -> ExperimentProposal:
    campaign = load_campaign(ROOT / "configs" / "assay-reagent-reduction.yaml").config
    space = campaign.parameter_space.parameters
    return ExperimentProposal(
        proposal_id=UUID("00000000-0000-0000-0000-000000000101"),
        experiment_id=UUID("00000000-0000-0000-0000-000000000102"),
        campaign_id=UUID("00000000-0000-0000-0000-000000000103"),
        suggestion_index=0,
        parameter_values={
            "reagent_concentration": space["reagent_concentration"].quantity(0.5),
            "reagent_volume": space["reagent_volume"].quantity(100),
            "sample_volume": space["sample_volume"].quantity(50),
            "incubation_time": space["incubation_time"].quantity(10),
            "temperature": space["temperature"].quantity(25),
        },
        strategy_name="random",
        strategy_version="1",
        strategy_trial_id="trial-0",
        created_at=datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
    )


def test_compiler_builds_complete_v0_assay_batch() -> None:
    campaign = load_campaign(ROOT / "configs" / "assay-reagent-reduction.yaml").config
    proposal = _proposal()

    protocol = AssayProtocolCompiler().compile(proposal, campaign)

    assert len(protocol.vessels) == 27
    assert len(protocol.steps) == 27 * 3 + 2
    assert len({step.step_id for step in protocol.steps}) == len(protocol.steps)
    assert [control.control_type for control in protocol.controls].count(ControlType.BLANK) == 1
    assert [control.control_type for control in protocol.controls].count(ControlType.STANDARD) == 6
    assert [control.control_type for control in protocol.controls].count(ControlType.POSITIVE) == 1
    assert [control.control_type for control in protocol.controls].count(
        ControlType.CHECK_STANDARD
    ) == 1
    command_counts = {
        command_type: sum(step.command.command_type is command_type for step in protocol.steps)
        for command_type in CommandType
    }
    assert command_counts == {
        CommandType.DISPENSE: 54,
        CommandType.ASPIRATE: 0,
        CommandType.TRANSFER: 0,
        CommandType.MIX: 27,
        CommandType.INCUBATE: 1,
        CommandType.MEASURE_ABSORBANCE: 1,
    }


def test_compiler_binds_recipe_to_source_proposal_and_material_properties() -> None:
    campaign = load_campaign(ROOT / "configs" / "assay-reagent-reduction.yaml").config
    proposal = _proposal()

    protocol = AssayProtocolCompiler().compile(proposal, campaign)
    reagent = next(material for material in protocol.materials if material.name == "reagent")

    assert protocol.experiment_id == proposal.experiment_id
    assert protocol.source_proposal_id == proposal.proposal_id
    assert protocol.parameter_values == proposal.parameter_values
    assert reagent.properties["concentration"] == proposal.parameter_values["reagent_concentration"]
    assert protocol.protocol_hash is None


def test_compilation_is_deterministic_for_the_same_input() -> None:
    campaign = load_campaign(ROOT / "configs" / "assay-reagent-reduction.yaml").config
    proposal = _proposal()
    compiler = AssayProtocolCompiler()

    first = compiler.compile(proposal, campaign)
    second = compiler.compile(proposal, campaign)

    assert first == second
    assert first.protocol_id == second.protocol_id
    assert tuple(step.step_id for step in first.steps) == tuple(
        step.step_id for step in second.steps
    )


def test_mix_stroke_is_bounded_quantized_and_does_not_aspirate_total_well_volume() -> None:
    campaign = load_campaign(ROOT / "configs" / "assay-reagent-reduction.yaml").config
    proposal = _proposal()
    values = dict(proposal.parameter_values)
    values["reagent_volume"] = campaign.parameter_space.parameters["reagent_volume"].quantity(200)
    values["sample_volume"] = campaign.parameter_space.parameters["sample_volume"].quantity(100)
    proposal = proposal.model_copy(update={"parameter_values": values})

    protocol = AssayProtocolCompiler().compile(proposal, campaign)
    mix_commands = [step.command for step in protocol.steps if isinstance(step.command, MixCommand)]

    assert {command.volume.to("microliter").value for command in mix_commands} == {200}
    assert all(command.volume.to("microliter").value <= 250 for command in mix_commands)
    assert all(command.volume.to("microliter").value < 300 for command in mix_commands)
