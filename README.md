# ORION

**Portable, private, cross-platform AI assistant.**

ORION is a local-first AI assistant designed to run from portable storage on Windows and Linux.

## Goals

- Lightweight local LLM inference
- Browser-based interface
- Multi-user authentication
- Isolated conversation history
- Offline-first operation
- Optional OpenRouter integration
- Portable Windows and Linux runtime
- Minimal external dependencies

## Project Structure

```text
app/        Application source
config/     Configuration
docs/       Documentation
scripts/    Development and build scripts
tests/      Automated tests
```

## Local AI (llama.cpp)

ORION runs a bundled `llama-server` on a random localhost port and talks to it
over HTTP. Put the files here (both folders are gitignored):

```text
runtime/llama/windows-x64/   unzip the llama.cpp "win-cpu-x64" release here
runtime/llama/linux-x64/     unpack the llama.cpp "ubuntu-x64" release here
models/                      one or more .gguf model files
```

ORION finds `llama-server` anywhere under the folder for the current OS/CPU
and uses the first `.gguf` in `models/`. With neither present it falls back
to an echo stand-in and says so at `GET /ai/status`.

| Variable | Default | Meaning |
|---|---|---|
| `ORION_AI_PROVIDER` | `auto` | `auto`, `llama` (fail loudly if unusable) or `echo` |
| `ORION_MODEL_PATH` | first `models/*.gguf` | use a specific model file |
| `ORION_LLAMA_SERVER` | `runtime/llama/<os-cpu>/` | use a specific llama-server |
| `ORION_LLAMA_CTX` | `4096` | context window in tokens |
| `ORION_MAX_REPLY_TOKENS` | `1024` | longest single reply |
| `ORION_LLAMA_THREADS` | auto | CPU threads |
| `ORION_LLAMA_STARTUP_TIMEOUT` | `180` | seconds to wait for the model to load |

Streaming endpoint: `POST /conversations/{id}/messages/stream` returns
server-sent events (`user_message`, `delta`, `done`, `error`). The server log
is written to `data/llama-server.log`.

## Status

Early development - ORION v0.1

## License

TBD
