"""Look up a few public Portal products on demand; never fetch or store image bytes."""

import json
import logging
import re
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from threading import Lock

import httpx

from src.ktru import contracts as C
from src.ktru.catalog.reader import Position
from src.ktru.engine.text import content_stems, stems_match
from src.ktru.service.errors import ServiceError

BASE = "https://zakupki.mos.ru"
KTRU_CODE = re.compile(r"\d{2}\.\d{2}\.\d{2}\.\d{3}-\d{8}")
CARDS_CHECKED, PARALLEL_CARDS, PRODUCTS_SHOWN = 12, 4, 8
logger = logging.getLogger(__name__)


def _positive(value: object) -> str | None:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return format(number, "f") if number.is_finite() and number > 0 else None


def _related(left: str, right: str) -> bool:
    return left == right or left.startswith(right + ".") or right.startswith(left + ".")


def _overlap(text: str, name: str) -> int:
    names = content_stems(name)
    return sum(any(stems_match(word, candidate) for candidate in names) for word in set(content_stems(text)))


class PortalPhotos:
    def __init__(self):
        self._cache: OrderedDict[tuple, tuple[float, list[C.ProductPhoto]]] = OrderedDict()
        self._lock = Lock()

    @staticmethod
    def _json(client: httpx.Client, route: str, params: dict, deadline: float):
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise httpx.TimeoutException("Portal lookup deadline reached")
            # Every lookup starts anonymously; no credentials or previous session cookies are used.
            client.cookies.clear()
            started = time.monotonic()
            try:
                response = client.get(BASE + route, params=params, timeout=min(8, remaining))
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as error:
                status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
                logger.warning("Portal request failed: route=%s ste=%s attempt=%s error=%s status=%s seconds=%.2f",
                               route, params.get("id"), attempt + 1, type(error).__name__, status,
                               time.monotonic() - started)
                temporary = isinstance(error, httpx.TransportError) or status in (500, 502, 503, 504)
                if not temporary or attempt == 1 or deadline - time.monotonic() <= 0.5:
                    raise
                time.sleep(0.3)
        raise RuntimeError("Unreachable")

    def find(self, position: Position, text: str) -> list[C.ProductPhoto]:
        if not position.okpd2_code:
            return []
        key = (position.code, position.version, position.okpd2_code, position.name, text)
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < 300:
                self._cache.move_to_end(key)
                return cached[1]
        try:
            with httpx.Client(headers={"Accept": "application/json"}, follow_redirects=False) as client:
                result = self._find(client, position, text, time.monotonic() + 20)
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            logger.warning("Portal lookup failed: ktru=%s error=%s", position.code, type(error).__name__)
            retryable = True
            if isinstance(error, httpx.TimeoutException):
                message = "Портал не успел ответить. Повторите запрос фото и цен."
            elif isinstance(error, httpx.HTTPStatusError):
                status = error.response.status_code
                if status in (401, 403):
                    message, retryable = "Портал отклонил запрос к данным. Фото и цены сейчас недоступны.", False
                elif status == 429:
                    message = "Портал ограничил частоту запросов. Попробуйте получить фото и цены позже."
                else:
                    message = "Портал вернул ошибку при запросе фото и цен. Попробуйте ещё раз."
            elif isinstance(error, httpx.HTTPError):
                message = "Не удалось соединиться с Порталом. Повторите запрос фото и цен."
            else:
                message, retryable = "Портал вернул данные в неожиданном формате. Фото и цены сейчас недоступны.", False
            raise ServiceError(503, C.ErrorBody(
                code="unavailable", message_ru=message, retryable=retryable,
            )) from error
        with self._lock:
            self._cache[key] = (time.monotonic(), result)
            self._cache.move_to_end(key)
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
        return result

    def _find(self, client: httpx.Client, position: Position, text: str, deadline: float) -> list[C.ProductPhoto]:
        okpd = position.okpd2_code or ""
        query = {"filter": {"okpdCode": {"value": okpd, "contains": False}}, "take": 100}
        directories = self._json(client, "/newapi/api/Okpd/QueryOkpdProductions",
                                 {"queryFilter": json.dumps(query)}, deadline)
        paths = [row["treePathId"] for row in directories if row.get("code") == okpd]
        if not paths:
            return []
        # One bounded search within the selected catalog position, not a background crawl.
        query = {"filter": {"okpdPaths": {"values": paths}}, "skip": 0, "take": 18, "withCount": False,
                 "order": [{"field": "offersCount", "desc": True}]}
        payload = self._json(client, "/newapi/api/Sku/QueryIndex", {"queryFilter": json.dumps(query)}, deadline)
        rows = [row for row in payload["items"]
                if any((row.get("okpdPath") or "").startswith(path) for path in paths)]
        rows.sort(key=lambda row: (not any(_positive(row.get(field)) for field in ("medianPrice", "referencePrice")),
                                  -_overlap(position.name, row.get("name") or ""),
                                  -_overlap(text, row.get("name") or ""), -(row.get("offersCount") or 0)))
        rows = [row for row in rows if isinstance(row.get("id"), int) and not isinstance(row["id"], bool)
                and row["id"] > 0][:CARDS_CHECKED]

        def card(row: dict) -> tuple[dict, object, Exception | None]:
            try:
                return row, self._json(client, "/classifier/api/Sku/GetSku", {"id": row["id"]}, deadline), None
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
                return row, None, error

        # A few cards at a time: several products in about the time of one, without a burst against the Portal.
        with ThreadPoolExecutor(PARALLEL_CARDS) as pool:
            cards = list(pool.map(card, rows))
        result: list[C.ProductPhoto] = []
        available = 0
        unavailable: Exception | None = None
        for row, payload, error in cards:
            product = None
            if error is None:
                try:
                    product = self._product(row, payload, position)
                except (ValueError, TypeError, KeyError) as invalid:
                    error = invalid
            if error is not None:
                logger.warning("Portal STE skipped: ste=%s error=%s", row["id"], type(error).__name__)
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 429):
                    if result:
                        break
                    raise error
                unavailable = error
                continue
            available += 1
            if product is None:
                continue
            result.append(product)
            if len(result) == PRODUCTS_SHOWN:
                break
        if rows and not available and unavailable:
            raise unavailable
        # The Portal's own KTRU link first, then the same OKPD2; cards with a photo or a price before bare ones.
        rank = {"ktru": 0, "okpd2": 1, "okpd2_related": 2}
        result.sort(key=lambda photo: (rank[photo.match], photo.image_url is None and photo.price is None))
        return result

    @staticmethod
    def _product(row: dict, card: object, position: Position) -> C.ProductPhoto | None:
        sku_id = row["id"]
        if not isinstance(card, dict) or card.get("id") != sku_id:
            raise ValueError("Portal returned an invalid STE")
        production = card.get("production") or {}
        if not isinstance(production, dict) or not isinstance(production.get("okpd") or {}, dict):
            raise ValueError("Portal returned invalid STE production")
        card_okpd = (production.get("okpd") or {}).get("code") or ""
        if not isinstance(card_okpd, str):
            raise ValueError("Portal returned invalid STE OKPD")
        codes = set(KTRU_CODE.findall(json.dumps(card.get("ktru"))))
        if codes and position.code not in codes:
            return None
        if position.code not in codes and (not card_okpd or not _related(card_okpd, position.okpd2_code or "")):
            return None
        images = card.get("images") or []
        if not isinstance(images, list):
            raise ValueError("Portal returned invalid STE images")
        image_id = next((image.get("fileStorageId") for image in images if isinstance(image, dict)
                         and isinstance(image.get("fileStorageId"), int) and image["fileStorageId"] > 0), None)
        price = None
        # The public index exposes median/reference statistics even when detail price fields are empty.
        for field, kind in (("medianPrice", "median"), ("referencePrice", "reference")):
            amount = _positive(row.get(field))
            if amount:
                price = C.PortalPrice(amount=amount, currency="RUB", kind=kind,
                                      unit=card.get("okeiShortName") or card.get("okeiName") or row.get("okeiName"))
                break
        okpd = production.get("okpd") or {}
        # The Portal files every product under a КПГЗ entry («01.11.02.02.03.15 МОЛОТКИ») linked to its OKPD2.
        kpgz_code, kpgz_name = production.get("code"), production.get("name")
        return C.ProductPhoto(
            ste_id=sku_id, name=card.get("name") or row.get("name") or "Товар",
            source_url=f"{BASE}/sku/view/{sku_id}",
            image_url=f"{BASE}/newapi/api/Core/Thumbnail/{image_id}/300/300" if image_id else None,
            relation="portal_ktru" if position.code in codes else "okpd2_candidate",
            price=price, offers_count=row.get("offersCount") or 0, fetched_at=datetime.now(UTC).isoformat(),
            match="ktru" if position.code in codes else "okpd2" if card_okpd == position.okpd2_code else "okpd2_related",
            okpd2=C.PortalCode(code=card_okpd, name=okpd.get("name") if isinstance(okpd.get("name"), str) else None)
            if card_okpd else None,
            kpgz=C.PortalCode(code=kpgz_code, name=kpgz_name if isinstance(kpgz_name, str) else None)
            if isinstance(kpgz_code, str) and kpgz_code else None,
        )
