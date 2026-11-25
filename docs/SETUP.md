# Setting up the optional features

Everything below is optional. The core app (leads, webhooks, memory, ask) works with only the Gemini key.

## Daily commands (Windows PowerShell, from the project folder)

```powershell
.venv\Scripts\Activate.ps1
docker compose up -d                 # database
uvicorn app.main:app                 # terminal 1: API + UI at http://127.0.0.1:8000/ui/
python -m app.worker                 # terminal 2: background jobs
```

First time on a new copy: `pip install -r requirements.txt`, `copy .env.example .env` (then fill it in),
`alembic upgrade head`, `python -m app.seed`, optionally `python -m scripts.seed_demo`.

## Background worker

The worker turns new messages into memories, sends Slack alerts and syncs Gmail. In the UI open **Jobs**:
failed jobs retry with backoff; jobs that keep failing become **dead** and have a **Retry** button.

## Slack alerts

1. https://api.slack.com/apps -> Create New App -> From scratch -> Incoming Webhooks -> turn on -> Add New Webhook to Workspace.
2. Put the URL in `.env` as `SLACK_WEBHOOK_URL`, restart the API and the worker.
3. `POST /integrations/slack/test?inline=true` in `/docs` should post a message.
Alerts are sent for new enquiries (portal and WhatsApp) and when a patient is promised a follow-up.

## Gmail and Google Docs

1. https://console.cloud.google.com -> new project -> **APIs & Services -> Library**: enable *Gmail API* and *Google Docs API*.
2. **OAuth consent screen**: External, add yourself under **Test users**.
3. **Credentials -> Create credentials -> OAuth client ID -> Web application**. Authorized redirect URI:
   `http://127.0.0.1:8000/integrations/google/callback`
4. Put the client id and secret in `.env`, and generate an encryption key:
   ```powershell
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   Paste it as `TOKEN_ENCRYPTION_KEY`. Restart the API and worker.
5. Open **http://127.0.0.1:8000/integrations/google/connect** in your browser and approve.
6. `POST /integrations/google/sync?inline=true` pulls recent emails **from people who are already leads**
   (create a lead with the email address of someone who has emailed you). The worker then re-syncs every few minutes.
7. `POST /leads/{id}/treatment-plan-doc` creates a Google Doc (needs the Docs scope you approved in step 5).

Notes: while the consent screen is in *Testing* mode, Google expires refresh tokens after 7 days; connect again when
the account shows `needs_reauth`. Use a test mailbox, not your real inbox.

## WhatsApp (simulated)

Real WhatsApp Cloud API access needs a Meta business account. The webhook follows Meta's format, so you can test it locally:

1. In `.env` set `WHATSAPP_APP_SECRET=anything-random` and `WHATSAPP_VERIFY_TOKEN=anything-random`.
2. `python -m scripts.send_whatsapp --text "Hi, do you do implants?"` creates a lead from the phone number.
   Run it twice with the same `--id` to see duplicates ignored.

## MCP server (use ClinicMind from Claude Desktop)

Add this to Claude Desktop's config (Settings -> Developer -> Edit Config), using your real folder:

```json
{
  "mcpServers": {
    "clinicmind": {
      "command": "D:\\clinicmind\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server.server"],
      "env": { "PYTHONPATH": "D:\\clinicmind" }
    }
  }
}
```
Restart Claude Desktop. Try: "Find Priya and tell me what her budget is." The database container must be running.
You can also test with the MCP Inspector: `npx @modelcontextprotocol/inspector python -m mcp_server.server`.

## Evals

```powershell
python -m evals.run --leads 2 --strategies memory --sleep 2    # quick trial
python -m evals.run --sleep 2                                  # everything (about 48 x 3 answers + memory building)
```
Results go to the database, `evals/output/report.html` and the **Evals** tab. Re-run after changing retrieval
weights or prompts to see whether it helped. Keep your free-tier rate limit in mind: raise `--sleep` if you see errors.
