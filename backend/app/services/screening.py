"""Sanctions screening service using PostgreSQL pg_trgm fuzzy matching."""

import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schemas import SanctionsScreenRequest, SanctionsMatchResponse

logger = logging.getLogger(__name__)


async def screen_entity(
    db: AsyncSession,
    request: SanctionsScreenRequest,
) -> list[SanctionsMatchResponse]:
    """Screen a name against the sanctions database using fuzzy matching."""

    result = await db.execute(
        text(
            "SELECT entity_id, matched_name, similarity "
            "FROM sanctions.search_entity_names(:name, :threshold, :max_results)"
        ),
        {
            "name": request.name,
            "threshold": request.threshold,
            "max_results": request.max_results,
        },
    )

    rows = result.fetchall()
    return [
        SanctionsMatchResponse(
            entity_id=row.entity_id,
            matched_name=row.matched_name,
            similarity=float(row.similarity),
        )
        for row in rows
    ]
