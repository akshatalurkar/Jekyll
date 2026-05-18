# Jekyll

## About the Project

Jekyll is a Google Calendar assistant native to WhatsApp. You can schedule events, check your day, and get reminders without leaving the chat you already use.

### Built With

[![Python][Python-shield]][Python-url]
[![Flask][Flask-shield]][Flask-url]
[![PostgreSQL][Postgres-shield]][Postgres-url]
[![Gemini][Gemini-shield]][Gemini-url]
[![Google Calendar][GCal-shield]][GCal-url]
[![WhatsApp][WhatsApp-shield]][WhatsApp-url]
[![Railway][Railway-shield]][Railway-url]

## How It Works

Jekyll runs a four-stage pipeline on every incoming WhatsApp message.

1. **Parse**

   a) The raw message is sent to Gemini 2.5 Flash with a structured system prompt and the user's current pending state as context.

   b) Gemini returns a typed JSON action (`create`, `update`, `delete`, `list`, `confirm`, `cancel`) along with any extracted fields: title, date, time, duration, calendar, and reminder.

   c) The response is validated through a Pydantic model before any next step runs.

2. **Dispatch**

   a) The validated action hits a dispatch layer that routes based on action type and whether a pending state exists.

   b) Pending state is stored per user in Postgres and expires after 10 minutes.

   c) Corrections mid-flow merge into the existing pending event rather than starting over, so the user never loses context.

3. **Execute**

   a) Confirmed actions call the Google Calendar API directly.

   b) Calendar names resolve through fuzzy string matching, so "work cal" finds "Work Calendar" without an exact match.

   c) Read-only calendars are filtered at the API level before they are ever presented as options, which prevents write failures downstream.

4. **Remind**

   a) A persistent worker process polls every 60 seconds, independently of the web service.

   b) Reminder times are read live from Google Calendar on each poll rather than stored, so edits always reflect correctly.

   c) Reminders are deduplicated by event ID and reminder offset, so editing a reminder after one has already fired still triggers the new one.

## Architecture

<img width="3520" height="2368" alt="image" src="https://github.com/user-attachments/assets/c9aab963-d283-47b2-afd6-ce8842cf9e7b" />

**Request flow:**

1. A user texts the Jekyll number. Meta delivers the message as a POST to the Flask webhook at `/webhook`.
2. Flask verifies the request signature against `WHATSAPP_APP_SECRET`. First-time users receive an OAuth link to connect Google Calendar.
3. The message and the user's pending state are sent to Gemini 2.5 Flash with a structured system prompt.
4. The response is validated through a Pydantic model into a typed action.
5. The typed action hits the dispatch layer, which routes on action type and pending state. State is written to Postgres.
6. On a confirm, the dispatch layer calls the Google Calendar API and sends the result back to the user over the WhatsApp Cloud API.

**Reminder worker (runs independently on a 60-second poll):**

- Reads upcoming event and reminder times live from Google Calendar.
- Checks Postgres to dedup against already-sent reminders.
- Sends due reminders to the user over the WhatsApp Cloud API.

## Why This Stack?

**Flask:** The webhook is a thin HTTP layer. It receives a POST, verifies a signature, 
and hands off to the pipeline. Flask fits that surface without the overhead of a full 
framework.

**Gemini 2.5 Flash:** Parsing a calendar request is extraction and classification, not 
open-ended generation. The user is in a live chat waiting for a reply, so latency 
matters. Flash is fast and cheap, and since the structured system prompt constrains 
the output space, the model does not need to reason heavily.

**Pydantic:** LLM output is untrusted by default. Validating into a typed schema catches 
malformed responses before they reach the calendar API, so a bad parse fails loudly 
instead of silently corrupting a write.

**Postgres:** Pending state has to survive deploys and be shared across two processes: 
the web service and the reminder worker. An in-memory store would lose state on 
restart and cannot be read across processes. Postgres handles both, and the reminder 
dedup table gets the same durability for free.

**Separate reminder:** Reminders are time dependent, so polling cannot live in the request path or a slow poll would block a webhook response. Running it as its own persistent process keeps the two concerns isolated and independently restartable.

**Railway:** Railway runs a web service and a persistent worker side by side with a 
shared Postgres instance and shared environment variables. 

Jekyll lives in WhatsApp because that's where the users are. Minimizing friction is the rationale behind this choice.

## Project Structure

The codebase maps onto the pipeline stages.

`whatsapp.py` is the Flask web service. It owns webhook verification, signature checks, the Google OAuth flow, and outbound WhatsApp messages.

`parse.py` is the language layer. It holds the system prompt, the Gemini call, and the Pydantic models that define every valid action. Nothing leaves this file until it has passed validation.

The dispatch layer routes validated actions by type and pending state. The calendar layer wraps the Google Calendar API.

`reminders.py` is the worker. It runs as its own process, polling Google Calendar on a fixed interval.

## Getting Started

### Prerequisites

- Python 3.11+
- Meta WhatsApp Business account and phone number
- Google Cloud project with the Calendar API enabled
- Gemini API key
- Railway account (or any platform with Postgres and persistent workers)

### Installation

1. Clone the repo
   ```sh
   git clone https://github.com/aalurkar/jekyll.git
   cd jekyll
   ```

2. Install dependencies
   ```sh
   pip install -r requirements.txt
   ```

3. Create a `.env` file
   ```
   FLASK_SECRET_KEY=
   TOKEN_ENCRYPTION_KEY=
   GEMINI_API_KEY=
   GOOGLE_CLIENT_ID=
   GOOGLE_CLIENT_SECRET=
   WHATSAPP_TOKEN=
   WHATSAPP_PHONE_NUMBER_ID=
   WHATSAPP_APP_SECRET=
   WHATSAPP_VERIFY_TOKEN=
   DATABASE_URL=sqlite:///users.db
   BASE_URL=http://localhost:8001
   ```

4. Run locally
   ```sh
   FLASK_APP=whatsapp flask run --port 8001
   ```

5. Expose your local server for WhatsApp webhook verification
   ```sh
   ngrok http 8001
   ```

6. Set your webhook URL in the Meta Developer Console to `https://<your-ngrok-url>/webhook`

## Deployment

Jekyll runs two services on Railway.

### Web Service

Handles all incoming WhatsApp messages.

- Start command: `gunicorn whatsapp:app`

### Reminders Worker

Sends reminders before events. Runs as a persistent worker, so no cron schedule is needed.

- Start command: `python reminders.py`

Both services share the same Postgres database. Use Railway's shared variables so both stay in sync.

## Roadmap

[Here's](https://www.notion.so/Overview-3632b89f0b398058bd0be0cbc135aa44?source=copy_link#3632b89f0b39808fba5dd2b5f35fd210) what we have planned for the future of Jekyll. 

## Contact

Aanya Soni - aanya.soni@gmail.com - [LinkedIn](https://linkedin.com/in/aanyasonii)

Akshat Alurkar - aalurkar05@gmail.com - [LinkedIn](https://linkedin.com/in/akshatalurkar)

---

[Python-shield]: https://img.shields.io/badge/Python-1a1a1a?style=for-the-badge&logo=python&logoColor=white
[Python-url]: https://python.org
[Flask-shield]: https://img.shields.io/badge/Flask-1a1a1a?style=for-the-badge&logo=flask&logoColor=white
[Flask-url]: https://flask.palletsprojects.com
[Postgres-shield]: https://img.shields.io/badge/PostgreSQL-1a1a1a?style=for-the-badge&logo=postgresql&logoColor=white
[Postgres-url]: https://postgresql.org
[GCal-shield]: https://img.shields.io/badge/Google_Calendar-1a1a1a?style=for-the-badge&logo=google-calendar&logoColor=white
[GCal-url]: https://developers.google.com/calendar
[WhatsApp-shield]: https://img.shields.io/badge/WhatsApp-1a1a1a?style=for-the-badge&logo=whatsapp&logoColor=white
[WhatsApp-url]: https://developers.facebook.com/docs/whatsapp
[Gemini-shield]: https://img.shields.io/badge/Gemini-1a1a1a?style=for-the-badge&logo=googlegemini&logoColor=white
[Gemini-url]: https://aistudio.google.com
[Railway-shield]: https://img.shields.io/badge/Railway-1a1a1a?style=for-the-badge&logo=railway&logoColor=white
[Railway-url]: https://railway.app
