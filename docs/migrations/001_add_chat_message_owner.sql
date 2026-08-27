-- 迭代 2：给聊天消息补充资源所有者（已应用到生产）
--
-- 本次只新增允许 NULL 的 owner 字段，不猜测历史消息属于哪个用户。
-- 旧消息保持 user_id=NULL，因此不会出现在按用户授权的历史查询中。
-- 新版本应用写入的消息必须带真实 user_id。
--
-- 执行前检查：
-- 1. users.id 的实际类型与 user_id 一致
-- 2. 先备份 chat_messages
-- 3. 确认历史消息归属策略

ALTER TABLE chat_messages
    ADD COLUMN user_id INT NULL,
    ADD INDEX ix_chat_messages_user_id (user_id),
    ADD CONSTRAINT fk_chat_messages_user_id
        FOREIGN KEY (user_id) REFERENCES users(id);

-- 后续如果业务确认历史消息可以归属到某个用户，才执行第二阶段：
-- 1. UPDATE chat_messages SET user_id = <legacy_user_id> WHERE user_id IS NULL;
-- 2. 检查 SELECT COUNT(*) FROM chat_messages WHERE user_id IS NULL;
-- 3. 确认结果为 0 后，再将 user_id 改为 NOT NULL。
-- 不能把 <legacy_user_id> 替换成猜测值。
