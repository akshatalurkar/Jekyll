from flask import Flask, request
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
import os
import sys


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

@app.route("/webhook", methods=["POST"])
def webhook():
    phone = request.form.get("From")
    message = request.form.get("Body")
    sys.stdout.flush()
    print(f"Message from {phone}: {message}")
    return "OK", 200

if __name__ == "__main__":
    app.run(debug=True, port=8000)

