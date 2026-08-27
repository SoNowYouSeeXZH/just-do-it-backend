"""业务异常定义。

这是分层解耦最关键的一块,值得单独理解。

问题:业务层(services)如果直接抛 fastapi 的 HTTPException,
就等于把"HTTP 协议"这个知识塞进了业务层。后果:
- 业务代码无法脱离 HTTP 复用(定时任务、CLI 脚本里调用会很怪)
- 单元测试要 import fastapi 才能断言错误
- "用户名已存在"到底返回 409 还是 400,这个决策散落在各个 service 里

做法:业务层只抛"这是什么业务错误"(本文件的异常),
每个异常自带它对应的 HTTP 状态码,由 main.py 里注册的
全局异常处理器统一翻译成响应。业务代码里再也不用写
"出错了要返回什么结构"这种重复逻辑。

对照理解:
    业务层说的是    "用户名被占用了"
    全局处理器翻译成 "HTTP 409 + {code, message, data:null}"
"""


class AppError(Exception):
    """所有业务异常的基类。

    三个约定:
    - message 是"能直接给用户看"的文案,绝不要拼接 SQL、堆栈、内部路径
    - code 是给前端的机器可读标识,比数字状态码更能表达具体原因
    - status_code 是这类错误对应的 HTTP 语义,由子类声明一次,
      而不是每个路由函数自己拍脑袋决定
    """

    code: str = "APP_ERROR"
    status_code: int = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidCredentialsError(AppError):
    """用户名或密码错误。

    刻意不区分"用户不存在"和"密码不对":
    如果分开报错,攻击者就能靠"这个用户不存在"的提示批量枚举出
    系统里有哪些用户名,再针对这些账号做撞库。
    """

    code = "INVALID_CREDENTIALS"
    status_code = 401


class UsernameTakenError(AppError):
    """注册时用户名已被占用。"""

    code = "USERNAME_TAKEN"
    status_code = 409


class InvalidTokenError(AppError):
    """token 缺失、签名错误或已过期。

    前端拿到 401 + 这个 code,就知道该清掉本地 token 跳回登录页。
    """

    code = "INVALID_TOKEN"
    status_code = 401


class ConfigurationError(AppError):
    """服务端配置缺失,功能不可用(如没配 JWT 密钥)。

    用 503 而不是 500:503 表达的是"服务当前不可用,是环境/配置问题",
    运维看到这个码就知道该去检查部署配置,而不是翻业务代码找 bug。
    """

    code = "SERVICE_UNAVAILABLE"
    status_code = 503


class ResourceNotFoundError(AppError):
    """请求的资源不存在。

    为什么值得有一个通用类:原来每个只读接口都要写
    `raise HTTPException(404, detail="行业不存在")`,四个模块重复了八遍。
    现在业务层统一抛这个,404 的语义只在异常定义里出现一次。

    也用于"资源不属于当前用户"的情况:对外统一返回 404 而不是 403,
    避免泄露"这个 id 在系统里存在、只是不属于你"这一信息。
    """

    code = "NOT_FOUND"
    status_code = 404


class UpstreamServiceError(AppError):
    """依赖的外部服务失败(如大模型 API 超时、余额不足)。

    502 的语义是"我作为网关,后面那个服务出问题了"——
    和 500(我自己有 bug)要分开,否则排查时会去错方向。

    注意:message 会返回给用户,所以不要把上游返回的原始错误
    直接拼进来——那可能包含 API Key 片段、内部地址等信息。
    """

    code = "UPSTREAM_ERROR"
    status_code = 502


class TaskStateError(AppError):
    """任务状态转换不合法(如把已完成的任务再标记完成、给已归档任务改状态)。

    用 409 而不是 422:422 是"请求格式/参数本身不对",而这里是
    "请求格式没问题,但和当前资源状态冲突"——和"用户名已存在"同属一类,
    都是资源状态不允许这个操作。前端拿到 409 + TASK_STATE_CONFLICT
    就知道该刷新本地状态再重试,而不是当成表单校验错误。
    """

    code = "TASK_STATE_CONFLICT"
    status_code = 409


class IdempotencyConflictError(AppError):
    """同一个幂等键被用于不同的任务操作。"""

    code = "IDEMPOTENCY_KEY_CONFLICT"
    status_code = 409
