"""TOTP setup, login, logout routes."""
from urllib.parse import quote

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from shared.config import ACCOUNT, ENABLED_FILE
import shared.html_lib as hlib
from shared.html_lib import html, nav
from shared.security import audit, logged_in, require_auth, signer, totp

router = APIRouter()


@router.get("/setup")
def setup():
    if ENABLED_FILE.exists():
        return RedirectResponse("/login", 303)
    uri = totp().provisioning_uri(name=ACCOUNT, issuer_name="Codex Workstation")
    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    return html("绑定身份验证器",
        "<h1>首次绑定腾讯身份验证器</h1>"
        "<p>扫描二维码后输入当前 6 位验证码。</p>"
        "<div class='qr'>" + image.to_string(encoding="unicode") + "</div>"
        "<p>无法扫描时，在腾讯身份验证器中选择“手动输入密钥”：账户名为 <b>"
        + ACCOUNT + "</b>，类型为基于时间（TOTP），密钥为：</p>"
        "<input value='" + totp().secret + "' readonly>"
        "<form action='setup' method='post'>"
        "<input name='code' inputmode='numeric' autocomplete='one-time-code' "
        "pattern='[0-9]{6}' maxlength='6' placeholder='6 位验证码' required>"
        "<button>验证并启用</button></form>",
    )


@router.post("/setup")
def finish_setup(request: Request, code: str = Form(...)):
    if not totp().verify(code, valid_window=1):
        audit(request, "totp_setup_failed")
        return html("绑定失败",
            "<h1>验证码无效</h1>"
            "<p class='error'>请确认手机时间正确后重试。</p>"
            "<a href='setup'>返回二维码</a>",
        )
    ENABLED_FILE.touch(mode=0o600, exist_ok=True)
    audit(request, "totp_enabled")
    return RedirectResponse("/login", 303)


@router.get("/login")
def login(request: Request):
    if not ENABLED_FILE.exists():
        return RedirectResponse("/setup", 303)
    if logged_in(request):
        return RedirectResponse("/home", 303)
    return html("TOTP 登录",
        nav() + hlib.templates.get_template("login.html").render(),
    )


@router.post("/login")
def finish_login(request: Request, code: str = Form(...)):
    if not ENABLED_FILE.exists() or not totp().verify(code, valid_window=1):
        audit(request, "totp_login_failed")
        return html("登录失败", "<h1>验证码无效</h1><a href='login'>返回登录</a>")
    audit(request, "totp_login_succeeded")
    from shared.config import ACCOUNT, MAX_AGE
    response = RedirectResponse("/home", 303)
    response.set_cookie(
        "codex_web_session", signer().dumps(ACCOUNT),
        max_age=MAX_AGE, secure=True, httponly=True, samesite="lax", path="/",
    )
    return response


@router.post("/logout")
def logout(request: Request):
    audit(request, "logout")
    response = RedirectResponse("/login", 303)
    response.delete_cookie("codex_web_session", path="/")
    return response
