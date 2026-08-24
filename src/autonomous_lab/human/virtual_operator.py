"""Test-only operator automation that confirms worklists, not laboratory reality."""

from __future__ import annotations

from datetime import datetime

from autonomous_lab.domain.identifiers import utc_now
from autonomous_lab.human.backend import HumanExecutionBackend
from autonomous_lab.human.models import (
    AcknowledgementDecision,
    HumanExecutionRecord,
    ProtocolDeviation,
)
from autonomous_lab.human.worklists import OperatorWorklist


class VirtualOperator:
    """Confirm a worklist deterministically in automated tests.

    This fixture never creates measurements or calls the assay simulator.  It
    merely produces the evidence that a named test operator acknowledged each
    grouped procedure action.
    """

    def __init__(self, operator_id: str = "virtual-operator-fixture") -> None:
        if not operator_id.strip():
            raise ValueError("operator_id must not be empty")
        self.operator_id = operator_id.strip()

    def confirm(
        self,
        worklist: OperatorWorklist,
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        deviations_by_group: dict[str, tuple[ProtocolDeviation, ...]] | None = None,
    ) -> HumanExecutionRecord:
        start = started_at or utc_now()
        finish = completed_at or start
        if finish < start:
            raise ValueError("completed_at cannot precede started_at")
        deviations_by_group = deviations_by_group or {}
        backend = HumanExecutionBackend()
        session = backend.start(worklist, operator_id=self.operator_id, started_at=start)
        for group in worklist.procedure:
            deviations = deviations_by_group.get(group.group_id, ())
            session = backend.acknowledge(
                session,
                group_id=group.group_id,
                decision=(
                    AcknowledgementDecision.RECORD_DEVIATION
                    if deviations
                    else AcknowledgementDecision.CONFIRM
                ),
                acknowledged_at=finish,
                deviations=deviations,
                note="Confirmed by automated operator fixture; no measurement was generated.",
            )
        return backend.complete(session, completed_at=finish)
