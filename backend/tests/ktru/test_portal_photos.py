"""Regressions for transient Portal errors; no real network or image downloads."""

import time

import httpx
import pytest

from src.ktru.catalog.reader import Position
from src.ktru.service.errors import ServiceError
from src.ktru.service.photos import PortalPhotos

OKPD = "31.01.11.122"
PATH = ".1129754.1129760."


@pytest.fixture
def position():
    return Position(code=OKPD + "-00000001", version=1, name="Шкаф архивный металлический",
                    okpd2_code=OKPD, okpd2_name="Шкафы", status="ACTIVE", is_template=False,
                    family_id="cabinet", domain="furniture", supply_units=[])


def test_transient_request_retries_once_without_credentials(monkeypatch, caplog):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    requests = []

    def respond(request):
        requests.append(request)
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if len(requests) == 1:
            raise httpx.ReadTimeout("Slow Portal", request=request)
        return httpx.Response(200, json={"id": 1})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        client.cookies.set("session-cookie", "old-session")
        payload = PortalPhotos._json(client, "/classifier/api/Sku/GetSku", {"id": 1}, time.monotonic() + 20)
    assert payload == {"id": 1} and len(requests) == 2
    assert "ReadTimeout" in caplog.text and "/classifier/api/Sku/GetSku" in caplog.text


@pytest.mark.parametrize("failure", ["timeout", "rate_limit", "malformed"])
def test_failed_card_keeps_already_found_product(position, monkeypatch, failure):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    card_requests = []

    def respond(request):
        if request.url.path.endswith("QueryOkpdProductions"):
            return httpx.Response(200, json=[{"code": OKPD, "treePathId": PATH}])
        if request.url.path.endswith("QueryIndex"):
            return httpx.Response(200, json={"items": [
                {"id": i, "name": "Шкаф", "okpdPath": PATH} for i in (1, 2, 3)
            ]})
        sku_id = int(request.url.params["id"])
        card_requests.append(sku_id)
        if sku_id == 2:
            if failure == "timeout":
                raise httpx.ReadTimeout("Slow card", request=request)
            if failure == "malformed":
                return httpx.Response(200, json={})
            return httpx.Response(429)
        return httpx.Response(200, json={"id": sku_id, "name": "Шкаф", "ktru": None,
                                        "production": {"okpd": {"code": OKPD}},
                                        "images": [{"fileStorageId": sku_id}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        products = PortalPhotos()._find(client, position, "Шкаф", time.monotonic() + 20)
    assert products[0].ste_id == 1
    assert [p.ste_id for p in products] == ([1] if failure == "rate_limit" else [1, 3])
    assert card_requests.count(2) == (2 if failure == "timeout" else 1)


@pytest.mark.parametrize("status,expected", [(403, "отклонил"), (429, "частоту"), (None, "не успел ответить")])
def test_whole_lookup_reports_specific_reason(position, monkeypatch, status, expected):
    service = PortalPhotos()

    def fail(*_):
        if status is None:
            raise httpx.ReadTimeout("Slow Portal")
        request = httpx.Request("GET", "https://zakupki.mos.ru/newapi/api/Sku/QueryIndex")
        response = httpx.Response(status, request=request)
        raise httpx.HTTPStatusError("Portal rejected request", request=request, response=response)

    monkeypatch.setattr(service, "_find", fail)
    with pytest.raises(ServiceError) as caught:
        service.find(position, "Шкаф")
    assert caught.value.status == 503 and expected in caught.value.body.message_ru
    assert caught.value.body.retryable is (status != 403)


def test_several_products_carry_their_okpd2_and_kpgz(position):
    def respond(request):
        if request.url.path.endswith("QueryOkpdProductions"):
            return httpx.Response(200, json=[{"code": OKPD, "treePathId": PATH}])
        if request.url.path.endswith("QueryIndex"):
            return httpx.Response(200, json={"items": [{"id": i, "name": "Шкаф", "okpdPath": PATH} for i in range(1, 11)]})
        sku_id = int(request.url.params["id"])
        okpd = OKPD if sku_id % 2 else "31.01.11"  # odd cards: same OKPD2; even: its broader class
        return httpx.Response(200, json={
            "id": sku_id, "name": f"Шкаф {sku_id}", "ktru": [{"code": OKPD + "-00000001"}] if sku_id == 7 else None,
            "production": {"okpd": {"code": okpd, "name": "Шкафы"}, "code": "01.07.01.01", "name": "ШКАФЫ АРХИВНЫЕ"},
            "images": [] if sku_id == 3 else [{"fileStorageId": sku_id}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        products = PortalPhotos()._find(client, position, "Шкаф", time.monotonic() + 20)
    assert len(products) == 8  # more than one product, bounded
    assert products[0].ste_id == 7 and products[0].match == "ktru"  # the Portal's own KTRU link comes first
    assert {p.match for p in products[1:]} == {"okpd2", "okpd2_related"}
    assert all(p.kpgz and p.kpgz.code == "01.07.01.01" and p.okpd2 for p in products)
    assert next(p for p in products if p.ste_id == 3).image_url is None  # kept without a photo, shown later
