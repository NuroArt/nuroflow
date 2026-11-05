"""/api-keys: manage machine credentials for webhooks, events and integrations."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from nuroflow.api.deps import ApiKeyServiceDep, CurrentPrincipal
from nuroflow.api.v1.schemas.api_keys import ApiKeyCreate, ApiKeyCreated, ApiKeyRead

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@router.get("", response_model=list[ApiKeyRead])
async def list_api_keys(principal: CurrentPrincipal, keys: ApiKeyServiceDep) -> list[ApiKeyRead]:
    return [ApiKeyRead.model_validate(k) for k in await keys.list(principal)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ApiKeyCreated)
async def create_api_key(
    body: ApiKeyCreate, principal: CurrentPrincipal, keys: ApiKeyServiceDep
) -> ApiKeyCreated:
    """Create a key. **The full key appears only in this response.** Only a hash is stored."""
    created = await keys.create(
        principal, name=body.name, scopes=body.scopes, expires_in_days=body.expires_in_days
    )
    read = ApiKeyRead.model_validate(created.api_key)
    return ApiKeyCreated(**read.model_dump(exclude={"hint"}), key=created.plaintext)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: uuid.UUID, principal: CurrentPrincipal, keys: ApiKeyServiceDep
) -> None:
    await keys.revoke(principal, key_id)
