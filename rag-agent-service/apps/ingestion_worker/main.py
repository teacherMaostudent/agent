import argparse

from app.core.logging import configure_logging
from app.ingestion.container import IngestionContainer
from app.ingestion.worker import IngestionWorker


def main() -> None:
    """启动本地轮询 Worker；生产环境应优先部署 Temporal Worker 入口。"""
    parser = argparse.ArgumentParser(description="Process asynchronous knowledge ingestion jobs.")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job.")
    args = parser.parse_args()
    configure_logging()
    worker = IngestionWorker(IngestionContainer())
    if args.once:
        worker.run_once()
    else:
        worker.run_forever()


if __name__ == "__main__":
    main()
