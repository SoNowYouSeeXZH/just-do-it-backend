"""
应用配置模块。

作用:把所有"可变的设置项"(端口、大模型 API Key、允许跨域的前端地址等)
集中在一处管理,并且优先从环境变量 / .env 文件读取。

为什么这样做:
- 密钥这类敏感信息不应写死在代码里(会泄露),放环境变量最安全;
- 部署到不同环境(本地 / 阿里云服务器)时,只改环境变量即可,不用改代码。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # model_config 告诉 pydantic:去读取同目录下的 .env 文件
    # extra="ignore" 表示 .env 里有多余的变量也不报错
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 应用名称,会显示在自动生成的 API 文档标题上
    app_name: str = "Personal AI Backend"

    # 运行环境:development / production。
    # 生产环境会关掉 /docs 和 /openapi.json,避免接口结构对外暴露。
    environment: str = "development"

    # 可观测性配置。慢请求阈值用环境变量控制,不同环境不改代码。
    log_level: str = "INFO"
    slow_request_ms: int = 500

    # 开发环境可自动建表；生产环境必须使用 Alembic，避免应用启动时偷偷改表。
    auto_create_tables: bool = True

    # ===== 缓存(Redis)配置 =====
    # cache_enabled=False 时所有缓存调用直接落库,方便本地排查"是不是缓存的问题"。
    # 测试环境默认关闭(见 tests/conftest.py),让断言反映真实的数据库行为。
    cache_enabled: bool = True
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0
    # Redis 没配密码时留空。生产环境即使只在内网,也建议配上——
    # 内网不等于可信,一旦有别的容器被攻破,无密码的 Redis 就是敞开的。
    redis_password: str = ""
    # 缓存基础存活时间(秒)。内容类数据(职业/行业/题库)变更极少,
    # 5 分钟足够挡住绝大部分重复查询,同时保证运营改了内容不会长时间看不到。
    cache_ttl_seconds: int = 300
    # 空结果的 TTL 要短得多:它只是用来挡穿透攻击的,不是真正的数据。
    cache_null_ttl_seconds: int = 30
    # 连接/读写超时(秒)。必须设小值:缓存是为了让请求更快,
    # 如果 Redis 卡住而这里无限等待,加缓存反而让接口比不加更慢。
    redis_timeout_seconds: float = 0.5

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    # 允许访问本后端的前端地址(CORS 跨域白名单)。
    # 前端和后端端口不同就属于"跨域",浏览器默认会拦截,必须在这里放行。
    # 这里默认放行本地常见的前端开发端口(Vite 5173 / CRA 3000)。
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000"]

    # ===== 大模型相关配置 =====
    # 下面这些等号右边是"默认值",启动时若 .env 里有同名变量(大写),会自动覆盖。
    # 大模型 API Key,务必通过环境变量传入,不要写死
    llm_api_key: str = ""
    # 大模型服务地址(用兼容 OpenAI 协议的接口,如通义千问/DeepSeek)
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # 使用的模型名称
    llm_model: str = "qwen-plus"

    # 题库批量写入接口的入站鉴权 Key,与大模型出站 Key 分开管理
    admin_api_key: str = ""

    # ===== 登录鉴权(JWT)相关配置 =====
    # 签发/校验 token 用的密钥,务必通过环境变量传入随机高强度字符串,不要写死。
    jwt_secret_key: str = ""
    # 签名算法,HS256 是最常见的选择,和密钥配套使用即可,不需要额外配置。
    jwt_algorithm: str = "HS256"
    # token 有效期(分钟),过期后前端需要重新登录。
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 天

    # ===== PostgreSQL 数据库配置 =====
    # 用 Docker 时 host 由 compose 的 environment 覆盖为服务名 "postgres";
    # 本地直跑 uvicorn / alembic / 迁移脚本时用 "127.0.0.1"(见 .env)。
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    # compose 的 postgres 服务用这组账号密码初始化超级用户,
    # 本地单实例下应用直接用它连库;上生产再按最小权限拆分账号。
    postgres_user: str = "justdoit_app"
    postgres_password: str = ""  # 必须由 .env 提供,别写死
    postgres_database: str = "personal_ai"

    @property
    def database_url(self) -> str:
        """拼出 SQLAlchemy 需要的连接串。

        格式:postgresql+psycopg://用户:密码@主机:端口/库名
        - postgresql+psycopg 表示 "PostgreSQL 协议 + psycopg 3 驱动"
        - PG 建库默认 UTF-8,不需要 MySQL 时代的 charset=utf8mb4 参数
        """
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )


# 创建一个全局唯一的配置实例,其他文件直接 from app.config import settings 使用
settings = Settings()
