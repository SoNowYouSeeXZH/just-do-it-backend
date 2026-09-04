"""
数据库种子脚本 —— 灌入职业、行业知识库、学习路径数据。

用法:
    python -m app.seed            # 表里已有数据则跳过,防止重复灌入
    python -m app.seed --reset    # 清空所有业务表后重新灌入

设计说明(2026-09 题库域收敛后):
- 职业只保留 frontend / backend 两个——只有它们有开源题库与官方学习文档,
  其余 9 个职业已在 2026-09 的题库域收敛中删除(收敛规则见
  .comate/specs/game-guide-rag-agent/doc.md 的 Track C)。
- questions 表不再由 seed 灌入:历史上的种子题混着 LLM 凭空生成的题,
  无字段级区分,已整表换血。题库统一由开源导入管道供给:
    python -m app.import_question_banks
- INDUSTRIES:数据来源前端 data/industries.ts,字段逐字迁移
  (keyPoints → key_points 命名对齐后端 snake_case 习惯)。
- CAREER_PATHS / CURRENT_JOBS:数据来源前端 data/careers.ts,字段逐字迁移,
  steps/recommendations 整体搬进 JSON 列(quizJobId/targetId 字段名维持
  前端驼峰,因为它们是嵌套在 JSON 里的自由结构,不是顶层列名,不必转 snake_case)。
  career_paths 只保留 frontend / backend;current_jobs 的转型建议里
  指向已删职业的项已移除(个别职业的建议列表因此为空,一期接受)。
"""

from __future__ import annotations

import sys

from sqlalchemy import delete
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models.career import CareerPath, CurrentJob
from app.models.industry import Industry
from app.models.job import Job, Question


# ============ 职业元信息(对应 jobs 表) ============
JOBS: list[dict] = [
    {"id": "frontend", "title": "前端工程师", "emoji": "💻", "tagline": "HTML / CSS / JavaScript / 框架", "accent": "#1CB0F6"},
    {"id": "backend", "title": "后端工程师", "emoji": "🗄️", "tagline": "数据库 / 接口 / 分布式", "accent": "#58CC02"},
]


# ============ 行业知识库(对应 industries 表) ============
# 数据来源:前端 data/industries.ts,字段逐字迁移。
INDUSTRIES: list[dict] = [
    {
        "id": "web-dev",
        "name": "前端 / Web 开发",
        "emoji": "🌐",
        "accent": "#1CB0F6",
        "summary": "构建网站与 Web 应用的界面和交互",
        "overview": (
            "前端开发负责用户直接看到和操作的部分，核心是 HTML/CSS/JavaScript 三件套，"
            "进阶到 React/Vue 等框架与工程化。入门门槛相对友好，作品可视化强，适合转行起步。"
        ),
        "key_points": [
            "基础三件套：HTML 语义化、CSS 布局、JavaScript 逻辑",
            "主流框架：React / Vue，理解组件化与状态管理",
            "工程化：包管理、打包构建、Git 协作",
            "浏览器原理：渲染、事件循环、网络请求",
        ],
        "links": [
            {"label": "MDN Web 文档（中文）", "url": "https://developer.mozilla.org/zh-CN/"},
            {"label": "React 官方文档", "url": "https://react.dev/"},
            {"label": "开发者路线图 roadmap.sh", "url": "https://roadmap.sh/frontend"},
        ],
    },
    {
        "id": "backend",
        "name": "后端 / 服务端开发",
        "emoji": "🗄️",
        "accent": "#58CC02",
        "summary": "支撑业务逻辑、数据存储与接口的服务端",
        "overview": (
            "后端负责数据处理、业务逻辑和对外接口。需要掌握一门服务端语言、数据库、"
            "以及 HTTP/接口设计，进阶到缓存、消息队列与分布式。逻辑性强，岗位需求量大。"
        ),
        "key_points": [
            "一门服务端语言：Python / Java / Go 任选其一深入",
            "数据库：关系型（SQL）+ 缓存（Redis）",
            "接口设计：RESTful、鉴权、幂等",
            "进阶：并发、分布式、微服务",
        ],
        "links": [
            {"label": "后端路线图 roadmap.sh", "url": "https://roadmap.sh/backend"},
            {"label": "PostgreSQL 官方文档", "url": "https://www.postgresql.org/docs/"},
            {"label": "Redis 官方站点", "url": "https://redis.io/"},
        ],
    },
    {
        "id": "data",
        "name": "数据分析 / 数据科学",
        "emoji": "📊",
        "accent": "#FF4B4B",
        "summary": "用数据发现问题、驱动决策",
        "overview": (
            "数据方向从分析师到数据科学家，核心是 SQL、统计思维和可视化表达，"
            "进阶到 Python 数据处理与机器学习。适合有业务理解、擅长逻辑与表达的转行者。"
        ),
        "key_points": [
            "SQL：查询、聚合、多表关联",
            "统计基础：分布、假设检验、AB 实验",
            "Python 数据栈：pandas / numpy / matplotlib",
            "可视化与业务表达：把数据讲成故事",
        ],
        "links": [
            {"label": "pandas 官方文档", "url": "https://pandas.pydata.org/docs/"},
            {"label": "scikit-learn 官方站点", "url": "https://scikit-learn.org/stable/"},
            {"label": "LeetCode 数据库题（练 SQL）", "url": "https://leetcode.cn/problemset/database/"},
        ],
    },
    {
        "id": "product",
        "name": "产品经理",
        "emoji": "📋",
        "accent": "#FF9600",
        "summary": "定义做什么、为谁做、如何衡量成功",
        "overview": (
            "产品经理连接用户、业务和研发，负责需求挖掘、方案设计与项目推进。"
            "不强依赖编程，但需要极强的沟通、逻辑与数据判断能力，是很多转行者的热门方向。"
        ),
        "key_points": [
            "需求分析：用户调研、场景拆解、优先级排序",
            "文档与协作：PRD、原型、跨团队推进",
            "数据驱动：留存/转化指标、AB 测试",
            "商业理解：商业模式、增长与竞品分析",
        ],
        "links": [
            {"label": "产品经理路线图 roadmap.sh", "url": "https://roadmap.sh/product-manager"},
            {"label": "Figma（原型设计工具）", "url": "https://www.figma.com/"},
        ],
    },
]


# ============ 目标职业学习路径(对应 career_paths 表) ============
# 数据来源:前端 data/careers.ts 的 CAREER_PATHS,字段逐字迁移。
CAREER_PATHS: list[dict] = [
    {
        "id": "frontend",
        "title": "前端工程师",
        "emoji": "💻",
        "accent": "#1CB0F6",
        "summary": "门槛友好、成果可视化，适合转行起步",
        "steps": [
            {
                "id": "fe-s1",
                "title": "打好三件套基础",
                "desc": "系统学习 HTML 语义化、CSS 布局（Flex/Grid）、JavaScript 核心语法与 DOM 操作。",
                "links": [{"label": "MDN 学习区", "url": "https://developer.mozilla.org/zh-CN/docs/Learn"}],
            },
            {
                "id": "fe-s2",
                "title": "掌握一个主流框架",
                "desc": "以 React 为例，理解组件化、状态管理、Hooks 与单向数据流。",
                "links": [{"label": "React 官方教程", "url": "https://react.dev/learn"}],
            },
            {
                "id": "fe-s3",
                "title": "工程化与协作",
                "desc": "熟悉包管理、打包构建、Git 版本控制和团队协作流程。",
            },
            {
                "id": "fe-s4",
                "title": "刷面试题巩固",
                "desc": "通过前端面试题答题检验掌握程度，查漏补缺。",
                "quizJobId": "frontend",
            },
        ],
    },
    {
        "id": "backend",
        "title": "后端工程师",
        "emoji": "🗄️",
        "accent": "#58CC02",
        "summary": "逻辑性强、需求量大，适合喜欢钻研的人",
        "steps": [
            {
                "id": "be-s1",
                "title": "选定一门服务端语言",
                "desc": "Python / Java / Go 任选其一深入，掌握语法、数据结构与常用标准库。",
                "links": [{"label": "后端路线图", "url": "https://roadmap.sh/backend"}],
            },
            {
                "id": "be-s2",
                "title": "数据库与缓存",
                "desc": "学习关系型数据库（SQL、事务、索引）与 Redis 缓存的典型用法。",
                "links": [{"label": "PostgreSQL 文档", "url": "https://www.postgresql.org/docs/"}],
            },
            {
                "id": "be-s3",
                "title": "接口设计与鉴权",
                "desc": "理解 RESTful 设计、HTTP 语义、幂等性与常见鉴权方案。",
            },
            {
                "id": "be-s4",
                "title": "刷面试题巩固",
                "desc": "通过后端面试题答题检验对数据库、接口、分布式的理解。",
                "quizJobId": "backend",
            },
        ],
    },
]

# ============ 当前职业 + 转型建议(对应 current_jobs 表) ============
# 数据来源:前端 data/careers.ts 的 CURRENT_JOBS,字段逐字迁移。
CURRENT_JOBS: list[dict] = [
    {
        "id": "tester",
        "title": "测试工程师",
        "emoji": "🧪",
        "recommendations": [
            {"targetId": "backend", "reason": "已懂业务流程与接口，补齐编码即可转后端", "difficulty": "中等"},
            {"targetId": "frontend", "reason": "熟悉产品形态，转前端上手快、成果直观", "difficulty": "较易"},
        ],
    },
    {
        "id": "operation",
        "title": "运营 / 市场",
        "emoji": "📣",
        "recommendations": [],
    },
    {
        "id": "designer",
        "title": "UI / 视觉设计师",
        "emoji": "🎨",
        "recommendations": [
            {"targetId": "frontend", "reason": "对页面与交互敏感，补 JS 逻辑即可转前端", "difficulty": "较易"},
        ],
    },
    {
        "id": "traditional-it",
        "title": "传统 IT / 运维",
        "emoji": "🖥️",
        "recommendations": [
            {"targetId": "backend", "reason": "有系统与网络基础，转后端衔接顺畅", "difficulty": "中等"},
        ],
    },
    {
        "id": "student",
        "title": "应届 / 转行小白",
        "emoji": "🎓",
        "recommendations": [
            {"targetId": "frontend", "reason": "入门门槛友好、成果可视，适合零基础起步", "difficulty": "较易"},
            {"targetId": "backend", "reason": "需求量大、成长空间足，适合肯钻研的人", "difficulty": "较难"},
        ],
    },
]


def seed(
    session: Session, *, reset: bool = False
) -> tuple[int, int, int, int]:
    """灌入数据。返回 (职业数, 行业数, 学习路径数, 当前职业数)。

    各表各自独立做幂等判断,允许"jobs 已灌、industries/careers 还没灌"
    这类增量场景(比如这次新增 industries/careers 表,老环境不用 --reset 也能补灌)。

    questions 不在本脚本职责内:题库由开源导入管道供给
    (python -m app.import_question_banks),--reset 后记得重跑导入。
    """
    if reset:
        # 先删子表(questions)再删父表(jobs),否则外键约束会拒绝删除。
        # industries/career_paths/current_jobs 均无子表,直接删即可。
        # questions 删掉后不会由 seed 补——重跑导入管道。
        session.exec(delete(Question))
        session.exec(delete(Job))
        session.exec(delete(Industry))
        session.exec(delete(CareerPath))
        session.exec(delete(CurrentJob))
        session.commit()

    jobs_n, industries_n, career_paths_n, current_jobs_n = 0, 0, 0, 0

    if session.exec(select(Job)).first() is None:
        session.add_all(Job(**job) for job in JOBS)
        jobs_n = len(JOBS)

    if session.exec(select(Industry)).first() is None:
        session.add_all(Industry(**industry) for industry in INDUSTRIES)
        industries_n = len(INDUSTRIES)

    if session.exec(select(CareerPath)).first() is None:
        session.add_all(CareerPath(**path) for path in CAREER_PATHS)
        career_paths_n = len(CAREER_PATHS)

    if session.exec(select(CurrentJob)).first() is None:
        session.add_all(CurrentJob(**job) for job in CURRENT_JOBS)
        current_jobs_n = len(CURRENT_JOBS)

    session.commit()
    return jobs_n, industries_n, career_paths_n, current_jobs_n


def main() -> None:
    reset = "--reset" in sys.argv
    init_db()  # 表不存在时先建表,与启动时 init_db() 逻辑一致
    with Session(engine) as session:
        jobs_n, industries_n, career_paths_n, current_jobs_n = seed(session, reset=reset)

    if jobs_n or industries_n or career_paths_n or current_jobs_n:
        print(
            f"seed 完成:灌入 {jobs_n} 个职业、{industries_n} 个行业、"
            f"{career_paths_n} 条学习路径、{current_jobs_n} 个当前职业选项"
            "(题库请另行执行 python -m app.import_question_banks)"
        )
    else:
        print("数据已存在,跳过灌入;如需重灌请加 --reset")


if __name__ == "__main__":
    main()
