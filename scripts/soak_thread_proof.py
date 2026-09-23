"""多Thread Soak的逐轮匿名集合与分页证明。"""

from __future__ import annotations

from hashlib import sha256
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from harnessix.domain.models import ContractModel
from scripts.soak_samples import SoakSample

THREAD_PROOF_FILENAME = "thread-proof.json"
MAX_THREAD_PROOF_BYTES = 8 * 1024 * 1024


def thread_tag(run_id: str, thread_id: str) -> str:
    """使用每Run随机身份域生成不可跨Run关联的Thread标签。"""

    return sha256(bytes.fromhex(run_id) + thread_id.encode("utf-8")).hexdigest()


def thread_set_digest(tags: set[str] | frozenset[str]) -> str:
    """对完整匿名集合按规范顺序计算摘要。"""

    return sha256(("\n".join(sorted(tags)) + "\n").encode("ascii")).hexdigest()


class SoakThreadPage(ContractModel):
    """一个真实列表响应与对应时延样本的低敏索引。"""

    sample_index: StrictInt = Field(gt=0)
    thread_tags: tuple[str, ...] = Field(min_length=1, max_length=200)
    has_next: StrictBool

    @model_validator(mode="after")
    def validate_tags(self) -> Self:
        if any(
            len(tag) != 64 or any(char not in "0123456789abcdef" for char in tag)
            for tag in self.thread_tags
        ):
            raise ValueError("分页Thread标签无效")
        return self


class SoakThreadCycle(ContractModel):
    """一次Runtime/App Service启动后的完整分页事实。"""

    ordinal: StrictInt = Field(ge=1, le=11)
    phase: Literal["warmup", "measure"]
    startup_sample_index: StrictInt = Field(gt=0)
    pages: tuple[SoakThreadPage, ...] = Field(min_length=1, max_length=5000)


class SoakThreadProof(ContractModel):
    """保留逐页匿名标签，使Reader可独立检查集合覆盖与重复。"""

    spec_version: Literal["harnessix.soak-thread-proof/v1"]
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    thread_count: StrictInt = Field(ge=1, le=5000)
    list_limit: StrictInt = Field(ge=1, le=200)
    thread_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cycles: tuple[SoakThreadCycle, ...] = Field(min_length=2, max_length=11)

    @model_validator(mode="after")
    def validate_cycles(self) -> Self:
        for ordinal, cycle in enumerate(self.cycles, start=1):
            if cycle.ordinal != ordinal or cycle.phase != ("warmup" if ordinal == 1 else "measure"):
                raise ValueError("多Thread周期顺序无效")
            if any(
                len(page.thread_tags) > self.list_limit
                or page.has_next != (index < len(cycle.pages))
                for index, page in enumerate(cycle.pages, start=1)
            ):
                raise ValueError("多Thread分页边界无效")
            tags = [tag for page in cycle.pages for tag in page.thread_tags]
            if len(tags) != self.thread_count or len(set(tags)) != self.thread_count:
                raise ValueError("多Thread匿名集合数量或唯一性无效")
            if thread_set_digest(set(tags)) != self.thread_set_sha256:
                raise ValueError("多Thread匿名集合跨轮不一致")
        return self


def verify_thread_proof(
    proof: SoakThreadProof,
    *,
    run_id: str,
    thread_count: int,
    warmup_count: int,
    measured_restarts: int,
    measured_pages: int,
    samples: tuple[SoakSample, ...],
) -> None:
    """从原始样本和逐页匿名标签复算完整覆盖，不信任聚合宣称。"""

    indexed = {sample.sample_index: sample for sample in samples}
    covered: list[int] = []
    for cycle in proof.cycles:
        covered.append(cycle.startup_sample_index)
        covered.extend(page.sample_index for page in cycle.pages)
        for index, metric in (
            (cycle.startup_sample_index, "app_service_startup"),
            *((page.sample_index, "thread_list_page") for page in cycle.pages),
        ):
            sample = indexed.get(index)
            if sample is None or sample.metric != metric or sample.phase != cycle.phase:
                raise ValueError("多Thread证明未覆盖启动或分页样本")
    if (
        proof.run_id != run_id
        or proof.thread_count != thread_count
        or len(proof.cycles) != measured_restarts + 1
        or sum(len(cycle.pages) for cycle in proof.cycles[1:]) != measured_pages
        or proof.cycles[0].startup_sample_index != 1
        or covered != list(range(1, len(covered) + 1))
        or len(covered) != warmup_count + measured_restarts + measured_pages
        or len(samples) != len(covered) + 1
        or samples[-1].metric != "rss_peak"
        or samples[-1].phase != "measure"
    ):
        raise ValueError("多Thread证明与Manifest负载或样本顺序不一致")
