-- 迭代 3：创建任务表
--
-- 生产执行前先确认 users.id 的实际类型和字符集，并先备份数据库。
-- 当前服务器 users.id 为 INT，因此 user_id 也使用 INT，确保外键类型匹配。

START TRANSACTION;

CREATE TABLE IF NOT EXISTS tasks (
    id INT NOT NULL AUTO_INCREMENT,
    user_id INT NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    due_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    deleted_at DATETIME NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_tasks_user_id FOREIGN KEY (user_id) REFERENCES users(id),
    INDEX ix_tasks_user_id (user_id),
    INDEX ix_tasks_status (status),
    INDEX ix_tasks_created_at (created_at),
    INDEX ix_tasks_deleted_at (deleted_at)
);

COMMIT;
