from app.storage.local_storage import LocalFileStorage
from app.storage.s3_storage import S3FileStorage


def build_file_storage(settings):
    """按配置选择对象存储实现，令 API/摄取流程不依赖具体存储介质。"""
    if settings.object_storage_backend == "s3":
        return S3FileStorage(settings)
    return LocalFileStorage(settings)
