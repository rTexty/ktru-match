"""Answer an open clarification from the description itself: one short, bounded model call.

The engine asks only about characteristics the text does not state. Often the description still implies the value:
«Стол письменный, ЛДСП, бук» has a wooden body although no word names the frame. The model chooses one of the
characteristic's catalog values, or none, and must quote the words of the description that imply it. The choice is a
reading of the text like any model fact: it keeps its quote, appears as a field, and the user can change it. Without a
verbatim quote, or with a value outside the catalog list, nothing is applied and the question stays.
"""

from __future__ import annotations

import json
import urllib.request

from src.ktru.engine.text import norm
from src.ktru.ml.extract import MAX_RESPONSE_BYTES, _headers, config, request_parameters

TIMEOUT_S = 15
MAX_QUOTE = 160
SYSTEM = (
    "Ты помогаешь заполнить характеристику товара для закупки по каталогу КТРУ. Дано описание товара, название "
    "характеристики и её допустимые значения. Выбери значение, только если оно прямо названо в описании или "
    "однозначно следует из его слов: например, «рукоять из ясеня» означает деревянную рукоять, «корпус из "
    "нержавеющей стали» — металлический корпус. Если описание допускает несколько значений или ничего о "
    "характеристике не говорит, верни null. Не угадывай по типичным товарам. Ответ — только JSON: "
    '{"value": "<одно из допустимых значений или null>", "quote": "<дословные слова из описания, на которых '
    'основан выбор>"}'
)


def choose(text: str, characteristic: str, labels: list[str]) -> tuple[str, int, int] | None:
    """The chosen catalog label and the span of the quoted words, or None when the text does not decide it."""
    cfg = config()
    if cfg is None or len(labels) < 2:
        return None
    url, model = cfg
    user = f"Описание: «{text}»\nХарактеристика: {characteristic}\nДопустимые значения: " + "; ".join(labels)
    body = {"model": model, **request_parameters(), "max_tokens": 200,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    request = urllib.request.Request(url + "/v1/chat/completions", json.dumps(body).encode(), _headers())
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Model response is too large")
    data = json.loads(json.loads(raw)["choices"][0]["message"]["content"])
    if not isinstance(data, dict):
        return None
    value, quote = data.get("value"), data.get("quote")
    if not isinstance(value, str) or not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE:
        return None
    label = next((label for label in labels if norm(label) == norm(value)), None)
    start = text.lower().find(quote.strip().lower())
    if label is None or start < 0:
        return None  # a value outside the catalog list, or words the description does not contain
    return label, start, start + len(quote.strip())
