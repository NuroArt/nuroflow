"""Aggregates every v1 router. A future v2 gets its own module and is mounted beside v1."""

from __future__ import annotations

from fastapi import APIRouter

from nuroflow.api.v1.endpoints import api_keys, auth, runs, triggers, users, workflows

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(api_keys.router)
api_router.include_router(workflows.router)
api_router.include_router(runs.router)
api_router.include_router(triggers.router)
