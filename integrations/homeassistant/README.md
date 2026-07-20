# Home Assistant Integration

This directory contains the Home Assistant integration for engram, enabling voice assistants to store and recall memories via semantic search.

## Components

- **`pyscript/ha_semantic_memory.py`** — Pyscript thin client that runs inside HAOS and calls the engram API over HTTP
- **`blueprints/memory_tool.yaml`** — Blueprint that exposes memory operations as `script.memory_tool` for LLM tool calling

## Prerequisites

- [Pyscript](https://github.com/custom-components/pyscript) HACS integration installed
- engram server running and reachable from HAOS (same LAN subnet)
- An LLM conversation agent configured (e.g., Ollama with GLM-4.7-Flash)

## Setup

### 1. Deploy the Pyscript client

Copy `pyscript/ha_semantic_memory.py` to your HAOS Pyscript directory:

```
/config/pyscript/ha_semantic_memory.py
```

**Edit the file** and set `BACKEND_URL` to your engram server's LAN IP:

```python
BACKEND_URL = "http://192.0.2.10:8920"
```

If you have `ENGRAM_API_TOKEN` set on the server, also set `BACKEND_TOKEN` to match.

### 2. Import the Blueprint

Import `blueprints/memory_tool.yaml` as a Script Blueprint in HA:

1. Go to **Settings > Automations & Scenes > Blueprints**
2. Import from file or paste the YAML
3. Create a script from the blueprint (it will be exposed as `script.memory_tool`)

### 3. Configure your LLM agent

Add the memory tool instructions to your conversation agent's system prompt. See [docs/archive/SYSTEM_PROMPT.md](../../docs/archive/SYSTEM_PROMPT.md) for the recommended prompt.

Key points:
- Expose `script.memory_tool` to the agent in **Settings > Voice Assistants > Exposed Entities**
- Keep exposed entities minimal for best tool-calling accuracy
- The prompt's field names must match your deployed blueprint (`action` or `operation`)

### 4. Verify

Ask your voice assistant: "Remember that my favorite color is blue"

Then in a new conversation: "What is my favorite color?"

If both work, the integration is complete.

## Gotchas

- The blueprint uses `action` as the field name. If your live deployment uses `operation`, update the blueprint or prompt to match.
- `@service` decorators use `supports_response="optional"` — required for HA 2024.10+ blueprint `response_variable` support.
- LLMs may send `tags` as a list; the Pyscript client normalizes this to a comma-separated string.
