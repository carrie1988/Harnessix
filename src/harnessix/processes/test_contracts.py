"""测试profile与公开run_tests输入的无运行时依赖契约。"""

from __future__ import annotations

from typing import Self

from pydantic import Field, field_validator, model_validator

from harnessix.processes.contracts import ProcessContract, ProcessRequest

RUN_TESTS_POLICY = "run-tests/v1"
MAX_TEST_PROFILES = 32


class TestProfile(ProcessContract):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    description: str = Field(min_length=1, max_length=256)
    program: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    arguments: tuple[str, ...] = Field(default=(), max_length=128, repr=False)
    timeout_seconds: float = Field(default=300.0, gt=0, le=3600)

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ProcessRequest(program="profile", arguments=value)
        return value


class RunTestsInput(ProcessContract):
    profile: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


class TestProfiles(ProcessContract):
    profiles: tuple[TestProfile, ...] = Field(min_length=1, max_length=MAX_TEST_PROFILES)

    @model_validator(mode="after")
    def unique_sorted_profiles(self) -> Self:
        names = [profile.name for profile in self.profiles]
        if names != sorted(set(names)):
            raise ValueError("测试profile必须按名称唯一排序")
        return self

    def get(self, name: str) -> TestProfile | None:
        return next((profile for profile in self.profiles if profile.name == name), None)
