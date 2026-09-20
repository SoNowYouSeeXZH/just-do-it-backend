"""社区帖子相关表模型。

一期只做「帖 + 评论 + 点赞 + 审核」,不做关注/信息流/通知。

分区维度(2026-09 转型后):以地理位置为主轴的「附近广场」。
game_slug 仍必填(帖子归属某款游戏,现在是自由填写的话题标签);
lat/lng 是发帖时的坐标(可空,用户未授权定位时没有),用于「附近」距离过滤;
district 是模糊化后的行政区名(如「朝阳区」),展示只到这一级,精确坐标不外泄。
city 保留作为兼容字段。
"""

from datetime import datetime

from sqlmodel import Field, SQLModel, UniqueConstraint

# 审核状态。用 VARCHAR 而不是 ENUM:加状态不用改表结构。
POST_STATUS_PENDING = "pending"
POST_STATUS_PUBLISHED = "published"
POST_STATUS_REJECTED = "rejected"


class Post(SQLModel, table=True):
    __tablename__ = "posts"  # pyright: ignore[reportAssignmentType]

    id: int | None = Field(default=None, primary_key=True)
    # 作者。外键让数据库拒绝不存在的 user_id;index 是因为「我的帖子」按它过滤。
    user_id: int = Field(foreign_key="users.id", index=True)
    # 游戏分区 slug。转型后是自由填写(存在则收录、不存在则新增),不再限于 ys/sr/zzz。
    game_slug: str = Field(max_length=32, index=True)
    # 城市是二级维度,允许为空。
    city: str | None = Field(default=None, max_length=32, index=True)
    # 发帖坐标:用于「附近」距离过滤。用户未授权定位时为空——这类帖子不参与
    # 距离筛选,只在「不限地区」下可见。加 index 是因为附近查询用它做包围盒过滤。
    lat: float | None = Field(default=None, index=True)
    lng: float | None = Field(default=None, index=True)
    # 模糊化后的行政区名(如「朝阳区」)。展示只到这一级,精确坐标从不下发给前端。
    district: str | None = Field(default=None, max_length=32)
    title: str = Field(max_length=120)
    content: str
    # 审核状态:列表默认只查 published,审核未通过的帖子只有作者和管理端能看到。
    status: str = Field(default=POST_STATUS_PENDING, max_length=16, index=True)
    # 计数列。每次列表查询都要展示点赞数,实时 count(*) 在列表页是 N+1 的放大器,
    # 所以在写入点赞时同步维护这一列。
    like_count: int = Field(default=0)
    comment_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.now, index=True)


class PostComment(SQLModel, table=True):
    __tablename__ = "post_comments"  # pyright: ignore[reportAssignmentType]

    id: int | None = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="posts.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    content: str = Field(max_length=1000)
    created_at: datetime = Field(default_factory=datetime.now, index=True)


class PostLike(SQLModel, table=True):
    """点赞是「同一用户对同一帖子最多一次」,所以用联合唯一约束表达幂等。

    把去重交给数据库而不是先查再插:并发双击时应用层的「查不到就插」
    会插进两条,唯一约束才能真正兜住。
    """

    __tablename__ = "post_likes"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (UniqueConstraint("post_id", "user_id", name="uq_post_likes_post_user"),)

    id: int | None = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="posts.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=datetime.now)
