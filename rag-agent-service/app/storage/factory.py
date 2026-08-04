from app.storage.local_storage import LocalFileStorage
from app.storage.s3_storage import S3FileStorage


def build_file_storage(settings):
    if settings.object_storage_backend == "s3":
        return S3FileStorage(settings)
    return LocalFileStorage(settings)
