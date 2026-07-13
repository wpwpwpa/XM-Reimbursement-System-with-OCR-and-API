"""LLM 后端抽象（用于 OCR 正则解析失败时的兜底）。

不包含 GGUF/进程内模型——LLM 后端仅支持：
  - 正则判断（= 不调 LLM，纯正则）
  - 本地 API（OpenAI 兼容，如 LM Studio / Ollama）
  - 云端 API（OpenAI 兼容）
"""
from __future__ import annotations

import base64
import json
import re
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from order_fields import normalize_date

# OCR + 文字模型模式：发给 LLM 的默认提示词（用户可在模型选择页覆盖）
_OCR_PROMPT = (
    "你是订单商品提取助手。从订单 OCR 文本中提取信息，只返回 JSON：\n"
    "1. amount：实付金额（最终付款数字）\n"
    "2. platform：平台名称（拼小圈/拼多多→拼多多，闪购/美团→美团，天猫/淘宝/企→淘宝，其他→null）\n"
    "3. order_time：下单时间（拼多多取「下单时间」字段、美团取「期望时间」字段；格式 YYYY-MM-DD 或 M月D日；淘宝填 null）\n"
    "4. product_name：商品完整名称（严格遵守以下剥离规则）\n"
    "\n"
    "product_name 剥离规则（必须执行）：\n"
    "- 只保留商品标题本身，删除所有：规格型号（如500克/袋、50g）、数量、实付金额、打包费、配送费\n"
    "- 删除店铺名/商家名（如「菜来了」「长胜市场店」「小橙阿姨」「绿氧森林园艺店」等）\n"
    "- 删除快递/物流信息（如「快递单号」「申通快递」「极兔速递」「发生交易争议」「包裹已签收」等）\n"
    "- 删除按钮文字（联系商家、申请退款、分享商品、查看物流、再次拼单、确认收货等）\n"
    "- 删除地址、手机号、会员成长值、评价、晒单等无关内容\n"
    "- 若无法确定商品名，填 null\n"
    "\n"
    "只返回 JSON：{\"amount\": 数字或null, \"platform\": 字符串或null, "
    "\"product_name\": 字符串或null, \"order_time\": 字符串或null}\n"
    "不要输出其他内容。"
)


_MODEL_PLACEHOLDER = "gpt-4o-mini"


def _build_chat_url(endpoint: str) -> str:
    """拼接 chat/completions URL（兼容端点以 /v1 结尾或裸 base_url 两种写法）。"""
    endpoint = endpoint.rstrip("/")
    if endpoint.endswith("/v1"):
        return f"{endpoint}/chat/completions"
    return f"{endpoint}/v1/chat/completions"


def _list_model_ids(body: str) -> list | None:
    """解析 /v1/models 响应，返回已加载模型 id 列表；解析失败返回 None（不阻断校验）。"""
    try:
        data = json.loads(body)
    except Exception:
        return None
    return [m.get("id") for m in data.get("data", [])]


class LLMBackend(ABC):
    """LLM 解析后端基类。"""

    @abstractmethod
    def parse(self, ocr_text: str) -> dict:
        """用 LLM 从 OCR 文本中抽取金额与平台。

        返回字典：{status, amount, platform, confidence, reason?}
        """

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """测试连接是否可达。返回 (成功, 消息)。"""


class OpenAICompatibleBackend(LLMBackend):
    """OpenAI 兼容 API 后端（本地 / 云端通用）。

    使用标准库 urllib，零额外依赖。
    """

    def __init__(self, endpoint: str, api_key: str = "", model: str = "",
                 prompt: str | None = None):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.model = model or "gpt-4o-mini"
        self.prompt = prompt  # None → 回退内置 _OCR_PROMPT

    def parse(self, ocr_text: str) -> dict:
        url = _build_chat_url(self.endpoint)
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": self.prompt or _OCR_PROMPT,
                },
                {"role": "user", "content": ocr_text[:2000]},
            ],
            "temperature": 0.0,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {
                "status": "failed", "amount": None, "platform": None,
                "confidence": 0.0, "reason": f"LLM 请求失败: {e}",
            }

        content = data.get("choices", [{}])[0].get("message", {})
        raw_content = content.get("content", "")
        try:
            result = _extract_json(raw_content)
            if not result:
                raise json.JSONDecodeError("empty", raw_content, 0)
            _plat = result.get("platform")
            if _plat and "天猫" in str(_plat):
                _plat = "淘宝"
            return {
                "status": "success",
                "amount": result.get("amount"),
                "platform": _plat,
                "confidence": 0.85,
                "used_llm": True,
                "product_name": result.get("product_name") or None,
                "order_time": normalize_date(result.get("order_time")),
            }
        except (json.JSONDecodeError, AttributeError):
            return {
                "status": "failed", "amount": None, "platform": None,
                "confidence": 0.0, "reason": f"LLM 返回格式异常: {raw_content[:100]}",
            }

    def _test_url(self) -> str:
        """根据端点生成测试连接 URL。

        本地端口（localhost/127.0.0.1）：拼接 /v1/models。
        云端 API：如果端点已以 /v1 结尾则拼 /models，否则拼 /v1/models。
        """
        ep = self.endpoint
        if ep.startswith(("http://localhost", "http://127.0.0.1")):
            return f"{ep}/v1/models"
        # 云端：兼容用户填 base_url 或 base_url/v1 两种写法
        if ep.endswith("/v1"):
            return f"{ep}/models"
        return f"{ep}/v1/models"

    def test_connection(self) -> tuple[bool, str]:
        if not self.endpoint:
            return False, "请先填写端点"
        if self.endpoint.startswith(("http://localhost", "http://127.0.0.1")):
            # 本地 API 不要求 key
            pass
        elif not self.api_key:
            return False, "请先填写 API Key"

        try:
            url = self._test_url()
            req = urllib.request.Request(url)
            if self.api_key:
                req.add_header("Authorization", f"Bearer {self.api_key}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.URLError as e:
            return False, f"连接失败: {e.reason}"
        except Exception as e:
            return False, f"连接失败: {e}"

        # 端点可达 ≠ 模型就绪：校验所填模型是否已加载（仅当显式指定、非默认占位时）
        if self.model and self.model != _MODEL_PLACEHOLDER:
            ids = _list_model_ids(body)
            if ids is not None and self.model not in ids:
                avail = ", ".join(ids) if ids else "无"
                return False, f"连接成功，但模型「{self.model}」未加载（可用: {avail}）"
        loaded = (self.model and self.model != _MODEL_PLACEHOLDER)
        return True, "连接成功（模型已加载）" if loaded else "连接成功"


class StubLLMBackend(LLMBackend):
    """测试用假后端。始终返回固定结果。"""

    def parse(self, ocr_text: str) -> dict:
        return {
            "status": "success", "amount": 99.9, "platform": "Stub平台",
            "confidence": 1.0, "used_llm": True,
        }

    def test_connection(self) -> tuple[bool, str]:
        return True, "Stub 连接正常"


def build_llm_backend(kind: str, cfg: dict) -> LLMBackend | None:
    """根据 kind 和配置构建后端实例。配置不足时返回 None。

    kind: "正则判断" | "本地API" | "云端API"
    """
    if kind == "正则判断":
        # 正则模式不构建任何 LLM 实例
        return None

    if kind == "本地API":
        endpoint = (cfg.get("endpoint") or "").strip()
        if not endpoint:
            return None
        return OpenAICompatibleBackend(
            endpoint=endpoint,
            api_key=cfg.get("api_key", ""),
            model=cfg.get("model", ""),
            prompt=cfg.get("prompt"),
        )

    if kind == "云端API":
        endpoint = (cfg.get("endpoint") or "").strip()
        api_key = (cfg.get("api_key") or "").strip()
        if not endpoint or not api_key:
            return None
        return OpenAICompatibleBackend(
            endpoint=endpoint,
            api_key=api_key,
            model=cfg.get("model", ""),
            prompt=cfg.get("prompt"),
        )

    return None


# ============ 视觉模式后端（截图直送多模态大模型）============
VISION_WHITELIST = {"淘宝", "美团", "拼多多"}

_VISION_PROMPT = (
    "你是订单商品提取助手。请观察图片，严格提取以下信息，只返回 JSON：\n"
    "1. amount：实付金额（数字，取最终付款金额，如 26.52）\n"
    "2. platform：购物平台，严格按特征词判定（看到即判，不要凭界面风格猜测）：\n"
    "   - 截图出现「拼小圈」→ 拼多多\n"
    "   - 截图出现「闪购」→ 美团\n"
    "   - 截图出现「天猫」「淘宝」或「企」（如淘宝企业购）→ 淘宝\n"
    "   - 以上都不匹配 → null（不要猜测其他平台）\n"
    "3. order_time：下单时间（拼多多取「下单时间」、美团取「期望时间」；格式 YYYY-MM-DD 或 M月D日；淘宝填 null）\n"
    "4. product_name：商品完整名称（严格遵守以下剥离规则）\n"
    "\n"
    "product_name 剥离规则（必须执行）：\n"
    "- 只保留商品标题本身，删除所有：规格型号（如500克/袋、50g）、数量、实付金额、打包费、配送费\n"
    "- 删除店铺名/商家名（如「菜来了」「长胜市场店」「小橙阿姨」「绿氧森林园艺店」等）\n"
    "- 删除快递/物流信息（如「快递单号」「申通快递」「极兔速递」「发生交易争议」「包裹已签收」等）\n"
    "- 删除按钮文字（联系商家、申请退款、分享商品、查看物流、再次拼单、确认收货等）\n"
    "- 删除地址、手机号、会员成长值、评价、晒单等无关内容\n"
    "- 若无法确定商品名，填 null\n"
    "\n"
    "只返回 JSON：{\"amount\": 数字或null, \"platform\": 字符串或null, "
    "\"order_time\": 字符串或null, \"product_name\": 字符串或null}\n"
    "不要输出其他任何内容。"
)


def _extract_json(content: str) -> dict:
    """容错提取 JSON：取第一个 { 到最后一个 } 之间的内容。"""
    try:
        s = content.strip()
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(s[start:end + 1])
    except Exception:
        pass
    return {}


def _parse_vision_time(raw):
    """解析视觉模型返回的时间。

    不同平台能力不同：
    - 拼多多通常能读出完整年月日（如 2026-06-29）→ 合理年份直接保留
    - 美团通常只有月日（如 07-05）→ 年份待补（0000）
    - 淘宝填 null → 后期由 Excel 补全
    年份合理性判定范围 1990~2100；超出此范围的年份视为异常，丢弃待补全。
    返回 "YYYY-MM-DD" / "0000-MM-DD" 或 None。
    """
    if not raw:
        return None
    s = str(raw).strip()
    # 完整 YYYY-MM-DD：年份合理则保留，否则仅取月日
    m = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        y = int(m.group(1))
        if 1990 <= y <= 2100:
            return f"{y}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        # 年份异常（如模型把2026写成2023）→ 仅保留月日待补
        return f"0000-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # 仅 MM-DD
    m2 = re.search(r"(\d{1,2})[-/.](\d{1,2})", s)
    if m2:
        return f"0000-{int(m2.group(1)):02d}-{int(m2.group(2)):02d}"
    # 中文 6月29日
    m3 = re.search(r"(\d{1,2})月(\d{1,2})日", s)
    if m3:
        return f"0000-{int(m3.group(1)):02d}-{int(m3.group(2)):02d}"
    return None


def _normalize_vision(raw: dict) -> dict:
    """把视觉模型原始结果规范化为统一契约，并合成置信度。

    视觉模型读 金额/月日/商品名，并按特征词判平台（拼小圈→拼多多 等，
    见 _VISION_PROMPT）；年份：拼多多通常能读出完整年月日→合理年份保留，
    美团只有月日→待补，淘宝填 null→后期由 Excel 补全。
    """
    amount = raw.get("amount")
    platform = raw.get("platform")
    # 平台归一化：天猫 → 淘宝
    if platform and "天猫" in str(platform):
        platform = "淘宝"
    if amount is None:
        return {
            "status": "failed", "amount": None, "platform": None,
            "confidence": 0.0,
            "reason": f"视觉模型未解析出金额: {raw}",
        }
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {
            "status": "failed", "amount": None, "platform": None,
            "confidence": 0.0, "reason": f"金额非数字: {amount}",
        }
    # 金额合法 + 平台命中白名单 → 高置信；否则略低但仍成功
    conf = 1.0 if (platform in VISION_WHITELIST and 0 < amount < 1_000_000) else 0.9

    # 时间：年份不在此校验，保留月日、年标 0000 待后期补全
    ot = _parse_vision_time(raw.get("order_time"))

    return {
        "status": "success", "amount": amount, "platform": platform,
        "confidence": conf, "used_vision": True,
        "order_time": ot,
        "product_name": (raw.get("product_name") or None),
    }


def _vision_fail(reason: str) -> dict:
    return {
        "status": "failed", "amount": None, "platform": None,
        "confidence": 0.0, "reason": reason,
    }


class VisionBackend:
    """视觉识别后端：将截图直接发送给多模态大模型（OpenAI 兼容 chat/completions）。

    本地（Ollama Vision / LM Studio）与云端视觉 API 均走同一协议，
    仅端点与 API Key 不同。使用标准库 urllib，零额外依赖。
    """

    def __init__(self, endpoint: str, model: str, api_key: str = "",
                 timeout: int = 60, prompt: str | None = None):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.prompt = prompt  # None → 回退内置 _VISION_PROMPT

    def parse_image(self, image_path: str) -> dict:
        p = Path(image_path)
        if not p.exists():
            return _vision_fail("文件不存在")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png"}.get(
            p.suffix.lower().lstrip("."), "png"
        )
        try:
            # 压缩图片以控制 token 开销（手机截图通常 >5万 token）
            from PIL import Image as _PILImage
            _img = _PILImage.open(p)
            if _img.mode in ("RGBA", "P", "LA"):
                _img = _img.convert("RGB")
            _MAX_EDGE = 1024
            w, h = _img.size
            if max(w, h) > _MAX_EDGE:
                _ratio = _MAX_EDGE / max(w, h)
                _img = _img.resize((int(w * _ratio), int(h * _ratio)), _PILImage.LANCZOS)
            _buf = __import__("io").BytesIO()
            _img.save(_buf, format="JPEG", quality=85)
            b64 = base64.b64encode(_buf.getvalue()).decode("ascii")
        except Exception as e:
            return _vision_fail(f"读取/压缩图片失败: {e}")
        data_uri = f"data:image/{mime};base64,{b64}"

        url = _build_chat_url(self.endpoint)
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.prompt or _VISION_PROMPT},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": "请解析这张报销截图。"},
                ]},
            ],
            "temperature": 0.0,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return _vision_fail(f"视觉请求失败: {e}")

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return _normalize_vision(_extract_json(content))

    def _test_url(self) -> str:
        """根据端点生成测试连接 URL（与 OpenAICompatibleBackend 逻辑一致）。"""
        ep = self.endpoint
        if "://localhost" in ep or "://127.0.0.1" in ep:
            return f"{ep}/v1/models"
        if ep.endswith("/v1"):
            return f"{ep}/models"
        return f"{ep}/v1/models"

    def test_connection(self) -> tuple[bool, str]:
        if not self.endpoint:
            return False, "请先填写端点"
        if "://localhost" in self.endpoint or "://127.0.0.1" in self.endpoint:
            pass  # 本地服务不要求 key
        elif not self.api_key:
            return False, "请先填写 API Key"
        try:
            url = self._test_url()
            req = urllib.request.Request(url)
            if self.api_key:
                req.add_header("Authorization", f"Bearer {self.api_key}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.URLError as e:
            return False, f"连接失败: {e.reason}"
        except Exception as e:
            return False, f"连接失败: {e}"

        # 端点可达 ≠ 模型就绪：视觉模型必显式指定，校验其是否已加载
        if self.model:
            ids = _list_model_ids(body)
            if ids is not None and self.model not in ids:
                avail = ", ".join(ids) if ids else "无"
                return False, f"连接成功，但模型「{self.model}」未加载（可用: {avail}）"
        return True, "连接成功（模型已加载）" if self.model else "连接成功"


def build_vision_backend(kind: str, cfg: dict) -> VisionBackend | None:
    """根据视觉模式与配置构建后端。配置不足（缺端点/模型名）返回 None。

    kind: "本地视觉模型" | "云端视觉 API"
    """
    endpoint = (cfg.get("endpoint") or "").strip()
    model = (cfg.get("model") or "").strip()
    api_key = (cfg.get("api_key") or "").strip()
    if not endpoint or not model:
        return None
    if kind == "云端视觉 API" and not api_key:
        return None
    return VisionBackend(endpoint=endpoint, model=model, api_key=api_key,
                         prompt=cfg.get("prompt"))
