# Optional LLM assistance (local and cloud)

`stock_gplm.llm` is an opt-in, provider-agnostic chat client. It can help with research
notes, feature proposals, explanations and experiment ideas. A reply is an `Advisory`
with `authority="ADVISORY_ONLY"`. The training, validation, gating and ranking packages
never import it, and a test enforces that, so a reply cannot change a gate, split, label,
metric, ranking or risk rule. M3.5 does not need it.

## Configuration

Copy `config/llm/providers.example.json`, then enable the file and each provider
explicitly. The shipped example is disabled.

| Provider kind | Endpoint | Key |
| --- | --- | --- |
| `ollama` | `<base_url>/api/chat` on the local machine | None |
| `openai_compatible` | `<base_url>/chat/completions`, e.g. OpenRouter | Environment variable named by `api_key_env` |

A config file that contains an `api_key` is rejected. Only models listed for a provider
may be called, and each one's per-1k-token prices drive the spending estimate.

## Controls applied before any byte is sent

| Control | Behaviour |
| --- | --- |
| Enabled flags | Global and per-provider; off by default |
| Redaction | Private keys, bearer tokens, API keys, `*KEY/TOKEN/SECRET/PASSWORD/TOTP/PIN=` assignments, emails, local paths and long hex strings become typed placeholders |
| Market-data guard | A prompt with more date-plus-numbers rows than `max_market_rows_remote` (default 0) goes to a remote provider only with `approved_data_upload=True`; local Ollama is exempt |
| Budgets | The daily token limit and daily USD limit are checked against the worst case, prompt plus `max_output_tokens`, using that day's audit log |
| Audit | Append-only JSONL: time, provider, model, purpose, prompt and response SHA-256, redaction counts, tokens, estimated cost and outcome, including refusals. Prompt text is not stored |

Cost figures are estimates from configured prices, not provider invoices. No test opens
a network connection; all of them use a fake transport.

## Research provider registry (M5.4)

`config/research/providers.json` and `stock_gplm.llm.registry` add MWAPI and StartupAPI (Anthropic-compatible), OpenRouter, Ollama, OpenAI, Anthropic and a future Gemini adapter, all disabled and offline by default. Keys load from a 0600 `.env` via `stock_gplm.llm.envfile`. `gplm providers check` reports key presence only. See `m54-behaviour-discovery.md`.
