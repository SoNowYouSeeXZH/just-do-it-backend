"""分层边界守卫测试。

前面几个模块的重构靠人工 review 保证边界,但人会忘。这些用例把
「哪一层不许 import 什么」变成可执行的断言——以后有人图省事
在 service 里写一句 SQL,CI 就会拦下来。

这类测试叫「架构测试」(architecture test / fitness function),
是让分层约束真正长期有效的关键。光靠文档和 code review 会腐化。
"""

import ast
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parent.parent / "app"


def _imported_modules(path: Path) -> set[str]:
    """解析一个 Python 文件的所有 import,返回模块名集合。

    用 ast 而不是正则:注释和字符串里的 "from fastapi" 不会被误判,
    这正是人工 grep 容易出假阳性的地方。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _python_files(layer: str) -> list[Path]:
    # rglob 递归收集:层下面的子包必须同样被守卫覆盖。
    # 此前用非递归 glob,子包文件会静默逃过分层检查——守卫测试"仍然通过"
    # 反而是虚假安全感。
    #
    # __init__.py 也必须纳入。包初始化文件是最容易被忽略的越界通道:
    # 在 app/models/__init__.py 里写一句 from app.services.x import y,
    # 依赖就跨层了,而按文件名排除会让全部用例照样通过。
    return sorted((_APP / layer).rglob("*.py"))


def _query_builder_offenders(path: Path) -> set[str]:
    """找出「这个文件在自己构造查询」的证据。

    两种写法都要抓:
    - from sqlmodel import select        → ast.ImportFrom
    - import sqlmodel; sqlmodel.select() → ast.Attribute(只查 import 过的别名)

    只允许 Session(类型标注需要),select/desc/func 这些查询构造器一出现,
    就说明这一层在写 SQL。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"select", "desc", "asc", "func", "text"}
    orm_roots = {"sqlmodel", "sqlalchemy"}
    offenders: set[str] = set()

    # 先收集 `import sqlmodel [as sm]` 引入的本地名字,用于识别属性调用形态。
    orm_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in orm_roots:
                    orm_aliases.add(alias.asname or alias.name.split(".")[0])

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in orm_roots:
            offenders.update(
                alias.name for alias in node.names if alias.name in forbidden
            )
        elif isinstance(node, ast.Attribute) and node.attr in forbidden:
            value = node.value
            if isinstance(value, ast.Name) and value.id in orm_aliases:
                offenders.add(f"{value.id}.{node.attr}")

    return offenders


@pytest.mark.parametrize("path", _python_files("services"), ids=lambda p: p.name)
def test_services_do_not_depend_on_fastapi(path: Path) -> None:
    """业务层不许知道 HTTP 的存在。

    一旦 import 了 fastapi,这层就无法被 CLI 脚本、定时任务复用,
    单元测试也得拖上整个 web 框架。
    """
    offenders = {m for m in _imported_modules(path) if m.split(".")[0] == "fastapi"}
    assert not offenders, f"{path.name} 不应依赖 fastapi: {offenders}"


@pytest.mark.parametrize("path", _python_files("services"), ids=lambda p: p.name)
def test_services_do_not_import_orm_query_api(path: Path) -> None:
    """业务层不许自己构造查询,数据访问必须走 repositories。

    允许 import sqlmodel.Session(类型标注需要),但不允许 select/desc/func
    这些查询构造器——它们出现就说明这一层在写 SQL。
    """
    offenders = _query_builder_offenders(path)
    assert not offenders, f"{path.name} 不应直接构造查询: {offenders}"


@pytest.mark.parametrize("path", _python_files("repositories"), ids=lambda p: p.name)
def test_repositories_stay_free_of_web_and_business(path: Path) -> None:
    """数据层不许依赖 fastapi,也不许依赖业务异常。

    Repository 只负责如实取数/写入。「查不到算不算错误」是业务判断,
    放到这一层会让它绑定某个具体用法。
    """
    modules = _imported_modules(path)
    offenders = {
        m
        for m in modules
        if m.split(".")[0] == "fastapi" or m == "app.core.exceptions"
    }
    assert not offenders, f"{path.name} 不应依赖 {offenders}"


@pytest.mark.parametrize("path", _python_files("api"), ids=lambda p: p.name)
def test_api_does_not_import_orm_query_api(path: Path) -> None:
    """接口层不许写查询。

    deps.py 是唯一例外——它是 HTTP 与业务的翻译层,但也不该有 SQL。
    """
    offenders = _query_builder_offenders(path)
    assert not offenders, f"{path.name} 不应直接构造查询: {offenders}"


@pytest.mark.parametrize("path", _python_files("api"), ids=lambda p: p.name)
def test_api_does_not_reach_past_services(path: Path) -> None:
    """接口层只能调 services,不许越过它直接摸 repositories 或 ORM 模型。

    这是「跳层」最常见的形态:某个接口只要一行查询,顺手 import 了
    repository,业务规则就散到 api 层去了。禁掉 models 是同一个道理——
    直接返回 ORM 对象会让表结构泄漏成 API 契约。
    """
    modules = _imported_modules(path)
    offenders = {
        m
        for m in modules
        if m.startswith("app.repositories") or m.startswith("app.models")
    }
    assert not offenders, f"{path.name} 不应跳过 services 直接依赖 {offenders}"


@pytest.mark.parametrize("path", _python_files("models"), ids=lambda p: p.name)
def test_models_stay_at_the_bottom(path: Path) -> None:
    """模型层是依赖树的底,不许反向依赖任何上层。

    模型一旦 import service,依赖就成环了:service 需要模型来查数据,
    模型又需要 service 来做判断,两边再也无法单独测试或复用。
    """
    modules = _imported_modules(path)
    offenders = {
        m
        for m in modules
        if m.split(".")[0] == "fastapi"
        or m.startswith("app.services")
        or m.startswith("app.repositories")
        or m.startswith("app.api")
        or m.startswith("app.core")
    }
    assert not offenders, f"{path.name} 不应依赖上层 {offenders}"


@pytest.mark.parametrize("path", _python_files("core"), ids=lambda p: p.name)
def test_core_does_not_depend_on_business_layers(path: Path) -> None:
    """基础设施层只提供能力,不许知道业务。

    core 是被所有层依赖的公共底座。它一旦反向 import services/api,
    「任何业务都能用」就不成立了——引入 core 会顺带拖进整个业务栈。

    这里**不禁** fastapi:core 里的 handlers.py / middleware.py 本身就是
    框架适配器(全局异常处理、请求 ID 中间件),它们的职责就是跟 web 框架
    打交道。禁掉的是「业务方向」的依赖,不是「框架方向」的。
    """
    modules = _imported_modules(path)
    offenders = {
        m
        for m in modules
        if m.startswith("app.services")
        or m.startswith("app.repositories")
        or m.startswith("app.api")
        or m.startswith("app.models")
    }
    assert not offenders, f"{path.name} 不应依赖 {offenders}"


@pytest.mark.parametrize("path", _python_files("schemas"), ids=lambda p: p.name)
def test_schemas_do_not_import_models_or_services(path: Path) -> None:
    """DTO 层必须独立于 ORM 模型和业务逻辑。

    如果 schema 里 import 了 model,「API 契约」和「表结构」就又绑在一起了,
    独立 DTO 的意义就没了。
    """
    modules = _imported_modules(path)
    offenders = {
        m
        for m in modules
        if m.startswith("app.models")
        or m.startswith("app.services")
        or m.startswith("app.repositories")
    }
    assert not offenders, f"{path.name} 不应依赖 {offenders}"


# ---------------------------------------------------------------------------
# 守卫自身的反向验证
#
# 上面的用例全是"断言没有违规",全绿有两种可能:真的没违规,或者检测器
# 根本抓不到。只有喂进故意违规的代码、看它确实被抓,才能区分这两种情况。
# 这是架构测试最容易缺的一环。
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "offender.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_guard_catches_from_import_query_builder(tmp_path: Path) -> None:
    path = _write(tmp_path, "from sqlmodel import Session, select\n")
    assert _query_builder_offenders(path) == {"select"}


def test_guard_catches_attribute_style_query_builder(tmp_path: Path) -> None:
    """`import sqlmodel` + `sqlmodel.select(...)` 是绕过 ImportFrom 检查的写法。"""
    path = _write(tmp_path, "import sqlmodel\n\nq = sqlmodel.select(1)\n")
    assert _query_builder_offenders(path) == {"sqlmodel.select"}


def test_guard_catches_aliased_orm_module(tmp_path: Path) -> None:
    path = _write(tmp_path, "import sqlalchemy as sa\n\nq = sa.func.count()\n")
    assert _query_builder_offenders(path) == {"sa.func"}


def test_guard_ignores_same_named_attribute_on_other_objects(tmp_path: Path) -> None:
    """不能只看属性名:业务对象上的 .text/.select 是正常代码,不该误报。"""
    path = _write(tmp_path, "def f(resp):\n    return resp.text\n")
    assert _query_builder_offenders(path) == set()


def test_guard_ignores_orm_query_words_in_comments(tmp_path: Path) -> None:
    """用 ast 而不是正则的收益:注释和字符串里的关键字不该被误判。"""
    path = _write(tmp_path, '# from sqlmodel import select\nS = "select * from t"\n')
    assert _query_builder_offenders(path) == set()


def test_guard_sees_package_init_files() -> None:
    """__init__.py 必须在守卫收集范围内,否则包初始化就是越界后门。"""
    collected = {p.name for p in _python_files("models")}
    assert "__init__.py" in collected