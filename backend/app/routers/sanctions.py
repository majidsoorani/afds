"""Sanctions screening and entity resolution endpoints."""

import logging
from difflib import SequenceMatcher

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.schemas import SanctionsScreenRequest, SanctionsMatchResponse
from app.services.screening import screen_entity

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sanctions", tags=["Sanctions"])




def _offline_screen(name: str, threshold: float = 0.6) -> list[SanctionsMatchResponse]:
    """Fuzzy match against offline sanctions list using SequenceMatcher."""
    results = []
    query = name.upper()
    for entry in _OFFLINE_SANCTIONS:
        ratio = SequenceMatcher(None, query, entry["name"]).ratio()
        if ratio >= threshold:
            results.append(SanctionsMatchResponse(
                entity_id=entry["entity_id"],
                matched_name=entry["name"],
                similarity=round(ratio, 4),
                source=entry.get("source", ""),
                entity_type=entry.get("entity_type", ""),
                nationality=entry.get("nationality", ""),
                designation_date=entry.get("designation_date", ""),
                reason=entry.get("reason", ""),
            ))
    results.sort(key=lambda x: x.similarity, reverse=True)
    return results


async def _get_db_optional():
    """Yield a DB session if available, else None."""
    try:
        async for session in get_db():
            yield session
            return
    except Exception:
        yield None


@router.post("/screen", response_model=list[SanctionsMatchResponse])
async def screen_name(
    request: SanctionsScreenRequest,
    db: AsyncSession = Depends(get_db),
):
    """Screen a name against OpenSanctions/OFAC/UN sanctions lists.
    Uses pg_trgm for sub-millisecond fuzzy matching (falls back to offline list).
    """
    return await screen_entity(db, request)
