# AI Chatbot — Ollama Backend

A minimal streaming AI chat web app powered by [Ollama](https://ollama.com/) and Flask.

## Requirements

- Python 3.9+
- [Ollama](https://ollama.com/) running locally with at least one model pulled

## Quick start

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the server
python app.py
```

Then open **http://127.0.0.1:5000** in your browser.

## Configuration

All options are controlled via environment variables:

| Variable         | Default                   | Description                        |
|------------------|---------------------------|------------------------------------|
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama API base URL                |
| `OLLAMA_MODEL`   | `llama3.2:latest`         | Default model (overridable in UI)  |
| `REQUEST_TIMEOUT`| `120`                     | Seconds before streaming times out |

## Project layout

```
app.py            ← Flask backend + streaming SSE endpoint
static/
  index.html      ← Single-page chat UI (vanilla JS, no build step)
requirements.txt
```

## API

| Method | Path          | Description                          |
|--------|---------------|--------------------------------------|
| GET    | `/`           | Serves the chat UI                   |
| GET    | `/api/models` | Lists locally available Ollama models|
| POST   | `/api/chat`   | Streams a chat response (SSE)        |

### POST `/api/chat` body

```json
{
  "model": "llama3.2:latest",
  "messages": [
    { "role": "user", "content": "Hello!" }
  ]
}
```

Response is a stream of `text/event-stream` lines:

```
data: {"content": "Hi", "done": false}
data: {"content": " there!", "done": false}
data: {"content": "", "done": true}
```
