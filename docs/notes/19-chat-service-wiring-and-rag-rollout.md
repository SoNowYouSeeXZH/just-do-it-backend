# 19 聊天服务接线与灰度开关

> 对应 Track B B3：把已完成的 RAG Agent 接入现有聊天链路，同时保留可配置的回退路径。

## 一句话总结

`api/chat.py` 继续负责 HTTP 和认证，`services/chat.py` 负责按 `RAG_ENABLED` 分流：关闭时走原有直连大模型链路，开启时走 `services/rag/agent.py`。这样切换实现只需要修改环境变量，不需要回滚代码或修改数据库结构。

## 两条业务路径

```text
POST /api/chat
    ↓
api/chat.py
    ↓
services/chat.py
    ├─ RAG_ENABLED=false → ask_llm / ask_llm_stream
    └─ RAG_ENABLED=true  → rag_agent.answer / answer_stream
```

非流式请求统一返回 `{"reply": "..."}`。两条路径都会先尽力保存用户消息，模型成功后再尽力保存完整的 assistant 消息；保存失败只记录日志，不阻塞问答。

## 流式事件适配

Agent 不知道 HTTP 或 SSE，只产生结构化事件：

- `StageEvent` 表示检索进度，例如 `retrieving`、`answering`；服务层序列化为 `{"stage": ..., "detail": ...}`。
- `DeltaEvent` 表示最终答案增量；服务层序列化为旧协议的 `{"delta": ...}`。
- 服务层始终在流末尾发送 `data: [DONE]`。

只有 `DeltaEvent.text` 会加入 assistant 落库缓冲，检索阶段文字和 SSE 的 `data:` 包装不会进入数据库。模型在中途失败时发送脱敏 error 事件，并且不保存半截 assistant 回复。

## 开关和灰度边界

配置字段 `rag_enabled` 对应环境变量 `RAG_ENABLED`，默认值为 `true`。将部署环境设置为 `RAG_ENABLED=false` 可以立即回退到旧链路，适合故障止损和前后效果对比。

这是进程级配置开关，不是按用户或百分比的流量分桶。真实用户灰度需要额外的用户分组、稳定哈希或发布平台能力，不属于本阶段范围。

## 为什么接在 services/chat.py

`api/chat.py` 的职责是校验请求、认证用户和选择响应类型；RAG Agent 的职责是检索与综合；聊天服务正好位于两者之间，负责业务分流、SSE 序列化和消息持久化。这样既不污染 Agent 的可复用性，也不改变现有 HTTP 契约。

## 验证重点

- `RAG_ENABLED=false` 时，非流式和流式行为与旧链路一致。
- `RAG_ENABLED=true` 时，非流式使用 Agent，流式事件顺序为 stage、delta、`[DONE]`。
- stage/detail 不混入 assistant 落库内容。
- Agent 错误对外保持脱敏；流式错误后仍发送 `[DONE]`，且不保存半截回复。
- 关闭开关只改变服务分流，不涉及路由、数据库表或 Alembic 迁移。
