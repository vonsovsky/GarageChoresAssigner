"""Pydantic request/response models for our own API."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    discord_handle: str = Field(min_length=1, max_length=80)
    skills: list[str] = Field(default_factory=list)


class ChoreCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    necessary_workers: int = Field(default=1, ge=1, le=20)
    estimated_time_min: int = Field(default=10, ge=1, le=1000)
    assignment_timeout_min: int = Field(default=15, ge=1, le=1000)
    necessary_capabilities: list[str] = Field(default_factory=list)
    deadline: Optional[str] = None  # ISO 8601
    urgent: bool = False
    # optional link back to the preset it was created from
    template_key: Optional[str] = None
    # scale head-count chores (dishes/cooking) by number of people eating
    headcount: Optional[int] = None


class ManualWorkIn(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    minutes: int = Field(ge=1, le=1000)


class ProfileSettingsIn(BaseModel):
    # Name/handle come from Discord; only skills are user-editable.
    skills: list[str] = Field(default_factory=list)


class LoginIn(BaseModel):
    discord_handle: str = Field(min_length=1, max_length=80)


class AssignIn(BaseModel):
    # None → auto-assign the lowest-workload eligible person
    discord_id: Optional[str] = None


class MergeIn(BaseModel):
    from_id: str = Field(min_length=1)  # duplicate identity to fold in
    to_id: str = Field(min_length=1)    # canonical identity to keep


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    necessary_workers: int = Field(default=1, ge=1, le=20)
    estimated_time_min: int = Field(default=10, ge=1, le=1000)
    assignment_timeout_min: int = Field(default=15, ge=1, le=1000)
    necessary_capabilities: list[str] = Field(default_factory=list)
    scales_with_headcount: bool = False
    per_person_min: int = Field(default=0, ge=0, le=120)
