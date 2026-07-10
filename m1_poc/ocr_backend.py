"""OCR 抽象层（模式 C 实现）。

OcrBackend 接口统一封装，当前实现为 RapidOCR。
未来模式 A/B 实现同一接口即可无缝替换，上层无感知。
"""
from pathlib import Path


class OcrBackend:
    """模式 C：RapidOCR 纯文本提取。"""

    def __init__(self, use_gpu: bool = False):
        from rapidocr_onnxruntime import RapidOCR

        self.engine = RapidOCR(use_angle_cls=True, use_gpu=use_gpu)

    def recognize(self, image_path) -> str:
        """返回图片 OCR 拼接纯文本（按行）。"""
        result, _ = self.engine(str(image_path))
        if not result:
            return ""
        # result: [(box, text, score), ...] 取 item[1] 拼文本
        full_text = "\n".join(
            i[1].strip() for i in result if i and len(i) >= 2 and i[1]
        )
        return full_text


def ocr_image_to_text(image_path, backend: OcrBackend | None = None) -> str:
    if backend is None:
        backend = OcrBackend()
    return backend.recognize(image_path)
