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

    # ===== MySQL 数据库配置 =====
    # 用 Docker 时 host 是 compose 里的服务名 "mysql";
    # 本地不走容器直接跑 uvicorn 时,改成 "127.0.0.1" 即可。
    mysql_host: str = "mysql"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""  # 必须由 .env 提供,别写死
    mysql_database: str = "personal_ai"

    @property
    def database_url(self) -> str:
        """拼出 SQLAlchemy 需要的连接串。

        格式:mysql+pymysql://用户:密码@主机:端口/库名?charset=utf8mb4
        - mysql+pymysql 表示 "MySQL 协议 + PyMySQL 驱动"
        - charset=utf8mb4 保证能存 emoji / 罕见汉字,别偷懒省掉
        """
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            f"?charset=utf8mb4"
        )


# 创建一个全局唯一的配置实例,其他文件直接 from app.config import settings 使用
settings = Settings()
