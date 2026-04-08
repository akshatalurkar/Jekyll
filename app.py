import os
import sys
import json
from datetime import date

from flask import Flask, request
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from google import genai


load_dotenv()
app = Flask(__name__)

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///users.db"
db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    oauth_token = db.Column(db.Text, nullable=True)
    refresh_token = db.Column(db.Text, nullable=True)

with app.app_context():
    db.create_all()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def parse_event(message):
    today = date.today().strftime("%A, %B %d, %Y")
    prompt = f"""Today is {today}.
Extract calendar event details from this message and return ONLY a JSON object with these fields:
title, date (YYYY-MM-DD), time (HH:MM, 24hr), duration_minutes.
Set any missing fields to null.
Message: "{message}" """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    raw = response.text.strip().replace("```json", "").replace("```", "")
    return json.loads(raw)

@app.route("/webhook", methods=["POST"])
def webhook():
    phone = request.form.get("From")
    message = request.form.get("Body")
    sys.stdout.flush()
    print(f"Message from {phone}: {message}")
    return "OK", 200

if __name__ == "__main__":
    app.run(debug=True, port=8000)

