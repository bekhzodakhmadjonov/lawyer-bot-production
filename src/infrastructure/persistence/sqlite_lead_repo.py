from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities import Lead
from domain.value_objects import LeadScore, LeadStatus
from infrastructure.persistence.database import LeadModel


class SQLiteLeadRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, lead: Lead) -> None:
        lead.last_updated_at = datetime.now()
        await self._session.merge(self._to_model(lead))
        await self._session.flush()

    async def get_by_conversation(self, conversation_id: UUID) -> Lead | None:
        stmt = select(LeadModel).where(LeadModel.conversation_id == conversation_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def get_by_user(self, user_id: UUID) -> Lead | None:
        stmt = select(LeadModel).where(LeadModel.user_id == user_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def list_open(self, *, limit: int = 10) -> tuple[Lead, ...]:
        closed_statuses = (
            LeadStatus.PAID.value,
            LeadStatus.LOST.value,
            LeadStatus.CLOSED.value,
        )
        stmt = (
            select(LeadModel)
            .where(LeadModel.status.not_in(closed_statuses))
            .order_by(LeadModel.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return tuple(self._to_entity(model) for model in result.scalars().all())

    async def update_status(
        self, conversation_id: UUID, status: LeadStatus
    ) -> Lead | None:
        lead = await self.get_by_conversation(conversation_id)
        if lead is None:
            return None
        lead.mark_status(status)
        await self.save(lead)
        return lead

    async def count_all(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(LeadModel)
        )
        return int(result.scalar_one())

    async def count_since(self, since: datetime) -> int:
        stmt = (
            select(func.count())
            .select_from(LeadModel)
            .where(LeadModel.created_at >= since)
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def count_by_status(self, status: LeadStatus) -> int:
        stmt = (
            select(func.count())
            .select_from(LeadModel)
            .where(LeadModel.status == status.value)
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def list_with_pagination(
        self,
        *,
        offset: int = 0,
        limit: int = 10,
        status_filter: str | None = None,
        sort_by_score: bool = False,
    ) -> tuple[Lead, ...]:
        """List leads with pagination, optional status filter, and score sorting."""
        stmt = select(LeadModel)

        if status_filter:
            if status_filter == "ochiq":
                closed_statuses = (
                    LeadStatus.PAID.value,
                    LeadStatus.LOST.value,
                    LeadStatus.CLOSED.value,
                )
                stmt = stmt.where(LeadModel.status.not_in(closed_statuses))
            elif status_filter == "yangi":
                stmt = stmt.where(LeadModel.status == LeadStatus.NEW.value)
            elif status_filter == "yopiq":
                closed_statuses = (
                    LeadStatus.PAID.value,
                    LeadStatus.LOST.value,
                    LeadStatus.CLOSED.value,
                )
                stmt = stmt.where(LeadModel.status.in_(closed_statuses))
            else:
                # Try to match exact status value
                stmt = stmt.where(LeadModel.status == status_filter)

        if sort_by_score:
            stmt = stmt.order_by(
                LeadModel.score_value.desc(), LeadModel.created_at.desc()
            )
        else:
            stmt = stmt.order_by(LeadModel.created_at.desc())

        stmt = stmt.offset(offset).limit(limit)
        result = await self._session.execute(stmt)
        return tuple(self._to_entity(model) for model in result.scalars().all())

    async def count_by_filter(self, status_filter: str | None = None) -> int:
        """Count leads with optional status filter."""
        stmt = select(func.count()).select_from(LeadModel)

        if status_filter:
            if status_filter == "ochiq":
                closed_statuses = (
                    LeadStatus.PAID.value,
                    LeadStatus.LOST.value,
                    LeadStatus.CLOSED.value,
                )
                stmt = stmt.where(LeadModel.status.not_in(closed_statuses))
            elif status_filter == "yangi":
                stmt = stmt.where(LeadModel.status == LeadStatus.NEW.value)
            elif status_filter == "yopiq":
                closed_statuses = (
                    LeadStatus.PAID.value,
                    LeadStatus.LOST.value,
                    LeadStatus.CLOSED.value,
                )
                stmt = stmt.where(LeadModel.status.in_(closed_statuses))
            else:
                stmt = stmt.where(LeadModel.status == status_filter)

        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    @staticmethod
    def _to_model(lead: Lead) -> LeadModel:
        return LeadModel(
            id=lead.id,
            conversation_id=lead.conversation_id,
            user_id=lead.user_id,
            score_value=lead.score.value,
            score_reasons=list(lead.score.reasons),
            topic_summary=lead.topic_summary,
            contact_info=lead.contact_info,
            status=lead.status.value,
            created_at=lead.created_at,
            last_updated_at=lead.last_updated_at,
        )

    @staticmethod
    def _to_entity(model: LeadModel) -> Lead:
        return Lead(
            id=model.id,
            conversation_id=model.conversation_id,
            user_id=model.user_id,
            score=LeadScore(
                value=model.score_value, reasons=tuple(model.score_reasons)
            ),
            topic_summary=model.topic_summary,
            contact_info=model.contact_info,
            status=LeadStatus(model.status or LeadStatus.NEW.value),
            created_at=model.created_at,
            last_updated_at=model.last_updated_at,
        )
