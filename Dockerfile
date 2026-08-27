# syntax=docker/dockerfile:1

# ===== 基础镜像 =====
# 用官方 Python 3.12 的精简版(slim):体积小,适合服务器部署。
FROM python:3.12-slim

# ===== 环境变量 =====
# PYTHONDONTWRITEBYTECODE:不生成 .pyc 缓存文件,保持镜像干净
# PYTHONUNBUFFERED:让日志实时输出(不缓冲),方便在容器里看日志
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 设置容器内的工作目录,后续命令都在这个目录下执行
WORKDIR /app

# ===== 先装依赖,再拷代码(利用 Docker 缓存加速构建)=====
# 只要 requirements.txt 没变,这一层缓存就能复用,不用每次都重装依赖。
# --mount=type=cache 把 pip 的下载缓存挂到宿主机并跨构建保留:
# 即使 requirements.txt 变了需要重装,也从本地缓存装,不再走网络重下。
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# 再把应用代码拷进来
COPY ./app ./app
# Alembic 迁移配置也进入镜像，部署时可执行 alembic current/upgrade/stamp
COPY alembic.ini .
COPY alembic ./alembic

# 声明容器对外暴露 8000 端口(仅作文档说明,真正映射在 compose 里配)
EXPOSE 8000

# 容器启动时执行的命令:用 uvicorn 启动服务。
# --host 0.0.0.0 表示监听所有网卡,这样容器外才能访问到(必须,别写 127.0.0.1)。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
