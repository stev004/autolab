"""Transactional unit of work used by campaign and orchestration services."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from autonomous_lab.storage.database import Database
from autonomous_lab.storage.repositories import (
    CampaignRepository,
    ExperimentRepository,
    OptimizerRepository,
    ProvenanceRepository,
    ScientificRecordRepository,
)

if TYPE_CHECKING:
    from autonomous_lab.measurement_ingestion.repository import MeasurementIngestionRepository


class UnitOfWork:
    """Group one scientific state change and its provenance in one transaction.

    Callers must call :meth:`commit` explicitly.  Leaving the context without a
    commit always rolls back, which protects against accidentally persisting a
    partial execution step.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.session: Session | None = None
        self.campaigns: CampaignRepository
        self.experiments: ExperimentRepository
        self.records: ScientificRecordRepository
        self.optimization: OptimizerRepository
        self.provenance: ProvenanceRepository
        self.measurement_ingestion: MeasurementIngestionRepository
        self._committed = False

    def __enter__(self) -> UnitOfWork:
        from autonomous_lab.measurement_ingestion.repository import (
            MeasurementIngestionRepository,
        )

        if self.session is not None:
            raise RuntimeError("UnitOfWork cannot be re-entered")
        self.session = self.database.session_factory()
        self.campaigns = CampaignRepository(self.session)
        self.experiments = ExperimentRepository(self.session)
        self.records = ScientificRecordRepository(self.session)
        self.optimization = OptimizerRepository(self.session)
        self.provenance = ProvenanceRepository(self.session)
        self.measurement_ingestion = MeasurementIngestionRepository(self.session)
        return self

    def flush(self) -> None:
        self._require_session().flush()

    def commit(self) -> None:
        self._require_session().commit()
        self._committed = True

    def rollback(self) -> None:
        self._require_session().rollback()
        self._committed = False

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._require_session()
        try:
            if exc_type is not None or not self._committed:
                session.rollback()
        finally:
            session.close()
            self.session = None

    def _require_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("UnitOfWork is not active")
        return self.session


class Storage:
    """Small application-facing facade for schema setup and units of work."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @classmethod
    def open(cls, location: str = "data/autolab.sqlite3", *, echo: bool = False) -> Storage:
        return cls(Database(location, echo=echo))

    def initialize(self) -> None:
        """Upgrade a local installation to the repository's current schema.

        Tests may still use :meth:`Database.create_schema` for short-lived,
        isolated databases.  User-facing initialization deliberately goes
        through Alembic so an existing scientific record is upgraded rather
        than silently diverging from the migration history.
        """

        project_root = Path(__file__).resolve().parents[3]
        config = Config(str(project_root / "alembic.ini"))
        config.set_main_option("script_location", str(project_root / "migrations"))
        config.set_main_option(
            "sqlalchemy.url",
            self.database.engine.url.render_as_string(hide_password=False),
        )
        command.upgrade(config, "head")

    def unit_of_work(self) -> UnitOfWork:
        return UnitOfWork(self.database)

    def close(self) -> None:
        self.database.dispose()
