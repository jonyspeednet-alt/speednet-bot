import os
import requests
import logging
import threading
from flask import Flask, request, jsonify
import sqlite3
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
FACEBOOK_API_VERSION = os.getenv("FACEBOOK_API_VERSION", "v19.0")

client = Groq(api_key=GROQ_API_KEY)

# --- Database Setup (SQLite) ---
def init_db():
    """ডাটাবেস এবং টেবিল তৈরি করে"""
    conn = sqlite3.connect('conversations.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS summaries (
            sender_id TEXT PRIMARY KEY,
            summary TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

def add_message_to_history(sender_id, role, content):
    """ডাটাবেসে মেসেজ সংরক্ষণ করে"""
    conn = sqlite3.connect('conversations.db')
    conn.execute('INSERT INTO messages (sender_id, role, content) VALUES (?, ?, ?)', (sender_id, role, content))
    conn.commit()
    conn.close()

def get_conversation_history(sender_id, limit=10):
    """নির্দিষ্ট ইউজারের পুরনো মেসেজগুলো ডাটাবেস থেকে নিয়ে আসে"""
    conn = sqlite3.connect('conversations.db')
    conn.row_factory = sqlite3.Row
    messages = conn.execute('SELECT role, content FROM messages WHERE sender_id = ? ORDER BY timestamp DESC LIMIT ?', (sender_id, limit)).fetchall()
    conn.close()
    return [{"role": msg["role"], "content": msg["content"]} for msg in reversed(messages)]

def get_summary(sender_id):
    """ডাটাবেস থেকে ইউজারের সামারি নিয়ে আসে"""
    conn = sqlite3.connect('conversations.db')
    cursor = conn.cursor()
    cursor.execute('SELECT summary FROM summaries WHERE sender_id = ?', (sender_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""

def save_summary(sender_id, summary):
    """সামারি আপডেট করে"""
    conn = sqlite3.connect('conversations.db')
    conn.execute('INSERT OR REPLACE INTO summaries (sender_id, summary) VALUES (?, ?)', (sender_id, summary))
    conn.commit()
    conn.close()

def generate_summary(current_summary, new_lines):
    """LLM ব্যবহার করে সামারি আপডেট করে"""
    prompt = (
        f"Update the conversation summary with the new lines. Keep it concise and relevant to customer support.\n"
        f"Current Summary: {current_summary}\n"
        f"New Lines:\n{new_lines}\n"
        f"Output only the updated summary."
    )
    try:
        completion = client.chat.completions.create(
            messages=[{"role": "system", "content": "You are a helpful assistant that summarizes conversations."}, {"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant"
        )
        return completion.choices[0].message.content
    except Exception as e:
        logging.error(f"Summarization failed: {e}")
        return current_summary

def prune_and_summarize(sender_id):
    """মেসেজ সংখ্যা বেশি হলে পুরনো মেসেজ সামারি করে ডিলিট করে"""
    conn = sqlite3.connect('conversations.db')
    conn.row_factory = sqlite3.Row
    count = conn.execute('SELECT COUNT(*) FROM messages WHERE sender_id = ?', (sender_id,)).fetchone()[0]
    
    if count > 10:  # যদি ১০টির বেশি মেসেজ থাকে
        old_msgs = conn.execute('SELECT id, role, content FROM messages WHERE sender_id = ? ORDER BY timestamp ASC LIMIT 5', (sender_id,)).fetchall()
        if old_msgs:
            ids_to_delete = [msg['id'] for msg in old_msgs]
            text_to_summarize = "\n".join([f"{msg['role']}: {msg['content']}" for msg in old_msgs])
            current_summary = get_summary(sender_id)
            new_summary = generate_summary(current_summary, text_to_summarize)
            save_summary(sender_id, new_summary)
            placeholders = ','.join('?' * len(ids_to_delete))
            conn.execute(f'DELETE FROM messages WHERE id IN ({placeholders})', ids_to_delete)
            conn.commit()
            logging.info(f"Summarized and pruned {len(ids_to_delete)} messages for {sender_id}")
    conn.close()

# --- Load ISP context once at startup ---
def get_isp_context():
    try:
        with open("training_data.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logging.error("training_data.txt not found!")
        return "স্পিড নেট সম্পর্কিত তথ্য পাওয়া যায়নি।"

ISP_CONTEXT = get_isp_context()

def ask_speednet_ai(user_question, summary, history):
    # --- বিস্তারিত সিস্টেম প্রম্পট ---
    system_prompt = (
        f"### পার্সোনা (Persona)\n"
        f"তুমি  স্পিড নেট খুলনার একজন দক্ষ ও বিনয়ী এআই অ্যাসিস্ট্যান্ট। তোমার নাম 'স্পিডি'।\n"
        f"তোমার প্রধান কাজ হলো গ্রাহকদের দ্রুত এবং সঠিক তথ্য দিয়ে সহায়তা করা।\n\n"
        
        f"### অনুসরণীয় নির্দেশনাবলী (Instructions to Follow):\n"
        f"১. **ভাষা:** সবসময় মার্জিত এবং শুদ্ধ বাংলায় কথা বলবে।\n"
        f"২. **ব্র্যান্ডিং:** 'স্পিড নেট খুলনা'-এর সুনাম বজায় রাখবে।\n"
        f"৩. **তথ্যসূত্র:** শুধুমাত্র নিচের 'তথ্য' এবং 'পূর্ববর্তী আলোচনার সারাংশ' ব্যবহার করে উত্তর দেবে। কোনো অবস্থাতেই কাল্পনিক বা বাইরের তথ্য দেওয়া যাবে না।\n"
        f"৪. **সংক্ষিপ্ততা:** উত্তর হবে সংক্ষিপ্ত, নির্ভুল এবং টু-দ্য-পয়েন্ট। প্রয়োজনে বুলেট পয়েন্ট ব্যবহার করবে।\n"
        f"৫. **অজানা প্রশ্ন:** যদি কোনো প্রশ্নের উত্তর তোমার জানা না থাকে, তাহলে সরাসরি বলবে, 'এই মুহূর্তে আমার কাছে তথ্যটি নেই। বিস্তারিত জানতে অনুগ্রহ করে আমাদের হটলাইনে (09639333111) যোগাযোগ করুন।' কোনোভাবেই ভুল উত্তর দেবে না।\n"
        f"৬. **টেকনিক্যাল সাপোর্ট:** সাধারণ টেকনিক্যাল সমস্যার (যেমন: রাউটার রিস্টার্ট, লাল বাতি) জন্য ধাপে ধাপে (step-by-step) সমাধান দেবে। জটিল সমস্যার জন্য হটলাইনে যোগাযোগ করতে বলবে।\n\n"
        f"৭. **সহানুভূতি:** সমস্যাজনিত মেসেজে আগে দুঃখ প্রকাশ করবে।\n"
f"৮. **লিড জেনারেশন:** নতুন সংযোগ প্রত্যাশীদের ফোন নম্বর ও এলাকা জানতে চাইবে।\n"
f"৯. **ইমোজি:** উত্তরের সাথে মানানসই ইমোজি ব্যবহার করবে।\n"
f"১০. **নিরাপত্তা:** কখনো পাসওয়ার্ড চাইবে না।\n"
f"১১. **সমাধান নিশ্চিতকরণ:** টেকনিক্যাল গাইড দেওয়ার পর সমাধান হয়েছে কি না জানতে চাইবে।\n"
f"১২. **স্মার্ট সাজেশন:** প্যাকেজ সম্পর্কিত তথ্যের সাথে সেরা ডিলটি হাইলাইট করবে।\n"
f"১৩. **অভিযোগ সংগ্রহ:** অভিযোগের জন্য ইউজার আইডি ও ফোন নম্বর ফরম্যাট মেনে চাইবে।\n"
f"১৪. **সময় সচেতনতা:** অফিস সময়ের (৯টা-১০টা) বাইরে প্রাপ্ত অভিযোগের জন্য বিশেষ আশ্বাস দেবে।\n"


        f"--- ডেটা সেকশন ---\n"
        f"### তথ্য (Knowledge Base):\n"
        f"{ISP_CONTEXT}\n\n"
        
        f"### পূর্ববর্তী আলোচনার সারাংশ (Previous Conversation Summary):\n"
        f"{summary}\n"
        f"--- ডেটা সেকশন সমাপ্ত ---"
    )

    # সিস্টেম প্রম্পট এবং সাম্প্রতিক আলোচনা দিয়ে মেসেজ লিস্ট তৈরি
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_question})

    try:
        completion = client.chat.completions.create(
            messages=messages,
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
                    message = messaging_event["message"]

                    # কুইক রিপ্লাই বাটন ক্লিক হলে payload থেকে টেক্সট নেওয়া হয়
                    if message.get("quick_reply"):
                        message_text = message["quick_reply"]["payload"]
                    else:
                        message_text = message.get("text")

                    if message_text:
                        # ব্যাকগ্রাউন্ডে মেসেজ প্রসেস করার জন্য থ্রেড তৈরি
                        thread = threading.Thread(target=process_message, args=(sender_id, message_text))
                        thread.start()
                    else:
                        # যদি টেক্সট মেসেজ না হয়, কুইক রিপ্লাই সহ উত্তর পাঠানো
                        quick_replies = [
                            {
                                "content_type": "text",
                                "title": "📦 প্যাকেজ দেখুন",
                                "payload": "প্যাকেজগুলো দেখান",
                            },
                            {
                                "content_type": "text",
                                "title": "📞 কাস্টমার সাপোর্ট",
                                "payload": "কাস্টমার সাপোর্টে কথা বলতে চাই",
                            }
                        ]
                        send_message(sender_id, "দুঃখিত, আমি শুধু টেক্সট মেসেজ বুঝতে পারি।", quick_replies)

    return "EVENT_RECEIVED", 200

def process_message(sender_id, message_text):
    """AI থেকে উত্তর তৈরি করে এবং ব্যবহারকারীকে পাঠায়"""
    # টাইপিং ইন্ডিকেটর চালু করা
    send_action(sender_id, "typing_on")
    
    # ডাটাবেস থেকে সামারি এবং সাম্প্রতিক আলোচনা নিয়ে আসা
    summary = get_summary(sender_id)
    history = get_conversation_history(sender_id, limit=5)
    
    # AI থেকে উত্তর নেওয়া
    response_text = ask_speednet_ai(message_text, summary, history)
    
    # বর্তমান ইউজারের মেসেজ এবং AI-এর উত্তর ডাটাবেসে সেভ করা
    add_message_to_history(sender_id, "user", message_text)
    add_message_to_history(sender_id, "assistant", response_text)
    
    # টাইপিং ইন্ডিকেটর বন্ধ করা
    send_action(sender_id, "typing_off")
    
    # কুইক রিপ্লাই বাটন তৈরি
    quick_replies = [
        {
            "content_type": "text",
            "title": "📦 প্যাকেজ দেখুন",
            "payload": "প্যাকেজগুলো দেখান",
        },
        {
            "content_type": "text",
            "title": "📞 কাস্টমার সাপোর্ট",
            "payload": "কাস্টমার সাপোর্টে কথা বলতে চাই",
        }
    ]

    # ফেসবুকে রিপ্লাই পাঠানো
    send_message(sender_id, response_text, quick_replies)
    
    # পুরনো মেসেজ সামারি এবং ক্লিনআপ (ব্যাকগ্রাউন্ডে চলবে কারণ এটি থ্রেডের অংশ)
    prune_and_summarize(sender_id)

def send_message(recipient_id, message_text, quick_replies=None):
    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}
    message_data = {"text": message_text}
    if quick_replies:
        message_data["quick_replies"] = quick_replies

    data = {"recipient": {"id": recipient_id}, "message": message_data}
    try:
        response = requests.post(f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/me/messages", params=params, headers=headers, json=data)
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
        logging.info(f"Message sent to {recipient_id}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error sending message to {recipient_id}: {e}")
        # Log the response text from Facebook for easier debugging
        if 'response' in locals() and response.text:
            logging.error(f"Response Body: {response.text}")

def send_action(recipient_id, action):
    """Sender action (e.g., typing_on, typing_off) পাঠায়"""
    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}
    data = {"recipient": {"id": recipient_id}, "sender_action": action}
    try:
        requests.post(f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/me/messages", params=params, headers=headers, json=data).raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error sending action to {recipient_id}: {e}")

@app.route("/", methods=["GET"])
def home():
    return "স্পিড নেট এআই সার্ভার সচল আছে!"

if __name__ == "__main__":
    init_db()  # অ্যাপ চালু হওয়ার সময় ডাটাবেস ইনিশিয়ালাইজ করা
    print("--- স্পিড নেট এআই সার্ভার চালু হচ্ছে ---")
    is_debug = os.getenv("FLASK_DEBUG", "False").lower() in ("true", "1", "t")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=is_debug)