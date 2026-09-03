"""RAG 检索与 Agent 子包。

分成四块,职责严格分开:

- `search_provider.py` —— 搜索:query 进,候选来源出
- `page_fetcher.py`    —— 抓取:URL 进,正文出
- `tools.py`           —— 把上面两个包装成模型能调的工具(B2)
- `agent.py`           —— ReAct 循环编排 + 终答综合(B2)

这一层属于 services,因此**不许 import fastapi**——检索能力应该能被
CLI 脚本、定时任务直接复用,而不是只能从 HTTP 请求里进来。
`tests/test_architecture.py` 会递归检查本子包的每个文件,包括 __init__.py。
"""
