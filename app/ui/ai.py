"""AI settings: API key, live model catalogue, fallback chain, cost knobs."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, render
from ..llm import LLMError, ModelInfo, OpenRouter
from .common import Button, back, cut, kb, page_slice, pager, show

router = Router(name="ai")

FILTERS = {
    "all": "все",
    "free": "бесплатные",
    "cheap": "дешёвые",
    "so": "со structured outputs",
}


class AIInput(StatesGroup):
    key = State()
    sentence = State()
    truncate = State()
    budget = State()
    search = State()


def _matches(model: ModelInfo, kind: str, query: str) -> bool:
    if query and query not in model.id.lower() and query not in model.name.lower():
        return False
    if kind == "free":
        return model.free
    if kind == "cheap":
        return model.prompt_price <= 0.0000005  # ≤ $0.50 per 1M input tokens
    if kind == "so":
        return model.structured
    return True


async def _menu(client: OpenRouter) -> tuple[str, object]:
    chain = list(db.get("models") or [])
    lines = [
        "🧠 <b>ИИ</b>",
        "",
        f"🔑 Ключ OpenRouter: {'задан ✅' if client.api_key() else 'не задан ❌'}",
        "",
        "<b>Цепочка моделей</b>",
    ]
    if chain:
        for index, model_id in enumerate(chain, 1):
            info = await client.find(model_id)
            price = f" — {info.per_mtok}" if info else ""
            flag = "" if info is None or info.structured else " ⚠️ без strict-JSON"
            lines.append(f"{index}. <code>{render.esc(model_id)}</code>{price}{flag}")
        lines.append("<i>Следующая используется, если предыдущая недоступна.</i>")
    else:
        lines.append("<i>пусто — дайджест не соберётся</i>")

    budget = float(db.get("budget_usd") or 0)
    lines += [
        "",
        f"📏 Длина предложения: {db.get('sentence_max')} символов",
        f"✂️ Обрезка поста для модели: {db.get('truncate')} символов",
        f"💰 Лимит расходов в месяц: {f'${budget:.2f}' if budget else 'без лимита'}",
        f"🩳 Короткие посты без ИИ: до {db.get('short_verbatim')} символов",
    ]

    rows = [
        [
            Button(text="🔑 Ключ", callback_data="ai|key"),
            Button(text="➕ Добавить модель", callback_data="ai|list|all|0"),
        ],
        [
            Button(text="⚡ Быстрый старт", callback_data="ai|quick"),
            Button(text="🧹 Очистить цепочку", callback_data="ai|clear"),
        ],
    ]
    if chain:
        rows.append(
            [
                Button(text=f"➖ {cut(m, 22)}", callback_data=f"ai|rm|{i}")
                for i, m in enumerate(chain[:3])
            ]
        )
    rows += [
        [
            Button(text="📏 Длина", callback_data="ai|sentence"),
            Button(text="✂️ Обрезка", callback_data="ai|truncate"),
            Button(text="💰 Лимит", callback_data="ai|budget"),
        ],
        [
            Button(text="🗑 Очистить кэш саммари", callback_data="ai|cache"),
            Button(text="🔄 Обновить каталог", callback_data="ai|refresh"),
        ],
        [back()],
    ]
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data == "ai")
async def ai_menu(call: CallbackQuery, state: FSMContext, openrouter: OpenRouter) -> None:
    await state.clear()
    await show(call, *(await _menu(openrouter)))


# --------------------------------------------------------------------------- #
# model picker
# --------------------------------------------------------------------------- #


@router.callback_query(F.data.startswith("ai|list|"))
async def model_list(call: CallbackQuery, state: FSMContext, openrouter: OpenRouter) -> None:
    _, _, kind, page = call.data.split("|")
    data = await state.get_data()
    query = str(data.get("model_query", "")).lower()

    try:
        models = await openrouter.models()
    except LLMError as exc:
        await show(call, f"⚠️ {render.esc(str(exc))}", kb([back("ai")]))
        return

    found = [m for m in models if _matches(m, kind, query)]
    rows, page, total_pages = page_slice(found, int(page), per=6)

    buttons = [
        [
            Button(
                text=f"{'🆓' if m.free else '💵'} {cut(m.id, 34)} · {m.per_mtok}",
                callback_data=f"ai|set|{m.id[:58]}",
            )
        ]
        for m in rows
    ]
    filter_row = [
        Button(
            text=("• " if kind == key else "") + label,
            callback_data=f"ai|list|{key}|0",
        )
        for key, label in FILTERS.items()
    ]

    text = (
        f"🧠 <b>Модели OpenRouter</b> — найдено {len(found)} из {len(models)}\n"
        f"Фильтр: {FILTERS[kind]}" + (f", поиск «{render.esc(query)}»" if query else "") + "\n\n"
        "🆓 — бесплатная (лимит 20 запросов/мин и 50/сутки, поэтому держите её "
        "запасной, а не основной).\nЦены указаны за 1M токенов вход/выход."
    )
    await show(
        call,
        text,
        kb(
            *buttons,
            pager(f"ai|list|{kind}|", page, total_pages),
            filter_row[:2],
            filter_row[2:],
            [
                Button(text="🔎 Поиск", callback_data="ai|search"),
                Button(text="🧽 Сбросить поиск", callback_data="ai|noquery"),
            ],
            [back("ai")],
        ),
    )


@router.callback_query(F.data.startswith("g|mdl|"))
async def group_model_pick(call: CallbackQuery, state: FSMContext, openrouter: OpenRouter) -> None:
    """Opens the shared picker, pointed at one group instead of the global chain."""
    group_id = int(call.data.rsplit("|", 1)[1])
    group = db.group(group_id)
    if group is None:
        await call.answer("Группа удалена.", show_alert=True)
        return
    if group["model"]:
        db.update_group(group_id, model="")
        call.data = f"g|v|{group_id}"
        from .groups import group_view

        await group_view(call)
        await call.answer("Переопределение снято — используется общая цепочка.")
        return

    await state.update_data(pick_target=f"g:{group_id}", model_query="")
    call.data = "ai|list|all|0"
    await model_list(call, state, openrouter)


@router.callback_query(F.data == "ai|search")
async def model_search_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AIInput.search)
    await show(
        call,
        "🔎 Пришлите часть названия модели.\n\nНапример: <code>gemini</code>, "
        "<code>gpt-oss</code>, <code>qwen</code>.",
        kb([back("ai|list|all|0", "⬅️ Отмена")]),
    )


@router.message(AIInput.search)
async def model_search(message: Message, state: FSMContext, openrouter: OpenRouter) -> None:
    await state.set_state(None)
    await state.update_data(model_query=(message.text or "").strip().lower()[:40])
    await message.answer(
        "Готово, фильтр применён.",
        reply_markup=kb([Button(text="К списку моделей", callback_data="ai|list|all|0")]),
    )


@router.callback_query(F.data == "ai|noquery")
async def model_clear_query(call: CallbackQuery, state: FSMContext, openrouter: OpenRouter) -> None:
    await state.update_data(model_query="")
    call.data = "ai|list|all|0"
    await model_list(call, state, openrouter)


@router.callback_query(F.data.startswith("ai|set|"))
async def model_set(call: CallbackQuery, state: FSMContext, openrouter: OpenRouter) -> None:
    prefix = call.data.split("|", 2)[2]
    models = await openrouter.models()
    model = next((m for m in models if m.id == prefix), None) or next(
        (m for m in models if m.id.startswith(prefix)), None
    )
    if model is None:
        await call.answer("Модель не найдена в каталоге.", show_alert=True)
        return

    data = await state.get_data()
    target = str(data.get("pick_target", "global"))
    if target.startswith("g:"):
        group_id = int(target[2:])
        db.update_group(group_id, model=model.id)
        await state.update_data(pick_target="global")
        call.data = f"g|v|{group_id}"
        from .groups import group_view

        await group_view(call)
        return

    chain = list(db.get("models") or [])
    if model.id in chain:
        await call.answer("Уже в цепочке.", show_alert=True)
        return
    chain.append(model.id)
    db.put("models", chain[:4])
    await show(call, *(await _menu(openrouter)), alert=f"Добавлено: {model.id}")


@router.callback_query(F.data.startswith("ai|rm|"))
async def model_remove(call: CallbackQuery, openrouter: OpenRouter) -> None:
    index = int(call.data.rsplit("|", 1)[1])
    chain = list(db.get("models") or [])
    if 0 <= index < len(chain):
        chain.pop(index)
        db.put("models", chain)
    await show(call, *(await _menu(openrouter)))


@router.callback_query(F.data == "ai|clear")
async def model_clear(call: CallbackQuery, openrouter: OpenRouter) -> None:
    db.put("models", [])
    await show(call, *(await _menu(openrouter)), alert="Цепочка очищена")


@router.callback_query(F.data == "ai|quick")
async def quick_start(call: CallbackQuery, openrouter: OpenRouter) -> None:
    """Builds a chain from the live catalogue rather than hardcoded ids."""
    try:
        models = await openrouter.models(force=True)
    except LLMError as exc:
        await show(call, f"⚠️ {render.esc(str(exc))}", kb([back("ai")]))
        return

    usable = [m for m in models if m.structured and m.context >= 32000]
    paid = sorted(
        (m for m in usable if not m.free), key=lambda m: m.prompt_price + m.completion_price
    )
    free = [m for m in usable if m.free]

    chain = [m.id for m in paid[:1]] + [m.id for m in free[:1]]
    if not chain:
        await call.answer("В каталоге не нашлось подходящих моделей.", show_alert=True)
        return

    db.put("models", chain)
    await show(
        call,
        *(await _menu(openrouter)),
        alert="Цепочка собрана из актуального каталога. Проверьте качество и при "
        "желании поставьте модель поумнее.",
    )


@router.callback_query(F.data == "ai|refresh")
async def refresh_catalogue(call: CallbackQuery, openrouter: OpenRouter) -> None:
    try:
        models = await openrouter.models(force=True)
    except LLMError as exc:
        await call.answer(str(exc)[:180], show_alert=True)
        return
    await show(call, *(await _menu(openrouter)), alert=f"Каталог обновлён: {len(models)} моделей")


@router.callback_query(F.data == "ai|cache")
async def clear_cache(call: CallbackQuery, openrouter: OpenRouter) -> None:
    removed = db.clear_summary_cache()
    await show(call, *(await _menu(openrouter)), alert=f"Удалено записей: {removed}")


# --------------------------------------------------------------------------- #
# scalar inputs
# --------------------------------------------------------------------------- #

_PROMPTS = {
    "key": (
        AIInput.key,
        (
            "🔑 <b>Ключ OpenRouter</b>\n\nСоздайте ключ на openrouter.ai/keys и пришлите "
            "его сюда. Сообщение с ключом бот удалит сразу после сохранения."
        ),
    ),
    "sentence": (
        AIInput.sentence,
        (
            "📏 <b>Длина предложения</b>\n\nМаксимум символов в одной строке дайджеста. "
            "Пришлите число от 60 до 300."
        ),
    ),
    "truncate": (
        AIInput.truncate,
        (
            "✂️ <b>Обрезка поста</b>\n\nСколько символов поста уходит в модель. Меньше "
            "значение — дешевле запрос, но у длинных постов теряется хвост. "
            "Пришлите число от 200 до 6000."
        ),
    ),
    "budget": (
        AIInput.budget,
        (
            "💰 <b>Лимит расходов</b>\n\nПри достижении суммы за календарный месяц бот "
            "перестанет обращаться к ИИ. Пришлите сумму в долларах, например "
            "<code>2.5</code>, или <code>0</code> — снять лимит."
        ),
    ),
}


@router.callback_query(F.data.in_({f"ai|{name}" for name in _PROMPTS}))
async def ai_prompt(call: CallbackQuery, state: FSMContext) -> None:
    name = call.data.split("|")[1]
    target, text = _PROMPTS[name]
    await state.set_state(target)
    await show(call, text, kb([back("ai", "⬅️ Отмена")]))


@router.message(AIInput.key)
async def set_key(message: Message, state: FSMContext, openrouter: OpenRouter) -> None:
    key = (message.text or "").strip()
    await state.clear()
    if not key.startswith("sk-or-"):
        await message.answer(
            "Ключ OpenRouter начинается с <code>sk-or-</code>. Попробуйте ещё раз."
        )
        return
    db.put("openrouter_key", key)
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(
        "✅ Ключ сохранён, сообщение с ним удалено.",
        reply_markup=kb([Button(text="🧠 К настройкам ИИ", callback_data="ai")]),
    )


async def _numeric(
    message: Message, state: FSMContext, key: str, low: float, high: float, integer: bool
) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        await message.answer(f"Нужно число от {low:g} до {high:g}.")
        return
    if not low <= value <= high:
        await message.answer(f"Нужно число от {low:g} до {high:g}.")
        return
    await state.clear()
    db.put(key, int(value) if integer else round(value, 2))
    await message.answer(
        "✅ Сохранено.", reply_markup=kb([Button(text="🧠 К настройкам ИИ", callback_data="ai")])
    )


@router.message(AIInput.sentence)
async def set_sentence(message: Message, state: FSMContext) -> None:
    await _numeric(message, state, "sentence_max", 60, 300, True)


@router.message(AIInput.truncate)
async def set_truncate(message: Message, state: FSMContext) -> None:
    await _numeric(message, state, "truncate", 200, 6000, True)


@router.message(AIInput.budget)
async def set_budget(message: Message, state: FSMContext) -> None:
    await _numeric(message, state, "budget_usd", 0, 1000, False)
