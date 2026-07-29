# 条款覆盖率 Golden 集

这里存放经过脱敏并由 GMP 专家确认的单文档覆盖率标注。每个案例至少包含企业文件正文、`document_type`、清单版本，以及逐条 `requirement_id -> expected_status`。

允许状态为 `COVERED`、`PARTIAL`、`MISSING`、`NOT_APPLICABLE`、`UNCERTAIN`。标注还应保存支持结论的企业原文，后续用于评价证据引用准确率。

不要使用模型生成结果反向充当 golden。真实案例进入仓库前必须脱敏，并由至少一名 GMP 领域人员确认。`case.example.json` 只定义格式，不作为质量基线。
