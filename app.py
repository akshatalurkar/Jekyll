from flask import Flask, request
from dotenv import load_dotenv
import os
import sys

load_dotenv()
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    phone = request.form.get("From")
    message = request.form.get("Body")
    sys.stdout.flush()
    print(f"Message from {phone}: {message}")
    return "OK", 200

if __name__ == "__main__":
    app.run(debug=True, port=8000)

