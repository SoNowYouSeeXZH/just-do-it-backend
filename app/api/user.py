from sqlalchemy.exc import IntegrityError
from sqlmodel.sql._expression_select_cls import SelectOfScalar

from fastapi import APIRouter
from pydantic import BaseModel
from sqlmodel import select

from app.db import SessionDep
from app.models.user import Users
from app.services.user import hash_password, verify_password

router = APIRouter(prefix="/api", tags=["user"])

class UserResponse(BaseModel):
    code: int
    message: str
    data: dict[str, str|int|bool|None]
    
class UserRequest(BaseModel):
    username: str
    password: str

@router.post("/login", response_model=UserResponse)
def user_login(req: UserRequest, session: SessionDep) -> UserResponse:
    """
    用户登录接口
    """
    try:
        user_statement: SelectOfScalar[Users] = select(Users).where(Users.username == req.username)
        user: Users | None = session.exec(user_statement).first()
    
        if user and verify_password(plain=req.password, hashed=user.password_hash):
            return UserResponse(code=200, message='登录成功', data=user.model_dump(mode="json", exclude={"password_hash"}))
        else:
            return UserResponse(code=401, message='用户名或密码错误', data={})
    except Exception:
        return UserResponse(code=500, message='服务异常，请稍后再试', data={})
    

@router.post(path="/registry", response_model=UserResponse)
def user_registry(req: UserRequest, session: SessionDep) -> UserResponse:
    """
    用户注册接口
    """
    
    try:
        new_user = Users(username=req.username, password_hash=hash_password(req.password))
        session.add(new_user)
        session.commit()
        session.refresh(new_user)
        return UserResponse(code=200, message='新用户注册成功', data=new_user.model_dump(mode="json", exclude={"password_hash"}))
    except IntegrityError:
        session.rollback()
        return UserResponse(code=409, message='用户名已存在', data={})
    except Exception:
        session.rollback()
        return UserResponse(code=500, message='服务异常，请稍后再试', data={})