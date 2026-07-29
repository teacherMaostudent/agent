from fastapi import APIRouter

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


@router.post("/run")
def run_evaluation() -> dict:
    return {"status": "QUEUED", "message": "Golden Dataset/Ragas/Phoenix adapters are reserved for phase 2."}


@router.get("/{evaluation_id}")
def get_evaluation(evaluation_id: str) -> dict:
    return {"evaluation_id": evaluation_id, "status": "NOT_FOUND"}

