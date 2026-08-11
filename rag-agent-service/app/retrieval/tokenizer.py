import re

TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """以可预测的中英文 token 规则规范化文本，供 BM25 与哈希回退共享。"""
    return [token.lower() for token in TOKEN_RE.findall(text)]
