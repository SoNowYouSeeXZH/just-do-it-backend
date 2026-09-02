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
    # rglob 递归收集:层下面的子包(如 services/rag/)必须同样被守卫覆盖。
    # 此前用非递归 glob,子包文件会静默逃过分层检查——守卫测试"仍然通过"
    # 反而是虚假安全感。
    return sorted(p for p in (_APP / layer).rglob("*.py") if p.name != "__init__.py")


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
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"select", "desc", "asc", "func", "text"}
    offenders: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "sqlmodel",
            "sqlalchemy",
        }:
            offenders.update(
                alias.name for alias in node.names if alias.name in forbidden
            )
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
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"select", "desc", "asc", "func", "text"}
    offenders: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "sqlmodel",
            "sqlalchemy",
        }:
            offenders.update(
                alias.name for alias in node.names if alias.name in forbidden
            )
    assert not offenders, f"{path.name} 不应直接构造查询: {offenders}"


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
