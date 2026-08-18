"""Chat-based registration (DM only). Sender's own number is the credential:
users can only create/overwrite/remove their own row.

    register <provider> <model> <api_key>
    remove
    help
"""
import db
from llm import ALLOWED_PROVIDERS

HELP_MSG = (
    "Commands (DM only):\n"
    "register <provider> <model> <api_key>\n"
    "remove\n\n"
    f"Providers: {', '.join(sorted(ALLOWED_PROVIDERS))}\n"
    "Example:\nregister huggingface mistralai/Mistral-7B-Instruct-v0.3 hf_xxxx"
)

GROUP_REGISTER_MSG = (
    "⚠️ Never send register commands in a group — your API key was just visible "
    "to all members. Revoke that key with your provider and register a new one "
    "by messaging me directly."
)
GROUP_COMMAND_MSG = "Please message me directly (DM) to manage your registration."

REGISTERED_MSG = "Registered with provider '{provider}', model '{model}'. You can now ask questions."
REMOVED_MSG = "Your registration has been removed."
NOT_FOUND_MSG = "No registration found for your number."

_COMMANDS = frozenset({"register", "remove", "help"})


def is_command(text: str) -> bool:
    first = text.split(maxsplit=1)[0].lower() if text else ""
    return first in _COMMANDS


async def handle_command(phone: str, text: str, is_group: bool) -> str:
    """Returns the reply. Caller guarantees is_command(text). `text` may
    contain an API key — caller must never log it; nothing here echoes it."""
    parts = text.split()
    cmd = parts[0].lower()

    if is_group:
        return GROUP_REGISTER_MSG if cmd == "register" else GROUP_COMMAND_MSG

    if cmd == "help":
        return HELP_MSG

    if cmd == "remove":
        deleted = await db.delete_user_by_phone(phone)
        return REMOVED_MSG if deleted else NOT_FOUND_MSG

    # register
    if len(parts) != 4:
        return "Wrong format.\n\n" + HELP_MSG
    _, provider, model, api_key = parts
    provider = provider.lower()
    if provider not in ALLOWED_PROVIDERS:
        return f"Provider '{provider}' is not supported.\n\n" + HELP_MSG
    if len(api_key) < 8:
        return "That API key looks too short. Check it and try again."
    await db.upsert_user_llm_config(phone, api_key, provider, model)
    return REGISTERED_MSG.format(provider=provider, model=model)