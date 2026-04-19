# Arabic Lecture Player

A PWA that downloads YouTube Arabic lectures, transcribes them with Whisper, translates with Claude, and produces self-contained offline HTML files with synced bilingual transcripts.

## Setup

### 1. Modal Secrets

Create two Modal secrets before deploying:

```bash
modal secret create openai-secret OPENAI_API_KEY=sk-...
modal secret create anthropic-secret ANTHROPIC_API_KEY=sk-ant-...
```

### 2. Deploy Backend

```bash
cd backend
modal deploy app.py
```

Copy the printed URL (e.g. `https://mahdi--arabic-lecture-player-process-endpoint.modal.run`) — strip the `/process` suffix to get the base URL.

### 3. Update Frontend Config

Edit `frontend/config.js`:

```js
const CONFIG = {
  MODAL_BASE_URL: "https://mahdi--arabic-lecture-player.modal.run",
};
```

### 4. Deploy Frontend

```bash
cd frontend
vercel --prod
```

### 5. GitHub Actions (auto-deploy)

Add these secrets to your GitHub repo:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic key |
| `OPENAI_API_KEY` | Your OpenAI key |
| `MODAL_TOKEN_ID` | From `modal token new` |
| `MODAL_TOKEN_SECRET` | From `modal token new` |
| `VERCEL_TOKEN` | From Vercel dashboard |

## Usage

1. Open the PWA on your phone
2. Paste a YouTube URL and tap **Process**
3. Wait 5–10 minutes (status updates every 5s)
4. Tap **Download Lecture HTML**
5. Open the HTML file offline — audio player + Arabic/English transcript synced to playback

## Architecture

```
Phone (PWA) → Modal POST /process → spawns background job
                                    ├── yt-dlp (download audio)
                                    ├── Whisper (word-level timestamps)
                                    └── Claude Haiku (translate segments)
                                    → stores HTML in Modal Volume

Phone polls GET /status?job_id=... every 5s
When done → GET /download?job_id=... → self-contained HTML file
```
