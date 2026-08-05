"""Where to get a key for each cloud AI provider, and what it will cost.

The catalog cards in Settings answer "what is this and how do I turn it on".
Cloud AI needs the same treatment, with two differences worth being blunt
about on the page: these providers charge money, and using one sends file
metadata off this machine.

`Step` is shared with the catalog registry — an instruction with a link is the
same thing whether the destination is TMDB or OpenAI.
"""

from __future__ import annotations

from dataclasses import dataclass

from librairy.catalogs import Step


@dataclass(frozen=True)
class ProviderInfo:
    kind: str
    name: str
    #  What it is good at, in one line.
    summary: str
    cost: str
    key_field: str
    env_var: str
    signup_url: str
    steps: tuple[Step, ...]
    #  Shown as a warning where the honest answer is awkward. Empty for most.
    caveat: str = ""


# Owner asked (2026-07-23) for "authenticate with OpenAI via browser". As of
# 2026-08 there is still no public OAuth program that issues API keys to
# third-party self-hosted applications, and the alternatives — embedding
# someone else's client credentials, or scripting a login — are respectively
# dishonest and a good way to get an account banned. LibrAIry asks for a key
# and says why. Revisit if OpenAI opens a public program.
OPENAI_NO_OAUTH = (
    "LibrAIry asks for an API key rather than signing you in through your "
    "browser. OpenAI has no public sign-in flow that issues API keys to "
    "self-hosted apps like this one, and faking one would mean either "
    "borrowing another application's credentials or driving a login page on "
    "your behalf. A key you can see and revoke is the honest option."
)

AI_PROVIDERS: tuple[ProviderInfo, ...] = (
    ProviderInfo(
        kind="openai",
        name="OpenAI",
        summary="Strong general classification; the usual default for cloud AI.",
        cost="Pay per use — you are billed by OpenAI, not by LibrAIry.",
        key_field="openai",
        env_var="OPENAI_API_KEY",
        signup_url="https://platform.openai.com/api-keys",
        steps=(
            Step(
                "Create an OpenAI platform account. This is separate from a "
                "ChatGPT subscription — a paid ChatGPT plan does not include "
                "API access.",
                "https://platform.openai.com/signup",
                "Create an account",
            ),
            Step(
                "Add a payment method and, ideally, a monthly spend limit. The "
                "API has no free tier.",
                "https://platform.openai.com/settings/organization/billing",
                "Billing settings",
            ),
            Step(
                'Create a secret key. It is shown once — copy it before closing '
                "the dialog.",
                "https://platform.openai.com/api-keys",
                "Create a secret key",
            ),
            Step("Paste it below, then enable the provider by typing CLOUD."),
        ),
        caveat=OPENAI_NO_OAUTH,
    ),
    ProviderInfo(
        kind="anthropic",
        name="Anthropic",
        summary="Careful with ambiguous files; good at explaining its reasoning.",
        cost="Pay per use — you are billed by Anthropic, not by LibrAIry.",
        key_field="anthropic",
        env_var="ANTHROPIC_API_KEY",
        signup_url="https://console.anthropic.com/settings/keys",
        steps=(
            Step(
                "Create an Anthropic Console account. Separate from a Claude.ai "
                "subscription, which does not include API access.",
                "https://console.anthropic.com/login",
                "Create an account",
            ),
            Step(
                "Buy some credit. The API is prepaid, so there is no surprise bill.",
                "https://console.anthropic.com/settings/billing",
                "Billing settings",
            ),
            Step(
                "Create an API key. It is shown once — copy it before closing "
                "the dialog.",
                "https://console.anthropic.com/settings/keys",
                "Create an API key",
            ),
            Step("Paste it below, then enable the provider by typing CLOUD."),
        ),
    ),
    ProviderInfo(
        kind="gemini",
        name="Google Gemini",
        summary="Has a free tier, which makes it the cheapest way to try cloud AI.",
        cost="Free tier available, with rate limits; paid tiers beyond it.",
        key_field="gemini",
        env_var="GEMINI_API_KEY",
        signup_url="https://aistudio.google.com/apikey",
        steps=(
            Step(
                "Sign in to Google AI Studio with a Google account.",
                "https://aistudio.google.com",
                "Open AI Studio",
            ),
            Step(
                'Press "Create API key". The free tier needs no payment method.',
                "https://aistudio.google.com/apikey",
                "Create an API key",
            ),
            Step("Paste it below, then enable the provider by typing CLOUD."),
        ),
    ),
)

AI_PROVIDERS_BY_KIND = {provider.kind: provider for provider in AI_PROVIDERS}
