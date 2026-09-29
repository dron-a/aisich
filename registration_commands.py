"""Chat-based registration (DM only). Sender's own number is the credential:
users can only create/overwrite/remove their own row.

    register <provider> <model> <api_key>
    remove
    help
"""
import db
from llm import ALLOWED_PROVIDERS
from config import settings

HELP_MSG = (
    "Commands (DM only):\n"
    "To register remove your own LLM provider and model, use:\n"
    "register <provider> <model> <api_key>\n"
    "remove\n\n"
    f"Providers: {', '.join(sorted(ALLOWED_PROVIDERS))}\n"
    "Example:\nregister huggingface mistralai/Mistral-7B-Instruct-v0.3 hf_xxxx\n\n"
    "To sync your Taxila account follow these steps:\n"
    f"1. download and set up the chrome extension from {settings.taxila_chrome_extension}\n"
    "2. Open taxila click your account icon on the right upper corner and click profile\n"
    "3. Scroll down to the Mobil App section and click on view QR Code, a sync taxila button will appear\n"
    "4. Click on the sync taxila button and use the QR code or the options on the screen to send and register the token and userid\n"
)

LLM_REGISTER_HELP_MSG = (
    "To register remove your own LLM provider and model, use:\n"
    "register <provider> <model> <api_key>\n"
    "remove\n\n"
    f"Providers: {', '.join(sorted(ALLOWED_PROVIDERS))}\n"
    "Example:\nregister huggingface mistralai/Mistral-7B-Instruct-v0.3 hf_xxxx\n\n"
)

TAXILA_HELP_MSG =(
    "To sync your Taxila account follow these steps:\n"
    f"1. download and set up the chrome extension from {settings.taxila_chrome_extension}\n"
    "2. Open taxila click your account icon on the right upper corner and click profile\n"
    "3. Scroll down to the Mobil App section and click on view QR Code, a sync taxila button will appear\n"
    "4. Click on the sync taxila button and use the QR code or the options on the screen to send and register the token and userid\n"
)

MSTEAMS_HELP_MSG =(
    'Reach out to the admin to get the correct format for syncing Microsoft Teams account.'
)

GROUP_REGISTER_MSG = (
    "⚠️ Never send register commands or Sync taxila commands in a group — and risk exposing your API/registrations key "
    "to all members.  "
    "Please message help in DM directly."
)
GROUP_COMMAND_MSG = (
    "Please message me directly (DM) if you are trying to create/manage your BYOK registration or sync your taxila account.\n"
    "If you are trying to call a worflow use format @<my tag> function_name <your message>.\n"
    "*Available workflows*\n1. echo (set|update|remove) - setup/manage reminders (needs a title for set)\n2. engrave - register context\n3. enquire - ask questions about reminders or courses content from taxila(needs one time registration)\n"
    "Note: A valid echo title should be an unrepeated single text, you can use a hyphen (-) to join words."
)
GROUP_COMMAND_MSG2 = (
    "Please message me directly (DM) if you are trying to create/manage your BYOK registration."
    "If you are trying to set up a reminder use format @<my tag> echo <you reminder msg with a valid echo title>"
    "If you are trying to update a reminder use format @<my tag> echo <your update msg with the same valid echo title>"
    "If you want to enquire about a reminder use format @<my tag> echo:enquire <your update msg with or without the same valid echo title>"
    "if you are trying to register a context use format @<my tag> engrave <your context>"
    "A valid echo title is necessary to create update a reminder and should be an unrepeated single text,"
    "you can use a hyphen (-) to create unique title"
)

REGISTERED_MSG = "Registered with provider '{provider}', model '{model}'. You can now ask questions."
REMOVED_MSG = "Your registration has been removed."
NOT_FOUND_MSG = "No registration found for your number."

_COMMANDS = frozenset({"register", "remove", "help", "!sync_taxila"})


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
    if cmd == "register":
        if len(parts) != 4:
            return "Wrong format.\n\n" + LLM_REGISTER_HELP_MSG
        _, provider, model, api_key = parts
        provider = provider.lower()
        if provider not in ALLOWED_PROVIDERS:
            return f"Provider '{provider}' is not supported.\n\n" + HELP_MSG
        if len(api_key) < 8:
            return "That API key looks too short. Check it and try again."
        await db.upsert_user_llm_config(phone, api_key, provider, model)
        return REGISTERED_MSG.format(provider=provider, model=model)

    if cmd == "!sync_taxila":
        if len(parts) != 3:
            return "Wrong format.\n\n" + TAXILA_HELP_MSG
        _, token, userid = parts
        if len(token) < 8 or len(userid) < 4:
            return "That token or userid looks too short. Check it and try again."
        await db.upsert_user_taxila_config(phone, token, userid)
        return "Taxila account synced successfully."

    if cmd == "!sync_msteams":
        if len(parts) != 3:
            return "Wrong format.\n\n" + MSTEAMS_HELP_MSG
        _, token, userid = parts
        if len(token) < 8 or len(userid) < 4:
            return "That token or userid looks too short. Check it and try again."
        await db.upsert_user_msteams_config(phone, token, userid)
        return "Microsoft Teams account synced successfully."