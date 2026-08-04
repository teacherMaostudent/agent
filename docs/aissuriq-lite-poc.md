# AissuriQ 轻量合规 PoC

此 PoC 用一个 Python 进程验证以下假设：

1. 5000–8000 份企业质量文件能够通过适用性过滤和确定性规则完成大部分比对；
2. 只有部分覆盖、无法抽取或语义冲突的项目才成为 LLM 候选；
3. 在不启动 Control Plane、Context、Tool Gateway、Governance、Kafka、Temporal、
   OpenSearch 和 Java LLM Gateway 的情况下，也能验证核心业务闭环。

## 运行拓扑

PoC 最少只需一个服务：

```text
aissuriq-compliance-lite
  ├─ 文档和法规条款登记
  ├─ 外部法规规则比对
  ├─ 内部数值一致性比对
  ├─ SQLite 审查历史
  └─ 人工反馈记录
```

LLM 默认关闭。`UNCERTAIN` 项只标记为候选，不会静默判为通过。

## 启动

使用本地虚拟环境：

```powershell
cd rag-agent-service
& .\.venv\Scripts\uvicorn.exe apps.compliance_lite.main:app --host 0.0.0.0 --port 8000
```

或从仓库根目录启动最小容器：

```powershell
docker compose -f compose.lite.yaml up --build
```

打开 `http://localhost:8000/docs` 查看和调用 API。

## 最小验证流程

登记法规条款：

```http
POST /api/v1/lite/regulation-clauses/bulk
Content-Type: application/json

[
  {
    "regulation_id": "GMP-001",
    "regulation_version": "2026",
    "title": "偏差处理",
    "text": "偏差必须记录、调查并经质量部门批准。",
    "applicable_document_types": ["deviation_sop"],
    "required_concepts": {
      "record": ["偏差记录", "偏差报告"],
      "investigation": ["调查"],
      "approval": ["质量部门批准", "QA批准"]
    }
  }
]
```

批量登记企业文件：

```http
POST /api/v1/lite/documents/bulk
Content-Type: application/json

[
  {
    "document_id": "SOP-001-v3",
    "filename": "偏差管理规程-v3.txt",
    "document_type": "deviation_sop",
    "version": "3",
    "text": "建立偏差记录，完成调查后由质量部门批准。"
  }
]
```

运行外部法规比对：

```http
POST /api/v1/lite/reviews/external
Content-Type: application/json

{
  "document_ids": [],
  "clause_ids": [],
  "allow_llm": false
}
```

空 ID 列表表示使用全部已登记数据。响应中的关键指标为：

- `rule_resolution_rate`：规则直接解决的适用比对比例；
- `llm_candidate_rate`：需要进一步语义判断的比例；
- `llm_calls`：实际模型调用次数。

内部数值一致性示例：

```http
POST /api/v1/lite/reviews/internal
Content-Type: application/json

{
  "document_ids": [],
  "allow_llm": false,
  "rules": [
    {
      "rule_id": "storage-temperature",
      "title": "储存温度",
      "aliases": ["储存温度", "贮存温度"],
      "value_pattern": "(?:储存|贮存)温度为\\s*(?P<value>-?\\d+\\s*[-~～至]\\s*-?\\d+\\s*℃)",
      "severity": "HIGH"
    }
  ]
}
```

审查历史与人工意见：

```text
GET  /api/v1/lite/reviews
GET  /api/v1/lite/reviews/{job_id}
GET  /api/v1/lite/history
POST /api/v1/lite/feedback
```

## 8000 份模拟验证

自动化测试创建 8000 份文件并执行 8000 个适用比对：

```powershell
& .\rag-agent-service\.venv\Scripts\python.exe -m pytest \
  rag-agent-service\tests\test_lite_compliance.py -q
```

当前固定样本结果：

| 指标 | 结果 |
|---|---:|
| 文件/比对项 | 8000 |
| 规则通过 | 7600 |
| 规则失败 | 200 |
| LLM 候选 | 200 |
| 规则解决率 | 97.5% |
| 实际 LLM 调用 | 0 |

该数据验证的是软件漏斗和处理规模，不代表真实业务准确率。下一步必须使用人工标注的
200–500 份真实文件校准同义词、适用性和规则，并同时统计漏报率与人工推翻率。

## PoC 边界

为了轻量验证，当前版本有意不包含：

- PDF、Word、OCR 的批量上传编排（现有 ingestion 模块可在下一阶段接入）；
- 向量检索和 Rerank；
- 真正的 LLM 疑难仲裁；
- 多租户鉴权、WORM 审计、哈希链；
- 分布式任务、断点续跑和高可用。

因此该进程只适合本地或受控测试环境，不能直接作为受监管生产系统发布。
