"""OpenRouter client: live model catalogue, batched summarisation, accounting.

Cost discipline lives here:

* one request per channel, never one per post — free models allow only 50
  requests/day, and a per-post loop would blow that on a single channel while
  re-sending the system prompt every time;
* structured outputs when the endpoint supports them, so no output tokens are
  wasted on prose around the JSON;
* an explicit ``max_tokens`` ceiling derived from the batch size;
* every response's ``usage.cost`` is recorded, and a monthly cap can stop
  spending entirely.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from aiohttp import ClientSession, ClientTimeout

from . import db

API = "https://openrouter.ai/api/v1"
_TIMEOUT = ClientTimeout(total=180, connect=15)
_MODELS_TTL = 24 * 3600
_MODELS_KEY = "_models_cache"

SYSTEM_PROMPT = (
    "Ты — редактор Telegram-дайджеста. На вход подаётся список постов одного канала.\n"
    "Для каждого поста верни объект:\n"
    "• i — номер поста ровно как во входных данных;\n"
    "• s — если пост помечен «П»: ОДНО законченное предложение на русском языке "
    "длиной до {limit} символов, передающее главную суть поста. "
    "Если пост помечен «Т»: тема из 2–4 слов без точки в конце;\n"
    "• ad — true, если пост является рекламой, промо, розыгрышем, партнёрским "
    "материалом или призывом подписаться на сторонний ресурс.\n\n"
    "Правила для s:\n"
    "— всегда по-русски, даже если пост на другом языке;\n"
    "— без вводных оборотов («В посте сообщается», «Автор пишет») — сразу суть;\n"
    "— без эмодзи, хештегов, ссылок и упоминаний канала;\n"
    "— конкретика вместо общих слов: кто, что, где, сколько;\n"
    "— только факты из текста поста, ничего не додумывай;\n"
    "— начинай с заглавной буквы, заканчивай точкой.\n\n"
    "Верни ровно столько объектов, сколько постов на входе, в том же порядке."
)

_SCHEMA = {
    "name": "digest",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "i": {"type": "integer"},
                        "s": {"type": "string"},
                        "ad": {"type": "boolean"},
                    },
                    "required": ["i", "s", "ad"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    },
}


class LLMError(Exception):
    pass


class BudgetExceeded(LLMError):
    pass


@dataclass(slots=True)
class ModelInfo:
    id: str
    name: str
    prompt_price: float  # USD per token
    completion_price: float
    context: int
    structured: bool
    json_object: bool
    reasoning_optional: bool

    @property
    def free(self) -> bool:
        return self.prompt_price == 0 and self.completion_price == 0

    @property
    def per_mtok(self) -> str:
        if self.free:
            return "бесплатно"
        return f"${self.prompt_price * 1e6:.2f}/${self.completion_price * 1e6:.2f} за 1M"


@dataclass(slots=True)
class Item:
    """One post handed to the model. ``full`` picks sentence vs topic mode."""

    key: str
    text: str
    full: bool


@dataclass(slots=True)
class Result:
    summary: str
    is_ad: bool


def _slim(raw: dict) -> ModelInfo | None:
    pricing = raw.get("pricing") or {}
    params = set(raw.get("supported_parameters") or ())
    reasoning = raw.get("reasoning") or {}
    try:
        return ModelInfo(
            id=raw["id"],
            name=raw.get("name") or raw["id"],
            prompt_price=float(pricing.get("prompt") or 0),
            completion_price=float(pricing.get("completion") or 0),
            context=int(raw.get("context_length") or 0),
            structured="structured_outputs" in params,
            json_object="response_format" in params,
            reasoning_optional="reasoning" in params and not reasoning.get("mandatory"),
        )
    except (KeyError, TypeError, ValueError):
        return None


class OpenRouter:
    def __init__(self, session: ClientSession) -> None:
        self._session = session
        self._models: list[ModelInfo] = []

    # -- catalogue --------------------------------------------------------- #

    @staticmethod
    def api_key() -> str:
        return (db.get("openrouter_key") or "").strip()

    async def models(self, force: bool = False) -> list[ModelInfo]:
        if self._models and not force:
            return self._models

        cached = db.get(_MODELS_KEY)
        if (
            not force
            and isinstance(cached, dict)
            and time.time() - cached.get("ts", 0) < _MODELS_TTL
        ):
            self._models = [ModelInfo(**m) for m in cached["items"]]
            return self._models

        try:
            async with self._session.get(f"{API}/models", timeout=_TIMEOUT) as resp:
                if resp.status != 200:
                    raise LLMError(f"OpenRouter вернул HTTP {resp.status} на списке моделей.")
                payload = await resp.json()
        except LLMError:
            raise
        except Exception as exc:  # network hiccup: fall back to a stale catalogue
            if isinstance(cached, dict) and cached.get("items"):
                self._models = [ModelInfo(**m) for m in cached["items"]]
                return self._models
            raise LLMError(f"Не удалось получить список моделей: {type(exc).__name__}") from exc

        models = [m for m in (_slim(raw) for raw in payload.get("data", [])) if m]
        models.sort(key=lambda m: (not m.free, m.prompt_price, m.id))
        self._models = models
        db.put(_MODELS_KEY, {"ts": int(time.time()), "items": [vars(m) for m in models]})
        return models

    async def find(self, model_id: str) -> ModelInfo | None:
        for m in await self.models():
            if m.id == model_id:
                return m
        return None

    # -- summarisation ------------------------------------------------------ #

    async def summarize(
        self,
        channel_title: str,
        items: list[Item],
        chain: list[str],
    ) -> tuple[dict[str, Result], float, str]:
        """Summarises one channel's posts in a single request.

        Returns ``(results_by_key, cost_usd, model_used)``. Models in ``chain``
        are tried in order; a model that errors, rate-limits or returns
        unusable JSON hands over to the next one.
        """
        if not items:
            return {}, 0.0, ""
        if not self.api_key():
            raise LLMError("Не задан ключ OpenRouter — добавьте его в разделе «ИИ».")

        self._check_budget()

        limit = int(db.get("sentence_max"))
        user = self._render_prompt(channel_title, items)
        max_tokens = min(8000, 90 * len(items) + 300)

        errors: list[str] = []
        for model_id in chain:
            info = await self.find(model_id)
            try:
                raw, cost = await self._call(model_id, info, user, limit, max_tokens)
            except BudgetExceeded:
                raise
            except LLMError as exc:
                errors.append(f"{model_id}: {exc}")
                continue

            parsed = _parse(raw, items)
            if parsed is None:
                errors.append(f"{model_id}: модель вернула нечитаемый JSON")
                continue
            return parsed, cost, model_id

        raise LLMError("; ".join(errors) or "Не задана ни одна модель.")

    @staticmethod
    def _check_budget() -> None:
        cap = float(db.get("budget_usd") or 0)
        if cap <= 0:
            return
        spent = db.month_cost(db.local_date().strftime("%Y-%m"))
        if spent >= cap:
            raise BudgetExceeded(f"Достигнут месячный лимит расходов: ${spent:.2f} из ${cap:.2f}.")

    @staticmethod
    def _render_prompt(channel_title: str, items: list[Item]) -> str:
        truncate = int(db.get("truncate"))
        parts = [f"Канал: {channel_title}", ""]
        for n, item in enumerate(items, 1):
            text = item.text[:truncate]
            if len(item.text) > truncate:
                text += "…"
            parts.append(f"#{n} {'П' if item.full else 'Т'}\n{text}\n")
        return "\n".join(parts)

    async def _call(
        self,
        model_id: str,
        info: ModelInfo | None,
        user: str,
        limit: int,
        max_tokens: int,
    ) -> tuple[str, float]:
        body: dict = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT.format(limit=limit)},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
        if info is None or info.structured:
            body["response_format"] = {"type": "json_schema", "json_schema": _SCHEMA}
        elif info.json_object:
            body["response_format"] = {"type": "json_object"}
        if info is not None and info.reasoning_optional:
            # Summarising a news post needs no chain of thought; reasoning
            # tokens are billed as output and would dominate the bill.
            body["reasoning"] = {"enabled": False}

        headers = {
            "Authorization": f"Bearer {self.api_key()}",
            "X-Title": "Telegram Digest Bot",
        }

        last = ""
        for attempt in range(2):
            try:
                async with self._session.post(
                    f"{API}/chat/completions", json=body, headers=headers, timeout=_TIMEOUT
                ) as resp:
                    text = await resp.text()
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        raise LLMError(
                            f"лимит запросов (429{', retry ' + retry_after if retry_after else ''})"
                        )
                    if resp.status == 402:
                        raise LLMError("на балансе OpenRouter недостаточно средств (402)")
                    if resp.status == 401:
                        raise LLMError("ключ OpenRouter отклонён (401)")
                    if resp.status >= 500:
                        last = f"HTTP {resp.status}"
                        raise _Retry
                    if resp.status != 200:
                        raise LLMError(f"HTTP {resp.status}: {text[:160]}")
                    payload = json.loads(text)
            except _Retry:
                if attempt == 0:
                    await asyncio.sleep(2)
                    continue
                raise LLMError(last) from None
            except LLMError:
                raise
            except TimeoutError:
                last = "таймаут"
                if attempt == 0:
                    continue
                raise LLMError(last) from None
            except Exception as exc:
                raise LLMError(f"{type(exc).__name__}") from exc
            break

        if payload.get("error"):
            raise LLMError(str(payload["error"])[:180])

        choices = payload.get("choices") or []
        if not choices:
            raise LLMError("пустой ответ модели")
        content = (choices[0].get("message") or {}).get("content") or ""

        usage = payload.get("usage") or {}
        cost = float(usage.get("cost") or 0)
        db.add_usage(
            db.local_date().isoformat(),
            model_id,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            cost,
        )
        return content, cost


class _Retry(Exception):
    pass


def _extract_json(raw: str) -> dict | None:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if raw.count("```") >= 2 else raw[3:]
        raw = raw.removeprefix("json").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None


def _parse(raw: str, items: list[Item]) -> dict[str, Result] | None:
    data = _extract_json(raw)
    if not isinstance(data, dict):
        return None
    rows = data.get("items")
    if not isinstance(rows, list) or not rows:
        return None

    out: dict[str, Result] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        if not 1 <= idx <= len(items):
            continue
        summary = str(row.get("s") or "").strip()
        if not summary:
            continue
        out[items[idx - 1].key] = Result(summary=summary, is_ad=bool(row.get("ad")))

    return out or None
