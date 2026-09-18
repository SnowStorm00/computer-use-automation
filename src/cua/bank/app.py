from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

MEMBERS = {
    "12345": {
        "id": "12345",
        "name": "Jane Alvarez",
        "tin": "000-00-4412",
        "savings": "4,250.00",
        "checking": "812.33",
        "status": "Active",
        "restricted": False,
    },
    "67890": {
        "id": "67890",
        "name": "Robert Chen",
        "tin": "000-00-0091",
        "savings": "15,000.00",
        "checking": "2,104.80",
        "status": "Active",
        "restricted": False,
    },
    "11111": {
        "id": "11111",
        "name": "Restricted Record",
        "tin": "000-00-7777",
        "savings": "0.00",
        "checking": "0.00",
        "status": "Restricted",
        "restricted": True,
    },
}

SESSIONS: dict[str, dict] = {}
CONFIRMATIONS: dict[str, dict] = {}


def create_bank_app() -> FastAPI:
    app = FastAPI(title="CoreLink Member Servicing")
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def logged_in(request: Request) -> str | None:
        sid = request.cookies.get("bank_sid")
        if sid and sid in SESSIONS:
            return SESSIONS[sid]["operator_id"]
        return None

    def login_redirect():
        return RedirectResponse(url="/bank/login", status_code=303)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def root(request: Request):
        if not logged_in(request):
            return login_redirect()
        return RedirectResponse(url="/bank/shell", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str | None = None):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": error},
        )

    @app.post("/login")
    def login(operator_id: str = Form(""), password: str = Form("")):
        if not operator_id.strip() or not password.strip():
            return RedirectResponse(url="/bank/login?error=required", status_code=303)
        if operator_id.strip() != "teller01" or password != "demo":
            return RedirectResponse(url="/bank/login?error=invalid", status_code=303)
        sid = secrets.token_hex(8)
        SESSIONS[sid] = {"operator_id": operator_id.strip()}
        resp = RedirectResponse(url="/bank/shell", status_code=303)
        resp.set_cookie("bank_sid", sid, httponly=True)
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse(url="/bank/login", status_code=303)
        resp.delete_cookie("bank_sid")
        return resp

    @app.get("/shell", response_class=HTMLResponse)
    def shell(request: Request):
        operator = logged_in(request)
        if not operator:
            return login_redirect()
        return templates.TemplateResponse(
            request,
            "shell.html",
            {"operator": operator},
        )

    @app.get("/ws/search", response_class=HTMLResponse)
    def search(request: Request, notice: str | None = None):
        if not logged_in(request):
            return templates.TemplateResponse(request, "session_expired.html", {})
        return templates.TemplateResponse(request, "search.html", {"notice": notice, "error": None})

    @app.get("/ws/results", response_class=HTMLResponse)
    def results(request: Request, member_id: str = ""):
        if not logged_in(request):
            return templates.TemplateResponse(request, "session_expired.html", {})
        q = member_id.strip()
        if not q:
            return templates.TemplateResponse(
                request,
                "search.html",
                {"notice": None, "error": "Member ID is required."},
            )
        if q == "00000":
            return templates.TemplateResponse(request, "hold_dialog.html", {"member_id": q})
        if q.upper() == "TIMEOUT":
            return templates.TemplateResponse(request, "session_expired.html", {})
        member = MEMBERS.get(q)
        if not member:
            return templates.TemplateResponse(
                request,
                "not_found.html",
                {"member_id": q},
            )
        if member["restricted"]:
            return templates.TemplateResponse(
                request,
                "denied.html",
                {"member_id": q},
            )
        return templates.TemplateResponse(request, "results.html", {"member": member})

    @app.get("/ws/member/{member_id}", response_class=HTMLResponse)
    def member_detail(request: Request, member_id: str):
        if not logged_in(request):
            return templates.TemplateResponse(request, "session_expired.html", {})
        member = MEMBERS.get(member_id)
        if not member:
            return templates.TemplateResponse(request, "not_found.html", {"member_id": member_id})
        if member["restricted"]:
            return templates.TemplateResponse(request, "denied.html", {"member_id": member_id})
        return templates.TemplateResponse(request, "member.html", {"member": member})

    @app.get("/ws/subaccount/{member_id}", response_class=HTMLResponse)
    def subaccount_form(request: Request, member_id: str, error: str | None = None):
        if not logged_in(request):
            return templates.TemplateResponse(request, "session_expired.html", {})
        member = MEMBERS.get(member_id)
        if not member:
            return templates.TemplateResponse(request, "not_found.html", {"member_id": member_id})
        return templates.TemplateResponse(
            request,
            "subaccount.html",
            {"member": member, "error": error},
        )

    @app.post("/ws/subaccount/{member_id}/submit")
    def subaccount_submit(
        request: Request,
        member_id: str,
        product: str = Form(""),
        amount: str = Form(""),
    ):
        if not logged_in(request):
            return templates.TemplateResponse(request, "session_expired.html", {})
        member = MEMBERS.get(member_id)
        if not member:
            return templates.TemplateResponse(request, "not_found.html", {"member_id": member_id})
        if not product or not amount.strip():
            return RedirectResponse(
                url=f"/bank/ws/subaccount/{member_id}?error=required",
                status_code=303,
            )
        try:
            value = float(amount.replace(",", "").replace("$", ""))
        except ValueError:
            return RedirectResponse(
                url=f"/bank/ws/subaccount/{member_id}?error=amount",
                status_code=303,
            )
        if value <= 0:
            return RedirectResponse(
                url=f"/bank/ws/subaccount/{member_id}?error=amount",
                status_code=303,
            )
        conf = "CL-" + secrets.token_hex(3).upper()
        CONFIRMATIONS[conf] = {
            "member": member,
            "product": product,
            "amount": f"{value:,.2f}",
            "confirmation": conf,
        }
        return RedirectResponse(url=f"/bank/ws/confirm/{conf}", status_code=303)

    @app.get("/ws/confirm/{conf}", response_class=HTMLResponse)
    def confirm(request: Request, conf: str):
        if not logged_in(request):
            return templates.TemplateResponse(request, "session_expired.html", {})
        data = CONFIRMATIONS.get(conf)
        if not data:
            return templates.TemplateResponse(request, "not_found.html", {"member_id": conf})
        return templates.TemplateResponse(request, "confirm.html", data)

    return app
