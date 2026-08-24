"""Protocol compiler boundary.

Optimisation strategies produce scientific parameter values. Only a compiler is
allowed to translate those values into device-neutral commands.
"""

from typing import Protocol

from autonomous_lab.campaigns.config import CampaignConfig
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.protocols import ExperimentProtocol


class ProtocolCompiler(Protocol):
    """Translate a proposal into a complete, but not yet executable, protocol."""

    def compile(
        self,
        proposal: ExperimentProposal,
        campaign: CampaignConfig,
    ) -> ExperimentProtocol: ...
