"""Standalone KTRU API (no database). Run: uv run uvicorn src.api.ktru.app:app --port 8010

KTRU_SNAPSHOT_DIR points to a published snapshot (default: ../.ktru-build/snapshots/current).
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.ktru import contracts as C
from src.ktru.catalog.reader import Catalog
from src.ktru.engine.products import split_descriptions
from src.ktru.engine.resolve import value_display
from src.ktru.ml import extract as ml
from src.ktru.service.photos import PortalPhotos
from src.ktru.service.pipeline import Pipeline, ServiceError

DEFAULT_SNAPSHOT = Path(__file__).resolve().parents[4] / ".ktru-build" / "snapshots" / "current"


@asynccontextmanager
async def lifespan(app: FastAPI):
    snap = Path(os.environ.get("KTRU_SNAPSHOT_DIR", DEFAULT_SNAPSHOT))
    app.state.pipeline = Pipeline(Catalog(snap)) if (snap / "manifest.json").exists() else None
    app.state.photos = PortalPhotos()
    yield


app = FastAPI(title="KTRU card service", version=C.CONTRACT_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)
router = APIRouter(prefix="/api/ktru", tags=["ktru"])
ERRORS: dict = {s: {"model": C.ErrorResponse} for s in (404, 409, 422, 503)}


@app.exception_handler(ServiceError)
async def _service_error(_: Request, exc: ServiceError):
    return JSONResponse(status_code=exc.status, content=C.ErrorResponse(error=exc.body).model_dump())


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    body = C.ErrorBody(
        code="invalid_input",
        message_ru=f"Некорректный запрос: {'.'.join(map(str, first.get('loc', [])))} — {first.get('msg', '')}",
        retryable=False,
    )
    return JSONResponse(status_code=422, content=C.ErrorResponse(error=body).model_dump())


def _pipeline(request: Request) -> Pipeline:
    p = request.app.state.pipeline
    if p is None:
        raise ServiceError(503, C.ErrorBody(code="unavailable", message_ru="Снимок КТРУ не загружен", retryable=True))
    return p


@router.get("/health")
def health(request: Request) -> C.Health:
    p: Pipeline | None = request.app.state.pipeline
    m = p.catalog.manifest if p else {}
    return C.Health(
        contract_version=C.CONTRACT_VERSION,
        snapshot_id=m.get("snapshot_id"),
        snapshot_date=m.get("snapshot_date"),
        counts=m.get("counts", {}),
        ready=p is not None,
        llm=ml.status(),
        llm_concurrency=ml.concurrency() if ml.config() else None,
        **(p.runtime_metadata if p else {}),
    )


@router.post("/split", responses=ERRORS)
def split(req: C.SplitRequest, request: Request) -> list[str]:
    catalog = _pipeline(request).catalog
    return split_descriptions(req.text, catalog.family_names().values(), catalog.product_property_phrases)


@router.post("/parse", responses=ERRORS)
def parse(req: C.ParseRequest, request: Request) -> C.CardState:
    return _pipeline(request).parse(req)


@router.post("/analyze", responses=ERRORS)
def analyze(req: C.AnalyzeRequest, request: Request) -> C.CardState:
    """Open-model extraction on the accepted text (slow path); /parse is the fast rules-only path."""
    if ml.config() is None:
        raise ServiceError(503, C.ErrorBody(code="unavailable", message_ru="Модель извлечения не настроена",
                                            retryable=False))  # fmt: skip
    return _pipeline(request).analyze(req)


@router.post("/answer", responses=ERRORS)
def answer(req: C.AnswerRequest, request: Request) -> C.CardState:
    return _pipeline(request).answer(req)


@router.post("/edit", responses=ERRORS)
def edit(req: C.EditRequest, request: Request) -> C.CardState:
    return _pipeline(request).edit(req)


@router.post("/undo", responses=ERRORS)
def undo(req: C.UndoRequest, request: Request) -> C.CardState:
    return _pipeline(request).undo(req)


@router.get("/sessions/{session_id}", responses=ERRORS)
def session(session_id: str, request: Request) -> C.CardState:
    return _pipeline(request).get(session_id)


@router.get("/examples", responses=ERRORS)
def examples(
    request: Request, domain: Literal["clothing", "furniture", "tools"] = "furniture", n: int = Query(6, ge=1, le=30)
) -> list[C.Example]:
    """Random real listings (СТЕ) from the organizers' Portal export — live demo input, not evaluation gold."""
    rows = _pipeline(request).catalog.examples(domain, n)
    return [C.Example(ste_id=sid, domain=domain, name=name, text=text) for sid, name, text in rows]


@router.get("/positions/{code}", responses=ERRORS)
def position(code: str, request: Request, snapshot_id: str = Query(),
             version: int = Query(ge=1, le=2**63 - 1)) -> C.PositionDetail:
    cat = _pipeline(request).catalog
    if snapshot_id != cat.snapshot_id:
        raise ServiceError(404, C.ErrorBody(code="not_found", message_ru="Другой снимок КТРУ", retryable=False))
    p = cat.position(code, version)
    if p is None:
        raise ServiceError(404, C.ErrorBody(code="not_found", message_ru="Позиция не найдена", retryable=False))
    return C.PositionDetail(
        ref=C.PositionRef(snapshot_id=cat.snapshot_id, code=p.code, version=p.version),
        name=p.name,
        okpd2_code=p.okpd2_code,
        okpd2_name=p.okpd2_name,
        is_template=p.is_template,
        status=p.status,
        family_id=p.family_id,
        supply_units=p.supply_units,
        parent_ref=p.parent_ref,
        rubricators=p.rubricators,
        application_date_start=p.application_date_start,
        application_date_end=p.application_date_end,
        source=p.source,
        characteristics=[
            C.PositionCharacteristic(
                char_code=c.char_code,
                name=c.name,
                required=c.required,
                kind_raw=c.kind_raw,
                values_ru=[value_display(v) for v in c.allowed],
                ref=c.ref,
                value_refs=[C.AllowedValueRef.model_validate(v["ref"]) for v in c.allowed if v.get("ref")],
                value_type=c.value_type,
                defects=c.defects,
                source=c.source,
            )
            for c in p.chars
        ],
    )


@router.get("/positions/{code}/photos", responses=ERRORS)
def photos(code: str, request: Request, snapshot_id: str = Query(),
           version: int = Query(ge=1, le=2**63 - 1), text: str = Query("", max_length=C.MAX_TEXT)) -> list[C.ProductPhoto]:
    cat = _pipeline(request).catalog
    position = cat.position(code, version)
    if snapshot_id != cat.snapshot_id or position is None:
        raise ServiceError(404, C.ErrorBody(code="not_found", message_ru="Позиция не найдена", retryable=False))
    return request.app.state.photos.find(position, text)


app.include_router(router)
