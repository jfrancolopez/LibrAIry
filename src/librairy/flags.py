"""Things worth knowing about a file before you move it.

These are advisories, not classifications. Nothing here changes a category, a
destination, or a confidence score — a flag exists so the owner sees "this one
deserves a look" in the review queue instead of approving it in a bulk action.

Detection is deliberately conservative and path-based. Reading file contents to
be surer would mean opening every file in the inbox, and for the one category
where that would help most — wallets and key material — opening the file is
exactly what a careful person would rather not have happen automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

HIDDEN = "hidden"
CRYPTO = "crypto"
ADULT = "adult"


@dataclass(frozen=True)
class Flag:
    kind: str
    label: str
    detail: str
    severity: str  # "info" | "warn"


# Wallet files and key material, by the names the wallets themselves use.
# Every entry here is a name a wallet picks, not a word a person might pick,
# which is what keeps this from firing on ordinary documents.
WALLET_FILENAMES = {
    "wallet.dat",  # Bitcoin Core and its forks
    "wallet.db",
    "electrum.dat",
    "default_wallet",  # Electrum
    "keystore",
    "key.dat",
    "wallet.json",
    "wallet.aes.json",  # Blockchain.info
}
WALLET_SUFFIXES = {".wallet", ".keystore", ".kdbx"}
WALLET_PATTERNS = (
    # go-ethereum keystore: UTC--2024-01-01T00-00-00.0Z--<40 hex address>
    re.compile(r"^UTC--.+--[0-9a-f]{40}$", re.I),
    # Bare 40-hex or 64-hex names are addresses and private keys.
    re.compile(r"^(0x)?[0-9a-f]{64}$", re.I),
    re.compile(r"\b(mnemonic|seed[-_ ]?phrase|recovery[-_ ]?phrase|private[-_ ]?key)\b", re.I),
)
WALLET_DIRECTORIES = re.compile(
    r"\b(wallets?|keystore|electrum|exodus|metamask|ledger|trezor|bitcoin|ethereum|monero)\b",
    re.I,
)

# Unambiguous adult-industry markers only. This is a filename heuristic and
# nothing more: it cannot see what is in a file, so it will miss plenty and
# occasionally misjudge. It marks for review; it never files or hides anything.
ADULT_TERMS = re.compile(
    r"\b(xxx|porn|hardcore|nsfw|18\+|adult[-_ ]?(video|film|movie)s?)\b",
    re.I,
)


def flags_for(relpath: str) -> tuple[Flag, ...]:
    """Advisories for one inbox path. Empty when there is nothing to say."""
    path = PurePosixPath(relpath)
    found: list[Flag] = []

    hidden = _hidden_flag(path)
    if hidden:
        found.append(hidden)

    crypto = _crypto_flag(path)
    if crypto:
        found.append(crypto)

    if ADULT_TERMS.search(relpath):
        found.append(
            Flag(
                ADULT,
                "possibly adult",
                "The name suggests adult content. This is a guess from the "
                "filename only — LibrAIry cannot see inside the file.",
                "info",
            )
        )
    return tuple(found)


def _hidden_flag(path: PurePosixPath) -> Flag | None:
    """A dot-prefixed name anywhere in the path.

    Worth surfacing because a hidden file stays hidden after it is filed, and
    the owner will not find it again by looking.
    """
    hidden_parts = [part for part in path.parts if part.startswith(".") and part not in {".", ".."}]
    if not hidden_parts:
        return None
    where = "folder" if hidden_parts[-1] != path.name else "file"
    return Flag(
        HIDDEN,
        "hidden",
        f"Hidden {where} ({hidden_parts[-1]}). It stays hidden wherever it lands "
        f"unless you rename it without the leading dot.",
        "info",
    )


def _crypto_flag(path: PurePosixPath) -> Flag | None:
    name = path.name
    lowered = name.lower()
    stem = path.stem

    if lowered in WALLET_FILENAMES or path.suffix.lower() in WALLET_SUFFIXES:
        reason = f"{name} is a name wallet software uses"
    # Both, because a geth keystore name carries dots inside it
    # (UTC--2024-01-02T03-04-05.0Z--<address>) and `stem` cuts off the address.
    elif any(pattern.search(stem) or pattern.search(name) for pattern in WALLET_PATTERNS):
        reason = f"{name} looks like key material"
    elif WALLET_DIRECTORIES.search("/".join(path.parts[:-1])) and path.suffix.lower() in {
        ".dat",
        ".json",
        ".txt",
        ".key",
        "",
    }:
        reason = f"sits in a wallet-looking folder ({path.parent.as_posix()})"
    else:
        return None

    return Flag(
        CRYPTO,
        "possible wallet",
        f"Possible crypto wallet or key material — {reason}. Check before "
        f"moving it, and remember a moved file is not a backed-up file.",
        "warn",
    )


def unhidden_name(relpath: str) -> str:
    """The same filename without its leading dot. Unchanged if not hidden."""
    path = PurePosixPath(relpath)
    return path.name[1:] if path.name.startswith(".") else path.name
