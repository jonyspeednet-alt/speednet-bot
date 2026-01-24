import os
import requests
import logging
from flask import Flask, request, jsonify
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration from Environment Variables ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")

client = Groq(api_key=GROQ_API_KEY)

# --- Load ISP context once at startup ---
def get_isp_context():
    try:
        with open("training_data.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logging.error("training_data.txt not found!")
        return "স্পিডনেট সম্পর্কিত তথ্য পাওয়া যায়নি।"

ISP_CONTEXT = get_isp_context()

def ask_speednet_ai(user_question):
    system_prompt = (
        f"তুমি SpeedNet Khulna-এর একজন কাস্টমার সাপোর্ট এক্সিকিউটিভ। "
        f"নিচের তথ্যের আলোকে অত্যন্ত বিনয়ের সাথে বাংলায় উত্তর দাও। "
        f"তথ্য না থাকলে অফিসে যোগাযোগ করতে বলো।\n\nতথ্য:\n{ISP_CONTEXT}"
    )
    try:
        completion = client.chat.completions.create(
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_question}],
            model="llama-3.1-8b-instant"
        )
        return completion.choices[0].message.content
    except Exception as e:
        logging.error(f"Groq API Error: {e}")
        return f"দুঃখিত, সমস্যা হয়েছে। এরর: {e}"

# --- ফেসবুক ভেরিফিকেশন (GET) ---
@app.route("/webhook", methods=["GET"])
def verify():
    token_sent = request.args.get("hub.verify_token")
    if token_sent == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Verification Token Mismatch", 403

# --- ফেসবুক মেসেজ রিসিভ এবং রিপ্লাই (POST) ---
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if data.get("object") == "page":
        for entry in data.get("entry", []):
            for messaging_event in entry.get("messaging", []):
                if messaging_event.get("message"):
                    sender_id = messaging_event["sender"]["id"]
                    message_text = messaging_event["message"].get("text")
                    
                    if message_text:
                        # AI থেকে উত্তর নেওয়া
                        response_text = ask_speednet_ai(message_text)
                        # ফেসবুকে রিপ্লাই পাঠানো
                        send_message(sender_id, response_text)
    return "EVENT_RECEIVED", 200

def send_message(recipient_id, message_text):
    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}
    data = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text}
    }
    try:
        response = requests.post("https://graph.facebook.com/v19.0/me/messages", params=params, headers=headers, json=data)
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
        logging.info(f"Message sent to {recipient_id}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error sending message to {recipient_id}: {e}")
        # Log the response text from Facebook for easier debugging
        if 'response' in locals() and response.text:
            logging.error(f"Response Body: {response.text}")

@app.route("/", methods=["GET"])
def home():
    return "স্পিডনেট এআই সার্ভার সচল আছে!"

if __name__ == "__main__":
    print("--- স্পিডনেট এআই সার্ভার চালু হচ্ছে ---")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)