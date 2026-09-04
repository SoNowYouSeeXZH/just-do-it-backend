# 16. MySQL → PostgreSQL 全量迁移实战

> 2026-09 完成。动机:游戏攻略 RAG 模块需要 pgvector 向量能力,借机把整个后端
> 从 MySQL 切到 PostgreSQL,一次到位避免双库并存。这篇按「面试怎么讲」的视角
> 整理决策与踩坑。
>
> **代码已不在仓库里。** 产品转型为游戏攻略/社区后，线上那套旧数据确认不再保留，
> 所以迁移脚本 `app/migrate_mysql_to_pg.py`、`alembic/legacy_mysql/` 归档和
> `docs/migrations/*.sql` 都已删除（2026-09-03）。本篇保留的是**做法与踩坑**，
> 需要看具体实现用 git 历史。

## 一句话故事

「我把一个已上线项目的存储引擎从 MySQL 全量迁移到了 PostgreSQL + pgvector,
迁移是脚本化、原子、可对账的,业务代码几乎零改动。」

## 为什么值得讲:三个层次的决策

### 1. 驱动层——为什么选 psycopg3 同步驱动

- 全部业务层是同步 `Session`(SQLAlchemy 2.x),换**驱动**而不是换**编程模型**,
  repositories/services 一行不改;
- FastAPI 官方全栈模板同款选型(它甚至从 asyncpg 退回了同步 + psycopg3),
  说明这不是保守,是 SQLModel 生态下的主流答案;
- `requirements` 增删:PyMySQL/cryptography 出,psycopg[binary] 入。

### 2. 结构层——Alembic 重建基线,而不是逐条翻译旧迁移

MySQL 时代的历史包袱:0001 基线是空的(只 stamp),表结构长期靠
`create_all()` 生成,「完整 initial migration 缺失」是挂在文档里的 TODO。

迁移决策:**PG 谱系重新开基线**(`down_revision=None`),autogenerate 生成
完整建表 revision + `CREATE EXTENSION vector`;旧 revision 归档到
`alembic/legacy_mysql/`。理由:

- PG 是全新空库,结构由基线生成、数据由脚本搬运,两者职责分离;
- 顺手消灭了历史 TODO:从此表结构变更必须走 Alembic;
- 坑:旧 revision 文件必须**移出** `versions/` 目录——两个 `down_revision=None`
  的根会形成 multiple heads,`upgrade head` 直接报错。

### 3. 数据层——迁移脚本的五道安全措施

写 `app/migrate_mysql_to_pg.py` 时把「数据迁移最容易出事的地方」各设一道闸:

| 措施 | 防的事故 |
|---|---|
| 目标库非空直接拒绝执行 | 执行顺序错了/重复执行,把旧数据搅进半迁移状态 |
| 单事务写入,失败整体回滚 | 迁到一半报错,PG 留下脏的半截数据 |
| 逐表对账报告(期望行数按过滤规则推导) | 「迁完了」但实际少迁/多迁,没人发现 |
| 不变量断言(保留路径的 quizJobId ⊆ 保留职业) | 迁出悬空引用,用户点击 404 |
| 序列重置 setval | **PG 经典坑**,见下 |

## 踩坑实录(比成功更值得讲)

### 坑 1:PG 序列不会随显式插入推进

PG 自增列靠 sequence 发号,但**带 id 显式插入的行不会推进 sequence**。
迁移完数据一切正常,应用第一次 INSERT 就主键冲突——因为 sequence 还停在 1。

```sql
SELECT setval(pg_get_serial_sequence('users', 'id'),
              GREATEST((SELECT COALESCE(MAX(id), 0) FROM users), 0) + 1, false);
```

`false` 表示「这个值还没被使用」,下一次 `nextval` 恰好返回 `MAX(id)+1`;
空表时返回 1。四个整型主键表都要做;字符串主键的表没有 sequence。

### 坑 2:detached 对象被当成 UPDATE(StaleDataError)

从源 session `exec().all()` 拿到的对象带着持久化身份(有主键、persistent 状态)。
直接 `dst.add()` 会让目标 session 把它当「已存在的行」发 **UPDATE** 而不是
INSERT——目标表是空的,UPDATE 匹配 0 行,报
`StaleDataError: expected to update N row(s); 0 were matched`。

修法:复制成全新实例再入库(`type(row).model_validate(row.model_dump())`),
新实例是 pending 状态,正确走 INSERT。教训:**ORM 对象跨 session 不是值对象,
身份(identity)会跟着走**。

### 坑 3:源表结构落后于代码(schema drift)

迁移时发现源库 `chat_messages` 表**没有 `user_id` 列**——模型加了鉴权字段,
但 `create_all()` 只建新表、从不 ALTER 已有表,线上表结构悄悄落后于代码
(文档里记过这个 TODO,这次踩实了)。

处理:不硬塞假归属,跳过这 11 行匿名历史数据,在对账报告里如实交代。
教训:**create_all 不适合长期当迁移工具**;「代码和库不一致」要靠 schema
对比(inspect)在迁移时主动发现,而不是等 ORM 报错。

### 坑 4:错误判定不能依赖驱动文案

判重逻辑原来匹配 MySQL 的 "duplicate entry" 错误串,换 PG 后失灵。
重写为按结构化信号分级判定:psycopg3 的 `sqlstate == "23505"` → pymysql 的
`args[0] == 1062` → SQLite 的文案兜底,配 11 组三驱动参数化单测。

### 坑 5:PG 18 官方镜像的数据卷路径变了

PG 18 起数据卷必须挂 `/var/lib/postgresql`(内部按版本分子目录),
挂老的 `/var/lib/postgresql/data` 容器直接拒绝启动。

### 小坑:sqlmodel 的 select 与 sqlalchemy 的 select 不是一回事

`Session.exec()` 配 sqlalchemy 原生 `select` 返回的是 Row 元组,
`row.id` 直接 AttributeError;必须用 `from sqlmodel import select`。

## 没做但值得被追问的点(准备好答案)

- **为什么不上 pgvector 的向量表?** 一期只装扩展:向量维度是 DDL 常量,
  取决于二期 embedding 模型选型,现在定是拍脑袋;YAGNI。
- **线上怎么办?** 本地先行,云端旧 MySQL 栈继续运行(容器不自动拉新配置)。
  旧数据已确认不保留,所以后续线上切换是独立的全新部署窗口:完成审批后停止旧栈,
  清理旧卷,按 PostgreSQL compose 启动新环境,执行 Alembic 建表和 seed/题库导入。
  本次不连接线上环境,不执行停栈、清卷或数据迁移;回滚策略属于该部署窗口的单独评审内容。
- **为什么不用数据库名固定的双写/灰度方案?** 个人项目数据量小(千行级),
  停机迁移 + 对账的收益成本比远高于双写复杂度;但方案选型上能讲清
  「什么时候该上双写」(用户量大、不能停服、需要回滚窗口)。

## 复盘

- 迁移本身不难,难的是**把每类事故都提前设一道闸**——非空拒绝、原子事务、
  对账报告、不变量断言,这些比「能跑通」重要;
- ORM 帮我们屏蔽了 95% 的 SQL 方言差异,剩下的 5%(序列、错误码、函数名)
  恰恰是最容易漏测的;
- 一次性脚本用「真实执行 + 对账」验证,比硬凑单测更诚实。
