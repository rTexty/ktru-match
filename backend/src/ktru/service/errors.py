"""Typed service errors shared by the pipeline and session store."""

from src.ktru import contracts as C


class ServiceError(Exception):
    def __init__(self, status: int, body: C.ErrorBody):
        super().__init__(body.message_ru)
        self.status = status
        self.body = body


def _err(status: int, code: str, msg: str, *, retryable=False, revision=None, request_id=None) -> ServiceError:
    body = C.ErrorBody.model_validate(
        {
            "code": code,
            "message_ru": msg,
            "retryable": retryable,
            "current_revision": revision,
            "request_id": request_id,
        }  # fmt: skip
    )
    return ServiceError(status, body)
