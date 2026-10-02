from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from serve import pages


def render_error(request: Request, status_code: int, title: str, message: str) -> HTMLResponse:
    return pages._templates.TemplateResponse(
        request,
        f"{status_code}.html",
        {"title": title, "message": message},
        status_code=status_code,
    )


def csrf_error_page(request: Request) -> HTMLResponse:
    return render_error(
        request,
        403,
        "That action expired",
        "For safety, forms time out after a while. Go back and submit again.",
    )


def register_error_pages(application: FastAPI) -> None:
    @application.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404 and "text/html" in request.headers.get("accept", ""):
            return render_error(
                request, 404, "Page not found", "That page does not exist. Try Home or Searches."
            )
        return JSONResponse({"error": "not_found"}, status_code=exc.status_code)

    @application.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        if "text/html" in request.headers.get("accept", ""):
            return render_error(
                request,
                500,
                "Something broke",
                "The console hit an unexpected error. Nothing was lost — try again.",
            )
        return JSONResponse({"error": "internal_error"}, status_code=500)
