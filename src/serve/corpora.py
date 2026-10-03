from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, insert, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from serve import errors, pages
from store.models import Corpus, Runs

FINISHED_STATUSES = frozenset({"done", "partial"})
_ERROR_STATUS = {
    "invalid_name": 400,
    "not_finished": 400,
    "duplicate_name": 400,
    "not_found": 404,
}


class CorpusError(Exception):
    def __init__(self, code: str, message: str, hints: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = hints


@dataclass(frozen=True)
class CorpusView:
    id: int
    name: str
    note: str | None
    repo_count: int
    frozen_at: datetime | None
    source_run_id: int
    filter_hash: str
    status: str
    sentence: str


_COLUMNS = (
    Corpus.id,
    Corpus.name,
    Corpus.note,
    Corpus.repo_count,
    Corpus.frozen_at,
    Corpus.source_run_id,
    Runs.filter_hash,
    Runs.status,
    Runs.filter_spec,
)


def _view(row) -> CorpusView:
    return CorpusView(
        id=row["id"],
        name=row["name"],
        note=row["note"],
        repo_count=row["repo_count"],
        frozen_at=row["frozen_at"],
        source_run_id=row["source_run_id"],
        filter_hash=row["filter_hash"],
        status=row["status"],
        sentence=pages._spec_sentence(row["filter_spec"]),
    )


def list_corpora(engine: Engine) -> list[CorpusView]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(*_COLUMNS)
            .join(Runs, Runs.id == Corpus.source_run_id)
            .order_by(Corpus.frozen_at.desc(), Corpus.id.desc())
        ).mappings()
        return [_view(row) for row in rows]


def get_corpus(engine: Engine, corpus_id: int) -> CorpusView | None:
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(*_COLUMNS)
                .join(Runs, Runs.id == Corpus.source_run_id)
                .where(Corpus.id == corpus_id)
            )
            .mappings()
            .one_or_none()
        )
    return _view(row) if row is not None else None


def freeze_corpus(engine: Engine, run_id: int, name: str | None, note: str | None) -> int:
    cleaned = (name or "").strip()
    if not cleaned:
        raise CorpusError(
            "invalid_name",
            "Give the corpus a name.",
            ("the name appears in the Corpora list",),
        )
    cleaned_note = (note or "").strip() or None
    with engine.begin() as connection:
        run = (
            connection.execute(select(Runs.id, Runs.status).where(Runs.id == run_id))
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise CorpusError(
                "not_found",
                f"Search #{run_id} does not exist.",
                ("open a finished search and freeze it",),
            )
        if run["status"] not in FINISHED_STATUSES:
            raise CorpusError(
                "not_finished",
                "Only a finished search can be frozen into a corpus.",
                ("run the search to completion first",),
            )
        repo_count = int(
            connection.scalar(
                text("SELECT count(*) FROM run_items WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            or 0
        )
        try:
            corpus_id = connection.scalar(
                insert(Corpus)
                .values(
                    name=cleaned,
                    source_run_id=run_id,
                    note=cleaned_note,
                    repo_count=repo_count,
                )
                .returning(Corpus.id)
            )
        except IntegrityError as exc:
            raise CorpusError(
                "duplicate_name",
                f"A corpus named “{cleaned}” already exists.",
                ("pick a different name",),
            ) from exc
    return int(corpus_id)


def delete_corpus(engine: Engine, corpus_id: int) -> None:
    with engine.begin() as connection:
        result = connection.execute(delete(Corpus).where(Corpus.id == corpus_id))
        if not result.rowcount:
            raise CorpusError(
                "not_found",
                "That corpus does not exist.",
                ("it may have been deleted already",),
            )


def _render_list(
    request: Request,
    engine: Engine,
    *,
    error: str = "",
    hints: tuple[str, ...] = (),
    status_code: int = 200,
):
    try:
        views = list_corpora(engine)
    except Exception:
        views = []
        error = error or "Corpora are unavailable right now; the database did not answer."
        hints = ()
    items = [
        {
            "id": view.id,
            "name": view.name,
            "note": view.note,
            "repo_count": view.repo_count,
            "frozen_at": pages._relative_time(view.frozen_at),
            "source_run_id": view.source_run_id,
            "sentence": view.sentence,
        }
        for view in views
    ]
    return pages._templates.TemplateResponse(
        request,
        "corpora.html",
        {
            "corpora": items,
            "error": error,
            "hints": list(hints),
            "csrf_token": request.state.csrf_token,
        },
        status_code=status_code,
    )


def _render_detail(request: Request, engine: Engine, corpus_id: int, *, status_code: int = 200):
    view = get_corpus(engine, corpus_id)
    if view is None:
        return pages._templates.TemplateResponse(
            request,
            "corpus_detail.html",
            {"corpus": None, "csrf_token": request.state.csrf_token},
            status_code=404,
        )
    table = pages.results_context(
        engine,
        view.source_run_id,
        page=1,
        per_page=pages.PREVIEW_PAGE_SIZE,
        sort="stars",
        dir="desc",
    )
    return pages._templates.TemplateResponse(
        request,
        "corpus_detail.html",
        {
            "corpus": {
                "id": view.id,
                "name": view.name,
                "note": view.note,
                "repo_count": view.repo_count,
                "frozen_at": pages._iso(view.frozen_at),
                "source_run_id": view.source_run_id,
                "sentence": view.sentence,
                "status": view.status,
            },
            "preview_page_size": pages.PREVIEW_PAGE_SIZE,
            "csrf_token": request.state.csrf_token,
            **(table or {}),
        },
        status_code=status_code,
    )


def register_corpora(application: FastAPI, *, engine_factory: Callable[[], Engine]) -> None:
    @application.get("/corpora", response_class=HTMLResponse)
    def corpora_page(request: Request):
        return _render_list(request, engine_factory())

    @application.get("/corpora/{corpus_id}", response_class=HTMLResponse)
    def corpus_page(request: Request, corpus_id: int):
        return _render_detail(request, engine_factory(), corpus_id)

    @application.post("/runs/{run_id}/corpus")
    async def freeze_route(request: Request, run_id: int):
        if not await pages.validate_csrf(request):
            return errors.csrf_error_page(request)
        form = await request.form()
        name = form.get("name")
        note = form.get("note")
        try:
            corpus_id = await run_in_threadpool(
                freeze_corpus,
                engine_factory(),
                run_id,
                name if isinstance(name, str) else None,
                note if isinstance(note, str) else None,
            )
        except CorpusError as exc:
            return await run_in_threadpool(
                _render_list,
                request,
                engine_factory(),
                error=exc.message,
                hints=exc.hints,
                status_code=_ERROR_STATUS.get(exc.code, 400),
            )
        return RedirectResponse(f"/corpora/{corpus_id}", status_code=303)

    @application.post("/corpora/{corpus_id}/delete")
    async def delete_route(request: Request, corpus_id: int):
        if not await pages.validate_csrf(request):
            return errors.csrf_error_page(request)
        try:
            await run_in_threadpool(delete_corpus, engine_factory(), corpus_id)
        except CorpusError as exc:
            return await run_in_threadpool(
                _render_detail,
                request,
                engine_factory(),
                corpus_id,
                status_code=_ERROR_STATUS.get(exc.code, 400),
            )
        return RedirectResponse("/corpora", status_code=303)
