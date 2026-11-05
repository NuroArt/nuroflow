from __future__ import annotations

from typing import Self

from pydantic import model_validator

from nuroflow.api.v1.schemas.common import Schema
from nuroflow.domain.enums import UserRole


class UserUpdate(Schema):
    role: UserRole | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> Self:
        if self.role is None and self.is_active is None:
            raise ValueError("Provide at least one of: role, is_active")
        return self
