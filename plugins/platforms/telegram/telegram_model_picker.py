"""Small Telegram model-picker callback helpers."""
import logging

from agent.i18n import t

logger = logging.getLogger(__name__)


def provider_get_label():
    try:
        from hermes_cli.providers import get_label
    except ImportError:
        def get_label(slug):
            return slug
    return get_label


async def parse_page(query, raw: str) -> int | None:
    from plugins.platforms.telegram.adapter import _toast

    try:
        return int(raw)
    except ValueError:
        await query.answer(text=_toast("platform.telegram.picker.invalid_page"))
        return None


async def save_global_model(adapter, query, chat_id: str) -> None:
    from plugins.platforms.telegram.adapter import _toast

    pending = adapter._model_global_pending.pop(chat_id, None)
    if not pending:
        await query.answer(text=_toast("platform.telegram.picker.expired_model"))
        return
    try:
        result_text = await pending["callback"](
            chat_id, pending["model_id"], pending["provider_slug"], persist_global=True)
    except Exception:
        logger.exception("Model picker global save failed")
        await query.answer(text=_toast("platform.telegram.picker.switch_failed"))
        return
    await adapter._edit_result_text(query, result_text + t("platform.telegram.picker.saved_global"))
    await query.answer()
