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

    # ===== RAG / Agent 配置 =====
    # rag_enabled 既是功能开关也是回滚开关:置 False 时「问问AI」走原来的
    # 直连 LLM 路径,不做任何检索。出问题时改一个环境变量就能退回去,
    # 不需要回滚代码——这比"改代码删功能"安全得多,也是 A/B 对比的入口。
    rag_enabled: bool = True
    # 检索循环的硬上限。模型可能反复调工具却始终不给结论,
    # 没有上限就是一个会烧钱、会卡住请求的死循环。
    rag_max_iterations: int = 4
    rag_search_max_results: int = 5
    # 抓回来的正文截断长度。不截断的话一个长攻略页就能顶满上下文窗口,
    # 后面的检索结果反而被挤掉。
    rag_fetch_max_chars: int = 8000
    # 抓单个页面的超时(秒)。宁可这一步失败让模型换个来源,
    # 也不要让用户对着转圈等一个卡死的外部站点。
    rag_fetch_timeout_seconds: float = 10.0
    # 检索结果缓存时长。攻略类问题短时间内重复问的概率高,
    # 而搜索是抓取型来源、有频控风险,缓存既省时间也是护栏。
    rag_query_cache_ttl_seconds: int = 600
    # 搜索 Provider:
    # - "wiki"(默认):MediaWiki API 检索游戏 wiki。见 rag/wiki_provider.py
    #   开头那段说明——DDG 实测在当前网络下会被限流且不恢复,不能当主路径。
    # - "ddg":通用网页搜索,保留为备选实现。
    # 换 Provider 只改这一行,agent 和 tools 零改动(SearchProvider Protocol 的用处)。
    search_provider: str = "wiki"
    search_api_key: str = ""  # 仅 key 型 Provider 使用,DDG / wiki 都留空

    # ----- wiki Provider -----
    # 任何 MediaWiki 站群都行,换站只改 base_url + sites。
    wiki_base_url: str = "https://wiki.biligame.com"
    # 站点白名单,逗号分隔,一个 slug 对应一个游戏 wiki。
    # 实测可达:ys(原神)、sr(星穹铁道)、zzz(绝区零)。
    # 刻意做成白名单而不是"全网搜":宁可"没配的游戏答不了",
    # 也不要"什么都能搜但一半时间失败"——后者更难排查、体验更差。
    wiki_sites: str = "ys,sr,zzz"
    wiki_search_limit_per_site: int = 5
    wiki_request_timeout_seconds: float = 8.0
    # biligame 有 WAF,请求密了会返回 567。退避重试一次,间隔别太短。
    wiki_retry_delay_seconds: float = 1.0

    # ----- ddg Provider(备选)-----
    # DDG 的区域参数。cn-zh 让中文攻略站排在前面。
    search_region: str = "cn-zh"
    # ddgs 的后端引擎,逗号分隔。
    #
    # **不要用默认的 "auto"**,也不要把多个引擎写在一起。实测原因:
    # auto 会打乱顺序全试一遍,一次搜索耗 30~50 秒;而 ddgs 内部用
    # `return_when=FIRST_EXCEPTION` 并发批量跑,一个秒失败的引擎会把
    # 另一个正在返回结果的引擎一起带走——多后端不是冗余,是互相拖累。
    #
    # 实测(开发网络):brave 约 1.7~6.7s 可用但很快被限流;
    # duckduckgo、startpage 秒失败;google、mojeek、bing 超时。
    search_backends: str = "brave"
    # 单个后端引擎的 HTTP 超时。设小值让不可达的引擎快速失败。
    search_request_timeout_seconds: float = 8.0
    # 一次搜索的总预算。
    search_timeout_seconds: float = 20.0

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
