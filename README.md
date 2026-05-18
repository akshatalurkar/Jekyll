# Jekyll
 
## About the Project
 
Jekyll is a WhatsApp-native Google Calendar assistant. Schedule events, view your upcoming schedule, and get reminders all in the same chat.
 
[User Guide](https://www.notion.so/User-guide-for-Jekyll-35d2b89f0b3980faa54eccbd930609c0?source=copy_link)
 
### Built With
 
[![Python][Python-shield]][Python-url]
[![Flask][Flask-shield]][Flask-url]
[![PostgreSQL][Postgres-shield]][Postgres-url]
[![Gemini][Gemini-shield]][Gemini-url]
[![Google Calendar][GCal-shield]][GCal-url]
[![WhatsApp][WhatsApp-shield]][WhatsApp-url]
[![Railway][Railway-shield]][Railway-url]
 
## How It Works
 
Jekyll uses a four-stage pipeline that runs on each incoming WhatsApp message.
 
1. Parse
   1. The raw message is sent to Gemini 2.5 Flash with a structured system prompt and the user's current pending state as context.
   2. Gemini returns a typed JSON action (`create`, `update`, `delete`, `list`, `confirm`, `cancel`) along with any extracted fields — title, date, time, duration, calendar, and reminder.
   3. The response is validated through a Pydantic model before any next steps run.
2. Dispatch
   1. The validated action hits a dispatch layer that routes based on action type and whether a pending state exists.
   2. Pending state is stored per user in Postgres and expires after 10 minutes.
   3. Corrections mid-flow merge into the existing pending event rather than starting over, so the user never loses context.
3. Execute
   1. Confirmed actions call the Google Calendar API directly.
   2. Calendar names are resolved via fuzzy string matching — "work cal" finds "Work Calendar" without an exact match.
   3. Read-only calendars are filtered at the API level before they're ever presented as options, preventing write failures downstream.
4. Remind
   1. A persistent worker process polls every 60 seconds independently of the web service.
   2. Reminder times are read live from Google Calendar on each poll rather than stored, so edits always reflect correctly.
   3. Reminders are deduplicated by event ID and reminder offset — editing a reminder after one already fired still triggers the new one.
### Architecture
 
```
WhatsApp Cloud API → Flask webhook → Gemini 2.5 Flash → Pydantic model
→ Dispatch layer → Google Calendar API → Postgres (state + dedup)
                                       ↑
                     Reminder worker (persistent, 60s poll)
```
 
For the full product spec see the [Jekyll PRD](https://www.notion.so/Jekyll-PRD-WIP-3632b89f0b3980f5b8dcebb322d80e1b?source=copy_link).
 
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
## Usage
 
Once connected via OAuth, text Jekyll on WhatsApp.
 
**Scheduling**
```
Dentist Friday at 3pm
Add a team lunch tomorrow at noon on my work calendar
Coffee with Maya Saturday morning, 45 min
```
 
**Editing**
```
Move my dentist to Monday at 2pm
Change the lunch to 1 hour
Edit team lunch to my personal calendar
```
 
**Deleting**
```
Cancel the dentist appointment
Remove team lunch
```
 
**Viewing**
```
What do I have today?
What's tomorrow look like?
Tell me about the team lunch
What calendars do I have?
```
 
**Reminders**
```
Remind me 1 hour before the dentist
Add a 15 minute reminder to team lunch
```
 
**Other**
```
Refresh
```
 
Jekyll confirms every action before committing. Reply Yes, No, or send a correction inline.
 
For the full user guide see the [Jekyll User Guide](https://www.notion.so/User-guide-for-Jekyll-35d2b89f0b3980faa54eccbd930609c0?source=copy_link).
 
## Deployment
 
Jekyll runs two services on Railway.
 
### Web Service
 
Handles all incoming WhatsApp messages.
 
- Start command: `gunicorn whatsapp:app`
### Reminders Worker
 
Sends WhatsApp reminders before events. Runs as a persistent worker — no cron schedule needed.
 
- Start command: `python reminders.py`
- Polls every 60 seconds
- Reads reminder times live from Google Calendar so edits always reflect correctly
Both services share the same Postgres database. Use Railway's shared variables so both stay in sync.
 
## Roadmap
 
- [x] Create, edit, and delete events via WhatsApp
- [x] Multi-calendar support with fuzzy matching
- [x] Conflict detection
- [x] Configurable WhatsApp reminders
- [x] Persistent reminder worker
- [ ] Multi-user onboarding flow
- [ ] Recurring event support
- [ ] Time zone configuration per user
For the full product spec see the [Jekyll PRD](https://www.notion.so/Jekyll-PRD-WIP-3632b89f0b3980f5b8dcebb322d80e1b?source=copy_link).
 
## Contact
 
Akshat Alurkar — aalurkar05@gmail.com — [LinkedIn](https://linkedin.com/in/akshat-alurkar)
 
Project Link: [https://github.com/aalurkar/jekyll](https://github.com/aalurkar/jekyll)
 
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
