"""产品配置边界错误别名，避免调用方依赖Agent内部包。"""

from harnessix.agent.errors import KernelError as ProductConfigError

__all__ = ["ProductConfigError"]
