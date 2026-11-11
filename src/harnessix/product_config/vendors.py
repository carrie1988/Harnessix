"""配置向导的国内模型服务商菜单：接口地址、API Key变量名与推荐模型。

接口地址与变量名核对于2026-11-10各家官方文档（DeepSeek、阿里云百炼、智谱开放平台），变量名沿用
各家文档的写法，用户可同时设好几家的Key、切换服务商不必改环境变量。每家第一个模型是默认选项：
便宜且够日常编码用。上下文窗口、思考方式与牌价不在这里重复登记，仍由按端点核对的各表决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class VendorModel:
    model: str
    note: str


@dataclass(frozen=True, slots=True)
class Vendor:
    vendor_id: str
    name: str
    base_url: str
    api_key_env: str
    models: tuple[VendorModel, ...]
    menu_label: str = ""

    @property
    def label(self) -> str:
        """菜单里的名称；需要说明旗下模型时比name多一段括注。"""

        return self.menu_label or self.name

    @property
    def default_model(self) -> VendorModel:
        return self.models[0]


VENDORS: Final[tuple[Vendor, ...]] = (
    Vendor(
        "deepseek",
        "DeepSeek 官方",
        "https://api.deepseek.com",
        "DEEPSEEK_API_KEY",
        (
            VendorModel("deepseek-flash", "便宜、速度快"),
            VendorModel("deepseek-v4-pro", "能力更强"),
        ),
    ),
    Vendor(
        "bailian",
        "阿里云百炼",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
        (
            VendorModel("qwen3.8-flash", "便宜、速度快"),
            VendorModel("qwen3.8-max", "千问能力最强"),
            VendorModel("deepseek-v4-pro", "百炼提供的 DeepSeek"),
        ),
        menu_label="阿里云百炼（通义千问、DeepSeek）",
    ),
    Vendor(
        "zhipu",
        "智谱 GLM",
        "https://open.bigmodel.cn/api/paas/v4",
        "ZAI_API_KEY",
        (
            VendorModel("glm-5.3-flash", "便宜"),
            VendorModel("glm-5.3-flashx", "比 Flash 更快"),
            VendorModel("glm-5.3", "智谱能力最强"),
        ),
    ),
)
VENDOR_IDS: Final = tuple(item.vendor_id for item in VENDORS)


def vendor_by_id(vendor_id: str) -> Vendor:
    return next(item for item in VENDORS if item.vendor_id == vendor_id)
