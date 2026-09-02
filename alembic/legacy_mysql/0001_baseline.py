"""数据库当前结构基线。

这是给已有环境使用的空基线：已经通过历史 create_all()/SQL 脚本建好的数据库，
先人工确认结构与当前代码一致，再执行 `alembic stamp 0001_baseline`。

为什么 upgrade 是空操作：没有历史版本表时，不能假设线上已经是某一个模型版本，
也不能为了“自动迁移”删除或覆盖现有数据。基线只建立版本管理起点；之后每次结构
变化都必须新增一个可审查、可回滚的 revision。

新环境不能只执行这个空基线来建表。新环境初始化方案需要在确认现有生产数据
基线后单独编写并审查，不能用空基线伪装成完整初始化迁移。
"""

from typing import Sequence, Union

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """建立版本基线，不改变业务表。"""


def downgrade() -> None:
    """基线没有可安全自动删除的对象。"""
