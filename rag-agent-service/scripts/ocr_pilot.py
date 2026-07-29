"""RapidOCR 试点脚本：拿扫描件的前几页跑 OCR，肉眼看识别质量。

用途：在建 OCR 全套基建之前，先验证 RapidOCR 对这份真实扫描件认得准不准。
认得准 → 放心建后台任务全量；认不准 → 再评估 PaddleOCR+PP-Structure 或本地多模态。

跑法：
    cd rag-agent-service && source .venv/bin/activate
    python scripts/ocr_pilot.py "<扫描件PDF路径>" [起始页] [页数]
默认跑第 1~5 页。
"""
import sys
import time
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python scripts/ocr_pilot.py <PDF路径> [起始页=0] [页数=5]")
        sys.exit(1)
    pdf_path = Path(sys.argv[1])
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    if not pdf_path.exists():
        print(f"文件不存在: {pdf_path}")
        sys.exit(1)

    import fitz  # pymupdf
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    doc = fitz.open(pdf_path)
    total = len(doc)
    print(f"PDF 共 {total} 页，试点第 {start+1}~{min(start+count, total)} 页\n")

    for i in range(start, min(start + count, total)):
        page = doc[i]
        # 渲染成图片（2倍缩放提清晰度，OCR 更准）。
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img_bytes = pix.tobytes("png")

        t0 = time.time()
        result, _ = ocr(img_bytes)
        elapsed = time.time() - t0

        texts = [line[1] for line in result] if result else []
        print(f"===== 第 {i+1} 页　识别 {len(texts)} 段　耗时 {elapsed:.1f}s =====")
        print("\n".join(texts[:40]))  # 每页最多打印前 40 段
        if len(texts) > 40:
            print(f"...（还有 {len(texts)-40} 段省略）")
        print()

    doc.close()
    print("试点完成。请肉眼核对：认字准不准、表格/版面丢了多少、速度能否接受。")


if __name__ == "__main__":
    main()
