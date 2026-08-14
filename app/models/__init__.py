# 让 app/models 成为一个子包,方便别处 from app.models.message import ChatMessage
#
# 同时:自动导入本包下的所有模型模块。
# SQLModel 只有在模型类"被 import 过"之后,才会把它注册到 metadata,
# create_all 才建得出对应的表。以前是在 db.py 里手动写 import,每加一张表
# 就得改一次;这里改成遍历本包目录、逐个 import,新增模型只要在 app/models/
# 下建文件即可,不用再动别处。
#
# 原理:pkgutil.iter_modules 列出本包下的所有子模块名,
# importlib.import_module 逐个导入,导入的副作用就是"完成表注册"。
import importlib
import pkgutil

for _module_info in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_module_info.name}")
