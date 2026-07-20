> **ARCHIVED (2026-07-20).** From the local-models-only era: a comparison of local LLM tool-calling reliability (Ollama-served models). engram itself no longer depends on any local LLM (embeddings run in-process; chat models are the operator's choice — in practice, frontier models). Kept for historical reference; not maintained.

# LLM Model Selection for Memory Tool Calling

Your choice of local LLM matters **enormously** for agentic memory. The memory tool relies on the LLM making actual tool calls — not just generating text about remembering things. We tested multiple models and found dramatic differences in tool calling reliability.

## The Core Issue

When an LLM has access to tools (like a memory store), it must decide when to call tools and generate structured function calls. This works differently than just generating text — the model must output a `tool_calls` response instead of a `content` response.

There are three levels of tool calling difficulty:

1. **Explicit** — User says "remember that my name is Robert" -> model calls `memory_tool(operation=set)`
2. **Proactive** — User says "I live in Portland" -> model should detect this is a personal fact and store it without being asked
3. **Recall** — User says "Where do I live?" -> model should search memory before answering, even though it could guess

Most models handle level 1. Levels 2 and 3 are where models diverge sharply.

## Models Tested

### Qwen3-30B-A3B (Q4_K_S) — NOT RECOMMENDED

- **Size:** 14.4 GB VRAM (MoE, 30B total / 3B active params)
- **Quantization:** Q4_K_S (3.92 bpw)
- **Source:** `hf.co/byteshape/Qwen3-30B-A3B-Instruct-2507-GGUF`

#### What happened

Qwen3-30B-A3B handles explicit "remember X" commands correctly — it generates proper tool calls. But for proactive scenarios, it consistently generates **text about calling tools** instead of actually calling them:

```
User: "I live in Portland Oregon"
Expected: tool_call to memory_tool(operation=set, key=user_location, value=Portland Oregon)
Actual: "Got it, I'll remember that you live in Portland, Oregon!"
         (tool_calls=None in every response chunk)
```

The model "understands" it should store the information — it explicitly says "I'll remember" — but it generates a text response instead of a structured tool call. Debug logging confirmed `tool_calls=None` in every streaming response chunk.

#### What we tried (none of it worked)

1. **Prompt engineering** — Multiple prompt versions with increasingly forceful language ("MUST call tool", "NEVER just say I'll remember", "CRITICAL RULE"). No effect on proactive scenarios.
2. **Thinking mode** (`think: true`) — Model doesn't support thinking mode. Returns 400 error.
3. **Ask-to-remember approach** — Changed prompt to have the model ask "Would you like me to remember that?" before storing. This worked for the ask->confirm->store flow, but the model still wouldn't search memory on recall ("Where do I live?" got a text-only response).

#### Root cause

This appears to be a **model-level limitation** of the Qwen3-30B-A3B architecture at Q4 quantization for the Ollama tool calling protocol. The 3B active parameters (MoE) may not be enough to reliably distinguish "I should output a tool_call response" from "I should output text about calling a tool" when the trigger is implicit rather than explicit.

### GLM-4.7-Flash (Q4_K_M) — RECOMMENDED

- **Size:** 18.3 GB VRAM (MoE, 30B params)
- **Quantization:** Q4_K_M
- **Source:** `ollama pull glm-4.7-flash`
- **BFCL Score:** ~72% (Berkeley Function Calling Leaderboard)

#### What happened

GLM-4.7-Flash passes all tool calling tests — including the proactive ones that Qwen3 failed:

```
User: "I live in Portland Oregon"
Result: tool_call to memory_tool(operation=set, key=user_location, value=Portland, Oregon, tags=personal, location)
```

```
User: "Where do I live?"
Result: tool_call to memory_tool(operation=search, query=where do I live)
-> Found: user_location = Portland, Oregon
-> Text: "You live in Portland, Oregon!"
```

#### Test methodology

1. **Direct Ollama API test** — Sent tool-equipped chat requests directly to the Ollama API (bypassing HA). 4/4 passed.
2. **Full pipeline test** — Used the conversation API with the complete system prompt, entity exposure, and blueprint routing. 6/6 passed.

### Other Models Investigated (Not Tested)

During model research, we evaluated several other candidates:

| Model | Status | Why Not |
|-------|--------|---------|
| Qwen3-Next-32B | Doesn't exist at 32B | Only available at 80B (50GB VRAM) — too large |
| DeepSeek-V3.2-30B-Instruct | Cloud-only | Not available as a local GGUF model |
| Llama-3.3-Groq-49B-Tool-Use | Doesn't exist at usable size | Only 8B and 70B variants available |
| Qwen3-14B dense | Tested but slower | Same tool calling issues as 30B-A3B, plus slower inference |

## Full Test Results

| Test Case | Qwen3-30B-A3B | GLM-4.7-Flash |
|-----------|:-------------:|:-------------:|
| Explicit: "Remember my name is Robert" | PASS | PASS |
| Proactive: "I live in Portland Oregon" | FAIL | PASS |
| Proactive: "My dog's name is Buddy" | FAIL | PASS |
| Recall: "Where do I live?" | FAIL | PASS |
| Recall: "What is my dog's name?" | FAIL | PASS |
| Recall: "What is my favorite color?" | FAIL | PASS |

## Recommendations

### If you're starting fresh
Use **GLM-4.7-Flash** (`ollama pull glm-4.7-flash`). It's 18.3GB VRAM, which fits comfortably on a 32GB Apple Silicon Mac with room for the embedding model (0.5GB).

### If you have limited VRAM
If 18.3GB is too much, test your candidate model with this checklist before deploying:
1. Can it handle "Remember my name is X" -> tool call? (baseline)
2. Can it handle "I live in Portland" -> tool call without being asked to remember? (proactive)
3. Can it handle "Where do I live?" -> search tool call before answering? (recall)

If it passes 1 but fails 2-3, it will work for explicit commands but the assistant won't feel natural — users will have to say "remember" every time and "search your memory for" instead of just asking questions.

### System prompt matters too

Even with a capable model, some platforms inject a preamble into the system prompt that says "answer from internal knowledge" for non-device questions. This conflicts with memory tool instructions. You need forceful language at the top of your prompt:

```
## CRITICAL RULE — Memory Tool Usage
You have NO memory between conversations. The ONLY way to remember anything is by calling the memory_tool.
Saying "I'll remember" without a tool call means the data is LOST FOREVER.
```

See [SYSTEM_PROMPT.md](SYSTEM_PROMPT.md) for the full recommended prompt.

### Tool exposure matters too

If your platform exposes multiple tools to the LLM, each additional tool increases the decision space and reduces accuracy — especially for MoE models with limited active parameters. Keep only essential tools exposed.

## Testing Tool Calling Directly

You can test your model's tool calling without any platform by hitting the Ollama API directly:

```bash
curl -s http://localhost:11434/api/chat -d '{
  "model": "glm-4.7-flash",
  "messages": [
    {"role": "system", "content": "You are an assistant. When the user shares personal facts, you MUST call memory_tool with operation=set. NEVER just say I will remember."},
    {"role": "user", "content": "I live in Portland Oregon"}
  ],
  "tools": [{
    "type": "function",
    "function": {
      "name": "memory_tool",
      "description": "Store or retrieve personal memories.",
      "parameters": {
        "type": "object",
        "properties": {
          "operation": {"type": "string", "enum": ["set", "search"]},
          "key": {"type": "string"},
          "value": {"type": "string"},
          "query": {"type": "string"}
        },
        "required": ["operation"]
      }
    }
  }],
  "stream": false
}' | python3 -c "import sys,json; d=json.load(sys.stdin); tc=d.get('message',{}).get('tool_calls',[]); print('PASS: tool call made' if tc else 'FAIL: text only -', d.get('message',{}).get('content','')[:100])"
```

If this prints `FAIL: text only`, the model won't work for proactive memory storage regardless of prompt engineering.