# CommandCode Proxy

A fix for 9router's broken CommandCode provider integration. Translates between OpenAI-compatible and CommandCode-native API formats, forcing `stream=true` (which CommandCode requires unconditionally).

**Problem:** 9router's CommandCode handler fails on all non-streaming requests — 58.3% failure rate observed. The handler's `transformRequest` override is bypassed by the format translator, leaving `stream=false` which CommandCode's `/alpha/generate` API rejects with `400 BAD_REQUEST` or `ECONNRESET`.

**Solution:** A lightweight Python proxy (stdlib only, ~400 lines) that sits between 9router and CommandCode's API, forcing correct format and translating responses.

## Architecture

```
Hermes → 9router (port 20128) → proxy (port 20129) → api.commandcode.ai/alpha/generate
                                       │
                                  Forces stream=true
                                  Translates tool format
                                  Native-format pass-through
```

## Quick Start

### 1. Install

```bash
# Clone
git clone https://github.com/alfaatihs/commandcode-proxy.git
cd commandcode-proxy

# No dependencies — Python stdlib only

# Install systemd service
mkdir -p ~/.config/systemd/user
cp commandcode-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now commandcode-proxy.service
```

The proxy reads the CommandCode API key automatically from 9router's database (`~/.9router/db/data.sqlite`). No environment variables needed.

### 2. Patch 9router

9router has the CommandCode URL hardcoded in compiled JavaScript. Redirect it to the proxy:

```bash
sudo sed -i 's|https://api.commandcode.ai/alpha/generate|http://127.0.0.1:20129/alpha/generate|g' \
  /usr/local/lib/node_modules/9router/app/.next-cli-build/server/chunks/2276.js \
  /usr/local/lib/node_modules/9router/app/.next-cli-build/server/chunks/5079.js

systemctl --user restart 9router.service
```

### 3. Verify

```bash
# Proxy health
curl http://127.0.0.1:20129/health
# → {"status": "ok", "provider": "commandcode-proxy"}

# Models available
curl http://127.0.0.1:20129/v1/models

# Test through 9router
curl http://localhost:20128/v1/chat/completions \
  -H "Authorization: Bearer <your-9router-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"cmc/deepseek/deepseek-v4-flash","messages":[{"role":"user","content":"Say hi"}],"stream":false}'
```

## What It Fixes

| Before | After |
|--------|-------|
| 58.3% failure rate (84/144 requests) | 0% failure rate |
| `stream=false` → 400 / ECONNRESET | Forced `stream=true` |
| OpenAI tool format rejected by CC API | Auto-translated to CC format |
| Errors leak into conversation as `[CommandCode error: ...]` | Clean responses |
| Recursive error pollution loop | Broken |

## 9router Update Survival

`npm update -g 9router` wipes the chunk patches. Re-apply:

```bash
sudo sed -i 's|https://api.commandcode.ai/alpha/generate|http://127.0.0.1:20129/alpha/generate|g' \
  /usr/local/lib/node_modules/9router/app/.next-cli-build/server/chunks/2276.js \
  /usr/local/lib/node_modules/9router/app/.next-cli-build/server/chunks/5079.js
systemctl --user restart 9router.service
```

Verify the patch is intact:
```bash
grep "api.commandcode.ai" /usr/local/lib/node_modules/9router/app/.next-cli-build/server/chunks/2276.js
# Should return nothing
```

## Supported Models

11 models through CommandCode, all verified:

| Model | Basic | Tools | Streaming |
|-------|-------|-------|-----------|
| cmc/deepseek/deepseek-v4-pro | ✓ | ✓ | ✓ |
| cmc/deepseek/deepseek-v4-flash | ✓ | ✓ | ✓ |
| cmc/Qwen/Qwen3.6-Plus | ✓ | ✓ | ✓ |
| cmc/Qwen/Qwen3.6-Max-Preview | ✓ | ✓ | ✓ |
| cmc/MiniMaxAI/MiniMax-M2.7 | ✓ | ✓ | ✓ |
| cmc/MiniMaxAI/MiniMax-M2.5 | ✓ | ✓ | ✓ |
| cmc/moonshotai/Kimi-K2.6 | ✓ | ✓ | ✓ |
| cmc/moonshotai/Kimi-K2.5 | ✓ | ✓ | ✓ |
| cmc/zai-org/GLM-5.1 | ✓ | ✓ | ✓ |
| cmc/zai-org/GLM-5 | ✓ | ✓ | ✓ |
| cmc/stepfun/Step-3.5-Flash | ✓ | ✓ | ✓ |

## Endpoints

| Path | Method | Description |
|------|--------|-------------|
| `/v1/models` | GET | OpenAI-compatible model list |
| Any path | POST | Proxy to CommandCode `/alpha/generate` |
| `/health` | GET | Health check |

## Upstream

- **Bug report:** [decolua/9router#1840](https://github.com/decolua/9router/issues/1840)
- **Root cause:** 9router's format translator bypasses `transformRequest`, leaving `stream=false` on non-streaming requests. CommandCode API requires `stream=true`.

## License

MIT
