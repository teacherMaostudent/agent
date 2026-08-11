import logging


def configure_logging() -> None:
    """配置统一文本日志格式；业务日志不得在此处写入敏感请求正文。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
