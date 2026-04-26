from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.core.security import create_access_token, hash_password, verify_password
from app.deps import get_current_user, get_db
from app.models import User
from app.schemas import (
    LoginIn,
    LoginOut,
    RegisterIn,
    UpdatePasswordIn,
    UpdateUsernameIn,
    UserPublic,
)


router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/auth/register", response_model=UserPublic)
def register(payload: RegisterIn, db: Session = Depends(get_db)):
    existing = db.exec(select(User).where(User.username == payload.username)).first()
    if existing:
        raise HTTPException(status_code=400, detail="username already exists")
    user = User(username=payload.username, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserPublic.model_validate(user, from_attributes=True)


@router.post("/auth/login", response_model=LoginOut)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.exec(select(User).where(User.username == payload.username)).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid username or password")
    token = create_access_token(subject=str(user.id))
    return LoginOut(token=token, user=UserPublic.model_validate(user, from_attributes=True))


@router.post("/auth/logout")
def logout():
    # stateless JWT - frontend should just delete token
    return {"ok": True}


@router.get("/me", response_model=UserPublic)
def me(user: User = Depends(get_current_user)):
    return UserPublic.model_validate(user, from_attributes=True)


@router.patch("/me", response_model=UserPublic)
def update_username(payload: UpdateUsernameIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    existing = db.exec(select(User).where(User.username == payload.username, User.id != user.id)).first()
    if existing:
        raise HTTPException(status_code=400, detail="username already exists")
    user.username = payload.username
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserPublic.model_validate(user, from_attributes=True)


@router.patch("/me/password")
def update_password(payload: UpdatePasswordIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="old password incorrect")
    user.password_hash = hash_password(payload.new_password)
    db.add(user)
    db.commit()
    return {"ok": True}

