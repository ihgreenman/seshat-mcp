# Credits

## Third-party material included in this repository

**BIP-39 English wordlist** — `src/seshat/bip39_english.txt`

The 2048-word English wordlist from BIP-0039 (Mnemonic code for generating
deterministic keys), by Marek Palatinus, Pavol Rusnak, Aaron Voisine and Sean
Bowe. Taken verbatim from `bip-0039/english.txt` in
[bitcoin/bips](https://github.com/bitcoin/bips). BIP-0039 is published under the
MIT License.

Used here only as a source of memorable, prefix-unique, non-confusable words for
note identifiers — no cryptographic or key-derivation use. See spec §6.1 for why
this list rather than the EFF diceware lists.

## Runtime dependencies

- **`mcp`** — the official Model Context Protocol Python SDK, by Anthropic. MIT.
- **`pydantic`** — Samuel Colvin and contributors. MIT.

## Design and implementation

The design in `docs/seshat-mcp-spec.md` emerged from a design conversation
between Ian Greenhoe and Claude (Opus 5), September 2026; its authorship note is
recorded in the document itself.

This implementation was written by Claude (Opus 5) against that specification,
September 2026.
