"""Aggregate router mounted under ``API_PREFIX``.

Feature routers are included here as they are built.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.admin.router import router as admin_system_router
from app.attestation.router import (
    admin_router as attestation_admin_router,
    router as attestation_router,
)
from app.auth.oauth.router import router as oauth_router
from app.auth.router import router as auth_router
from app.catalog.router import admin_router as catalog_admin_router, router as catalog_router
from app.media.router import item_router as media_item_router, router as media_router
from app.provenance.router import router as provenance_router
from app.qr.router import admin_router as qr_admin_router, router as qr_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(oauth_router)
api_router.include_router(catalog_router)
api_router.include_router(catalog_admin_router)
api_router.include_router(provenance_router)
api_router.include_router(attestation_router)
api_router.include_router(attestation_admin_router)
api_router.include_router(media_router)
api_router.include_router(media_item_router)
api_router.include_router(qr_router)
api_router.include_router(qr_admin_router)
api_router.include_router(admin_system_router)

__all__ = ["api_router"]
