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

## Starting ORION

- **Windows:** double-click `Start-ORION.bat`.
- **Linux / macOS:** run `./start-orion.sh`.
- **By hand:** `python launch.py` (from the project's virtual environment).

The launcher picks a free port (8000, or the next free one), starts ORION, and
opens your browser at the login page once it is ready. Starting it a second
time just reopens the copy that is already running. Options:
`--port 8000`, `--host 127.0.0.1`, `--no-browser`. Keep its window open while
you use ORION; press Ctrl+C in it (or close it) to stop.

If the `.venv` folder is missing, the scripts offer to create it and install
`requirements.txt` for you (internet needed, once).

### First run

With no AI engine or model yet, ORION still opens and shows a setup guide at
the top of the chat: it names the exact folders to put the llama.cpp files
and your `.gguf` model in. Add them, press **Check again**, and ORION starts
loading the model, with no restart.

## Project Structure

```text
launch.py   Launcher (Start-ORION.bat / start-orion.sh call this)
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

ORION finds `llama-server` anywhere under the folder for the current OS/CPU.
With no server or no model it falls back to an echo stand-in and says so at
`GET /ai/status`.

### Choosing a model

Every `.gguf` in `models/` appears in the **Model** dropdown in the top bar.
Pick one and ORION restarts the engine on it (the banner shows "Loading..."
until it is ready). The choice is remembered in `data/selected_model.txt`; on
first run the first file alphabetically is used.

- New files show up within ~30 seconds, no restart needed. If `models/` was
  empty at launch, adding a file and picking it brings the AI online.
- Switching is refused while a reply is being written, and applies to everyone
  using this ORION (there is one engine).
- Vision projector files (`mmproj-*.gguf`) and the extra parts of split models
  (`...-00002-of-00005.gguf`) are not listed; for a split model, pick part 1.
- Only chat models work here. Image-generation models need a different engine.
- Setting `ORION_MODEL_PATH` pins one model and disables the dropdown.

| Variable | Default | Meaning |
|---|---|---|
| `ORION_AI_PROVIDER` | `auto` | `auto`, `llama` (fail loudly if unusable) or `echo` |
| `ORION_MODEL_PATH` | unset (use the dropdown) | pin one model file and disable the dropdown |
| `ORION_LLAMA_SERVER` | `runtime/llama/<os-cpu>/` | use a specific llama-server |
| `ORION_LLAMA_CTX` | `8192` | context window in tokens (also limits how much attached-file text fits) |
| `ORION_MAX_UPLOAD_MB` | `5` | largest file that can be uploaded |
| `ORION_MAX_REPLY_TOKENS` | `1024` | longest single reply |
| `ORION_LLAMA_THREADS` | auto | CPU threads |
| `ORION_LLAMA_STARTUP_TIMEOUT` | `180` | seconds to wait for the model to load |
| `ORION_AGENT_ROOTS` | unset (any folder) | folders the reader may look at (`;` or `:` separated) |

Streaming endpoint: `POST /conversations/{id}/messages/stream` returns
server-sent events (`user_message`, `delta`, `done`, `error`). The server log
is written to `data/llama-server.log`.

## Attaching files

Click the paperclip (or drop a file on the chat) to attach a file to the
current conversation, then ask questions about it. Supported: text and code
files (`.txt .md .csv .json .py` ...), `.docx`, and `.pdf` with selectable text
(`pip install pypdf`). ORION stores only the extracted text, so deleting a
conversation removes everything that came from the upload.

The file's text is sent to the model with every question, so it must fit in the
context window (`ORION_LLAMA_CTX`) alongside your chat. ORION tells you if it
doesn't. The first question after attaching a big file is slower because the
model has to read the file; later questions reuse that work.

## Working with your chats

- **Regenerate**: the button under the newest answer asks the model again.
  The old answer is replaced only once the new one has arrived, so a failed
  attempt never loses it. If a question was left unanswered, the same button
  retries it.
- **Rename**: click the pencil next to the chat title (or double-click the
  title). Enter or clicking away saves, Escape cancels. A renamed chat keeps
  its name; it is never replaced by an automatic title.
- **Search**: the box above the chat list matches chat titles and message text
  (case-insensitive, no need for exact words). Results show an excerpt with the
  match highlighted; clear the box to get the full list back. Text inside
  attached files is not searched.
- **Export**: the arrow next to the title downloads the chat as a `.md` file
  (your messages and ORION's replies, plus the names of attached files, but
  not their contents).

## Agent runs

**Agent runs** (link in the chat's top bar, or `/runs`) is the foundation for
running multi-step tasks: each run is a fixed list of steps run by a
*supervisor* that is ordinary code, not a model. Every step, note, timing and
outcome is written to an event log and shown as a live timeline.

- **Self-check** checks the AI engine and models, asks the model a short test
  question, and reports how long it took.
- **Ask about a folder** is the *reader*: give it a folder (full path, e.g.
  `D:\Projects\myapp`) and a question. It lists folders, reads files and
  searches them, then answers in Markdown, naming the files it used. Every
  tool call appears in the timeline with the reason the model gave, and under
  the answer ORION itself lists which files and line ranges were really read
  (so you can see what the answer is based on, whatever the model claims). It needs a
  real model loaded (not echo mode) and works best with a context size of
  4096 or more.
- One run at a time per user (there is one AI model). **Cancel** stops a run
  between steps, and in the middle of a model reply.
- Every run has limits (number of steps, total time); a failing step stops the
  run, and the log says exactly where and why.
- Runs and their logs are kept until you delete them. If ORION is closed
  while a run is going, that run is marked as failed on the next start.

The reader asks the model for one small JSON action per turn and sends the
AI server a grammar so the reply is always valid. If a particular llama.cpp
build refuses the grammar (this happened with Gemma on a recent build), ORION
retries without it and reads the JSON leniently; the timeline shows a warning
when that happens, and it works but is a little less reliable.

- **Edit files in a folder** is the *editor*: give it a folder and a change you
  want ("rename the function helper to compute in util.py"). It reads the
  files, prepares small exact search-and-replace edits (and new files), and then
  **stops and shows you a coloured diff**. Nothing is written until you click
  **Apply changes**; **Reject** or **Cancel** leaves every file exactly as it
  was.

### How editing stays safe

- Edits are kept in memory. The only code that writes your files runs after
  your approval, and it is all-or-nothing: if any file changed since the diff
  was made, nothing is written; the originals are backed up first; files are
  written atomically; and if a write fails part way, everything is put back.
- After applying, **Undo changes** restores the backups. It only touches a
  file that still contains exactly what ORION wrote, so work you did after the
  change is never overwritten (those files are reported and left alone).
  Backups live in `data/run_changes/` and are removed when you delete the run.
- Each edit must match the file's text exactly, and exactly once, or it is
  refused with a hint; at most 5 files and 12 edits per task.
- The same gate as the reader applies (nothing outside the folder, ORION's own
  data, secrets files, `.git`/`node_modules`...), plus: binary, non-UTF-8,
  oversized and mixed-line-ending files are never edited, Windows (CRLF) line
  endings and byte-order marks are preserved, and a whole drive cannot be
  chosen as the folder.
- While waiting for you, a run does not time out (the wait is not counted), but
  it gives up after 30 minutes without a decision, changing nothing.

### Security checks before you approve

Before the diff reaches you, ORION runs a set of checks in plain code (no AI is
involved, so they cannot be talked out of anything) on exactly what is about to
be written, and shows the findings in the timeline and on the approval bar:

- **High risk** (you must confirm before you can apply; the server enforces
  this, not just the page): keys and tokens (AWS, GitHub, Slack, Google, Stripe,
  JSON web tokens, private keys), passwords in URLs or assigned in code, hidden
  direction-control characters, commands that download and run code, encoded
  PowerShell, and SSH or sudo configuration files.
- **Warning**: new or changed scripts (`.bat`, `.ps1`, `.sh`...), CI and git-hook
  files, dangerous commands (`eval`, `rm -rf`, switched-off certificate checks),
  invisible characters, text that reads like instructions to an AI, and
  surprising size (rewriting over half a file, removing most of it, 200+ lines).
- **Note**: build/dependency files, network calls, long new files.

Only the lines a change *adds* are scanned, so a secret already in a file is not
blamed on the edit. If the checker itself fails, the run stops (nothing is
applied). Placeholders such as `changeme` or `os.environ["KEY"]` are ignored,
and zero-width joiners are not flagged because Marathi and Hindi text needs them.

Secret-looking values are masked (`AKIA****`) everywhere ORION shows or stores
text from your files: the findings, the diff, and the tool-call previews. The
real text is still what gets written if you apply.

### How the reader stays safe

The model never touches your files directly. Each turn it can only ask for one
of three read-only actions, and every request goes through one strict gate:

- Only paths inside the folder you chose; `..`, absolute paths, drive
  letters and Windows device names are refused, and symlinks that lead
  outside the folder are not followed.
- ORION's own `data` folder (your accounts and chats) is always off limits,
  even if you pick a folder that contains it.
- Secrets-looking files (`.env`, private keys, `*.pem`...) are never shown,
  and `.git`, `node_modules` and similar folders are skipped.
- Caps on file size, output size, and search effort; binary files are skipped.
- Text inside files is treated as data, never as instructions, and the number
  of steps and the total time are limited.

To restrict which folders may be read at all, set `ORION_AGENT_ROOTS` to one
or more folders (separated by `;` on Windows, `:` elsewhere); anything outside
them is refused. Unset means any folder your account can read.

Later tasks plug into the same supervisor with steps that propose edits and
check them.

## Status

Early development - ORION v0.1

## License

TBD
