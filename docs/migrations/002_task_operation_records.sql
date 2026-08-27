-- 迭代 4：任务状态变更记录与幂等键
--
-- 前提：迭代 3 已经完成 tasks 表；如果生产库尚未部署 tasks，
-- 应先执行任务表迁移，再执行本文件。
-- 执行前请备份数据库，并在低流量窗口执行。

START TRANSACTION;

CREATE TABLE IF NOT EXISTS task_operation_records (
    id INT NOT NULL AUTO_INCREMENT,
    task_id INT NOT NULL,
    user_id INT NOT NULL,
    action VARCHAR(16) NOT NULL,
    from_status VARCHAR(16) NOT NULL,
    to_status VARCHAR(16) NOT NULL,
    idempotency_key VARCHAR(128) NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_task_operation_task
        FOREIGN KEY (task_id) REFERENCES tasks(id),
    CONSTRAINT fk_task_operation_user
        FOREIGN KEY (user_id) REFERENCES users(id),
    CONSTRAINT uq_task_operation_user_key
        UNIQUE (user_id, idempotency_key),
    INDEX ix_task_operation_task_id (task_id),
    INDEX ix_task_operation_user_id (user_id),
    INDEX ix_task_operation_idempotency_key (idempotency_key),
    INDEX ix_task_operation_created_at (created_at)
);

COMMIT;

-- 回滚（确认没有依赖操作记录后执行）：
-- DROP TABLE task_operation_records;
