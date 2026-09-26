from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.security import create_access_token, verify_password

router = APIRouter()
templates = Jinja2Templates(directory="templates")

COOKIE_NAME = "access_token"


class LoginIn(BaseModel):
    email: str
    password: str


def _authenticate(db: Session, email: str, password: str) -> models.User | None:
    user = db.query(models.User).filter(models.User.email == email.strip().lower()).first()
    if user and verify_password(password, user.hashed_password):
        return user
    return None


def _login_response(user: models.User, redirect: bool) -> RedirectResponse:
    token = create_access_token(user.email)
    if redirect:
        resp = RedirectResponse("/", status_code=303)
    else:
        resp = JSONResponse({"access_token": token, "token_type": "bearer"})
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=60 * 60 * 24)
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request, "auth/login.html", {"error": request.query_params.get("error")}
    )


@router.post("/login")
def login_form(
    email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)
):
    user = _authenticate(db, email, password)
    if not user:
        return RedirectResponse("/login?error=1", status_code=303)
    return _login_response(user, redirect=True)


@router.post("/api/auth/login")
def login_api(payload: LoginIn, db: Session = Depends(get_db)):
    user = _authenticate(db, payload.email, payload.password)
    if not user:
        return JSONResponse({"detail": "Invalid credentials"}, status_code=401)
    return _login_response(user, redirect=False)


@router.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/api/auth/me")
def me(user: models.User = Depends(get_current_user)):
    return {"email": user.email, "name": user.name, "role": user.role}
