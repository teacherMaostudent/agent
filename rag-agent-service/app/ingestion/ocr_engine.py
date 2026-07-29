"""本地 OCR 引擎(RapidOCR)。见项目进展报告 OCR 一节。

设计要点:
- **纯本地识别**,不调任何第三方 API(那份 468 页扫描件是保密材料,不得外传)。
  RapidOCR 基于 ONNX,模型权重内置在包里,离线可跑。
- 统一接口 recognize_image(image_bytes) -> str:日后想换 PaddleOCR/本地多模态,
  只改这个文件,上层(parsers/后台任务)不动。
- 引擎单例 + 懒加载:首次用时才初始化(初始化有几秒开销),之后复用。
- 已知限制:RapidOCR 只出"文字+坐标",不重建表格结构——表格页文字准但行列会乱序。
  SOP 正文(条款段落)识别质量优秀,表格页作为已知限制,由调用方在报告中标注。
"""
from __future__ import annotations

import threading


class OcrEngine:
    _instance: OcrEngine | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        # 懒加载:构造时不初始化 RapidOCR(它有几秒开销),首次 recognize 时才建。
        self._ocr = None
        self._init_lock = threading.Lock()

    @classmethod
    def instance(cls) -> OcrEngine:
        """进程内单例:模型只加载一次,多次审查复用。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _ensure_loaded(self):
        if self._ocr is None:
            with self._init_lock:
                if self._ocr is None:
                    from rapidocr_onnxruntime import RapidOCR
                    self._ocr = RapidOCR()
        return self._ocr

    def recognize_image(self, image) -> str:
        """识别一张图片,返回按识别顺序拼接的纯文字。

        image: 图片路径(str)或 numpy 数组(RapidOCR 两者都接受)。
        返回空串表示这页没识别到文字(可能是空白页或纯图)。
        """
        ocr = self._ensure_loaded()
        result, _elapsed = ocr(image)
        if not result:
            return ""
        # result: [[box, text, score], ...] —— 只取 text,按识别顺序拼接。
        return "\n".join(item[1] for item in result if len(item) >= 2 and item[1])
