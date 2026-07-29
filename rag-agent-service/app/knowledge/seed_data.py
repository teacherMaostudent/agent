from app.domain.models import ChecklistItem, Regulation, RiskLevel


DEFAULT_REGULATIONS = [
    Regulation(
        regulation_id="GMP-150",
        standard="GMP",
        clause_no="150",
        title="文件与记录控制",
        content="企业应建立文件管理制度，文件应包含编号、版本、批准人、生效日期，并保持受控、可追溯。",
        metadata={"dimension": "3.1", "chapter": "文件管理"},
    ),
    Regulation(
        regulation_id="GMP-167",
        standard="GMP",
        clause_no="167",
        title="偏差处理",
        content="任何偏差均应记录、调查、评估影响并采取纠正预防措施，重大偏差应经过质量部门批准。",
        metadata={"dimension": "3.2", "chapter": "质量风险管理"},
    ),
    Regulation(
        regulation_id="ALCOA-001",
        standard="ALCOA+",
        clause_no="A1",
        title="数据可靠性原则",
        content="记录应满足可归因、清晰、同步、原始、准确，并具备完整、一致、持久和可获得的特征。",
        metadata={"dimension": "3.3", "chapter": "数据可靠性"},
    ),
]

DEFAULT_CHECKLIST = [
    ChecklistItem(
        requirement_id="REQ-DOC-001",
        module="文件控制",
        dimension="3.1",
        title="文件编号与版本控制",
        description="文件应包含编号、版本号、生效日期和批准人。",
        severity=RiskLevel.HIGH,
        required_fields=["编号", "版本", "生效日期", "批准"],
        regulation_refs=["GMP 第150条"],
    ),
    ChecklistItem(
        requirement_id="REQ-DEV-001",
        module="偏差与CAPA",
        dimension="3.2",
        title="偏差记录与调查",
        description="偏差应被记录、调查、影响评估，并形成CAPA。",
        severity=RiskLevel.HIGH,
        required_fields=["偏差", "调查", "影响评估", "CAPA"],
        regulation_refs=["GMP 第167条"],
    ),
    ChecklistItem(
        requirement_id="REQ-DI-001",
        module="数据可靠性",
        dimension="3.3",
        title="ALCOA+ 数据可靠性",
        description="关键记录应满足可归因、清晰、同步、原始、准确等数据可靠性要求。",
        severity=RiskLevel.MEDIUM,
        required_fields=["记录人", "时间", "原始记录", "复核"],
        regulation_refs=["ALCOA+"],
    ),
]

