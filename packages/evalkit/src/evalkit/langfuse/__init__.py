"""运行报告到Langfuse的只读投影、发送与读回核验；本地证据保持权威。

对象映射：
- Dataset：计划中的数据集清单；
- Experiment：报告 × 组别，同一条目的同一重复在各组别间共享条目ID，便于对比；
- Trace：一个试次（槽位），根Observation承载白名单元数据；
- Score：试次上全部判分器的分数（名称为“判分器/分数”），以及主指标结果和耗时。
"""

from evalkit.langfuse.contracts import LangfuseExport, LangfuseReadback
from evalkit.langfuse.projection import build_export
from evalkit.langfuse.readback import read_back
from evalkit.langfuse.transport import send

__all__ = ["LangfuseExport", "LangfuseReadback", "build_export", "read_back", "send"]
