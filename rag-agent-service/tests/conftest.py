"""测试环境隔离:强制离线、确定性。

无论本地 .env 怎么配(可能设了 qwen + 真密钥用于建库),测试一律用
hash embedding、不开 LLM、清空密钥，保证:
- 不调通义/网关真 API(不花钱、不依赖网络、CI 无密钥也能跑)
- 结果确定可复现

必须在 import app.* 之前设好环境变量(container 是模块级单例，import 即初始化)。
"""

import os

os.environ["RAG_EMBEDDING_PROVIDER"] = "hash"
os.environ["RAG_LLM_ENABLED"] = "false"
os.environ["DASHSCOPE_API_KEY"] = ""
os.environ["RAG_DASHSCOPE_API_KEY"] = ""
