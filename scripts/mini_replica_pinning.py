"""mini-swe-agent model class that pins each worker thread to one model replica.

The reference arm R runs `mini-extra swebench -w 4`. With a single `api_base` all four workers
share one replica, while the preregistration allows at most two tasks per replica (as the
Harnessix arms run). Each worker thread processes its instances sequentially, so pinning
threads round-robin to the replicas keeps exactly `workers / replicas` tasks per replica and
keeps every trajectory on one replica (prefix cache stays warm).

Install next to mini-swe-agent (site-packages) and select it in the R config:
    model:
      model_class: "mini_replica_pinning.PinnedLitellmModel"
      replica_api_bases: ["http://127.0.0.1:18001/v1", "http://127.0.0.1:18002/v1"]
"""

from __future__ import annotations

import threading

from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig

_lock = threading.Lock()
_thread_slots: dict[int, int] = {}


class PinnedLitellmModelConfig(LitellmModelConfig):
    replica_api_bases: list[str]


def _thread_slot() -> int:
    """Stable 0-based index of the calling worker thread, in first-seen order."""

    ident = threading.get_ident()
    with _lock:
        return _thread_slots.setdefault(ident, len(_thread_slots))


class PinnedLitellmModel(LitellmModel):
    def __init__(self, **kwargs):
        super().__init__(config_class=PinnedLitellmModelConfig, **kwargs)
        bases = self.config.replica_api_bases
        if not bases:
            raise ValueError("replica_api_bases must name at least one endpoint")
        api_base = bases[_thread_slot() % len(bases)]
        self.config.model_kwargs = {**self.config.model_kwargs, "api_base": api_base}
