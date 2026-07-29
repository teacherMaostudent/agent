"""导出接口:把 Markdown(审查报告 / 生成初稿)转成 Word(.docx) 下载。

前端把已拿到的 Markdown 文本 POST 过来,后端转成排好版的 .docx 返回,
用户打开就是原生 Word 格式,看不到 #、**、| 等符号。
"""
import urllib.parse

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.domain.schemas import ExportDocxRequest
from app.report.docx_exporter import markdown_to_docx_bytes

router = APIRouter(prefix="/export", tags=["export"])

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@router.post("/docx")
def export_docx(request: ExportDocxRequest) -> Response:
    if not request.markdown.strip():
        raise HTTPException(status_code=400, detail="markdown 内容为空,无法导出")
    data = markdown_to_docx_bytes(request.markdown, title=request.title, highlight=request.highlight)
    # 文件名含中文时用 RFC 5987 编码,避免响应头非 ASCII 报错。
    filename = (request.filename or "document").rstrip(".docx") + ".docx"
    quoted = urllib.parse.quote(filename)
    return Response(
        content=data,
        media_type=_DOCX_MIME,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )
