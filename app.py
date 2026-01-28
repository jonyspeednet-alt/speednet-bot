import os
import requests
import logging
import re
import time
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

# --- Globals for Throttling ---
user_last_message_time = {}
THROTTLE_SECONDS = 10

client = Groq(api_key=GROQ_API_KEY)

# --- Database Setup (SQLite) ---
def init_db():
    """ডাটাবেস এবং টেবিল তৈরি করে"""
    # বিঃদ্রঃ: Render-এর মতো প্ল্যাটফর্মে ফাইল সিস্টেম অস্থায়ী (ephemeral)।
    # সার্ভার রিস্টার্ট বা নতুন ডেপ্লয়মেন্টের পর এই SQLite ডাটাবেস ফাইলটি ডিলিট হয়ে যাবে।
    # স্থায়ী ডাটা সংরক্ষণের জন্য Render PostgreSQL বা অন্য কোনো ক্লাউড ডাটাবেস ব্যবহার করা উচিত।
    # check_same_thread=False ফ্ল্যাগটি থ্রেডিং সম্পর্কিত সমস্যা এড়ানোর জন্য যোগ করা হয়েছে,
    # কারণ Flask রিকোয়েস্টগুলো ভিন্ন ভিন্ন থ্রেডে চলতে পারে।
    conn = sqlite3.connect('conversations.db', check_same_thread=False)
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
            summary TEXT NOT NULL DEFAULT '',
            isp_user_id TEXT
        )
    ''')
    # স্কিমা মাইগ্রেশন: পুরনো টেবিলে isp_user_id কলাম যোগ করা
    try:
        cursor.execute('ALTER TABLE summaries ADD COLUMN isp_user_id TEXT')
    except sqlite3.OperationalError:
        pass # কলামটি আগে থেকেই আছে
    conn.commit()
    conn.close()

# Gunicorn বা প্রোডাকশন সার্ভারে অ্যাপ রান করার সময় ডাটাবেস ইনিশিয়ালাইজ করা
init_db()

def add_message_to_history(sender_id, role, content):
    """ডাটাবেসে মেসেজ সংরক্ষণ করে"""
    conn = sqlite3.connect('conversations.db', check_same_thread=False)
    conn.execute('INSERT INTO messages (sender_id, role, content) VALUES (?, ?, ?)', (sender_id, role, content))
    conn.commit()
    conn.close()

def get_conversation_history(sender_id, limit=10):
    """নির্দিষ্ট ইউজারের পুরনো মেসেজগুলো ডাটাবেস থেকে নিয়ে আসে"""
    conn = sqlite3.connect('conversations.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    messages = conn.execute('SELECT role, content FROM messages WHERE sender_id = ? ORDER BY timestamp DESC LIMIT ?', (sender_id, limit)).fetchall()
    conn.close()
    return [{"role": msg["role"], "content": msg["content"]} for msg in reversed(messages)]

def get_user_profile(sender_id):
    """ডাটাবেস থেকে ইউজারের সামারি এবং ISP ইউজার আইডি নিয়ে আসে"""
    conn = sqlite3.connect('conversations.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT summary, isp_user_id FROM summaries WHERE sender_id = ?', (sender_id,)).fetchone()
    conn.close()
    if row:
        return {"summary": row["summary"], "isp_user_id": row["isp_user_id"]}
    return {"summary": "", "isp_user_id": None}

def save_summary(sender_id, summary):
    """ডাটাবেসে শুধুমাত্র সামারি আপডেট করে"""
    conn = sqlite3.connect('conversations.db', check_same_thread=False)
    conn.execute('INSERT OR IGNORE INTO summaries (sender_id) VALUES (?)', (sender_id,))
    conn.execute('UPDATE summaries SET summary = ? WHERE sender_id = ?', (summary, sender_id))
    conn.commit()
    conn.close()

def save_isp_user_id(sender_id, isp_user_id):
    """ডাটাবেসে শুধুমাত্র ISP ইউজার আইডি আপডেট করে"""
    conn = sqlite3.connect('conversations.db', check_same_thread=False)
    conn.execute('INSERT OR IGNORE INTO summaries (sender_id) VALUES (?)', (sender_id,))
    conn.execute('UPDATE summaries SET isp_user_id = ? WHERE sender_id = ?', (isp_user_id, sender_id))
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
            model="llama-3.1-8b-instant",
            max_tokens=200
        )
        return completion.choices[0].message.content
    except Exception as e:
        logging.error(f"Summarization failed: {e}")
        return current_summary

def prune_and_summarize(sender_id):
    """মেসেজ সংখ্যা বেশি হলে পুরনো মেসেজ সামারি করে ডিলিট করে"""
    conn = sqlite3.connect('conversations.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    count = conn.execute('SELECT COUNT(*) FROM messages WHERE sender_id = ?', (sender_id,)).fetchone()[0]
    
    if count > 10:  # যদি ১০টির বেশি মেসেজ থাকে
        old_msgs = conn.execute('SELECT id, role, content FROM messages WHERE sender_id = ? ORDER BY timestamp ASC LIMIT 5', (sender_id,)).fetchall()
        if old_msgs:
            ids_to_delete = [msg['id'] for msg in old_msgs]
            text_to_summarize = "\n".join([f"{msg['role']}: {msg['content']}" for msg in old_msgs])
            current_summary = get_user_profile(sender_id).get("summary", "")
            new_summary = generate_summary(current_summary, text_to_summarize)
            save_summary(sender_id, new_summary)
            placeholders = ','.join('?' * len(ids_to_delete))
            conn.execute(f'DELETE FROM messages WHERE id IN ({placeholders})', ids_to_delete)
            conn.commit()
            logging.info(f"Summarized and pruned {len(ids_to_delete)} messages for {sender_id}")
    conn.close()

# --- Load and Parse ISP context once at startup ---
# ৫. প্রম্পট ক্যাশিং: training_data.txt ফাইলটি শুধুমাত্র অ্যাপ চালু হওয়ার সময় একবার লোড এবং পার্স করা হয়।
# এর ফলে প্রতিটি রিকোয়েস্টে ফাইলটি পুনরায় প্রসেস করার প্রয়োজন হয় না, যা পারফরম্যান্স অপটিমাইজ করে।
def get_isp_context():
    try:
        with open("training_data.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logging.error("training_data.txt not found!")
        return "স্পিড নেট সম্পর্কিত তথ্য পাওয়া যায়নি।"

def parse_isp_context(text):
    """Splits the training data into a dictionary of sections for dynamic loading."""
    sections = {}
    parts = text.split('\n---\n')
    for part in parts:
        lines = part.strip().split('\n')
        if lines and '##' in lines[0]:
            key = lines[0].split('##')[1].strip()
            sections[key] = part.strip()
    return sections

PARSED_ISP_CONTEXT = parse_isp_context(get_isp_context())

# Keyword mapping for dynamic context selection
CONTEXT_KEYWORDS = {
    'Speed Net Khulna – সংক্ষিপ্ত প্রোফাইল': ['অফিস', 'ঠিকানা', 'contact', 'address', 'office'],
    'নতুন সংযোগ (New Connection)': ['সংযোগ', 'নতুন', 'connection', 'লাইন'],
    'কাভারেজ / লোকেশন সংক্রান্ত': ['কাভারেজ', 'এরিয়া', 'লোকেশন', 'location', 'area'],
    'প্যাকেজ ও বিলিং তথ্য': ['প্যাকেজ', 'দাম', 'টাকা', 'price', 'package', 'rate', 'খরচ'],
    'বিকাশ / নগদ পেমেন্ট নির্দেশনা': ['বিল', 'bill', 'payment', 'pay', 'বিকাশ', 'নগদ', 'bkash', 'nagad', 'পরিশোধ'],
    'টেকনিক্যাল / স্পিড সমস্যা': ['সমস্যা', 'problem', 'slow', 'স্পিড', 'speed', 'পিং', 'ping', 'disconnect', ' পাচ্ছে না', 'লাল বাতি'],
    'পাবলিক IP / IPv6': ['ip', 'ipv6', 'public', 'real'],
    'কাস্টমার কেয়ার ও যোগাযোগ': ['যোগাযোগ', 'care', 'সাপোর্ট', 'support', 'নম্বর', 'number', 'কথা বল'],
    'FTP / কনটেন্ট / গ্রুপ সংক্রান্ত': ['ftp', 'movie', 'server', 'সার্ভার', 'মুভি', 'গ্রুপ', 'group'],
    'অফার ও নোটিশ': ['অফার', 'offer', 'notice', 'নোটিশ', 'ডিসকাউন্ট', 'discount'],
}

def get_dynamic_context(user_question, parsed_context):
    """Selects relevant sections from the context based on keywords."""
    relevant_sections = []
    question_lower = user_question.lower()
    found_keys = set()

    for section_key, keywords in CONTEXT_KEYWORDS.items():
        for keyword in keywords:
            if keyword in question_lower:
                if section_key in parsed_context and section_key not in found_keys:
                    relevant_sections.append(parsed_context[section_key])
                    found_keys.add(section_key)
                    break
    
    if not relevant_sections:
        return "সাধারণ তথ্য এই মুহূর্তে উপলব্ধ নেই। অনুগ্রহ করে আমাদের হটলাইনে (09639333111) যোগাযোগ করুন।"
    return "\n\n---\n\n".join(relevant_sections)

def ask_speednet_ai(user_question, summary, dynamic_context, isp_user_id=None):
    # টোকেন ম্যানেজমেন্ট নোট:
    # এখন ব্যবহারকারীর প্রশ্নের উপর ভিত্তি করে training_data.txt থেকে শুধুমাত্র প্রাসঙ্গিক অংশ (Dynamic Context) পাঠানো হচ্ছে।
    # এটি টোকেন ব্যবহার কমায় এবং অপ্রাসঙ্গিক তথ্য পাঠানো থেকে বিরত থাকে।
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
f"১৫. **ইউজার প্রোফাইলিং:** যদি গ্রাহকের ISP ইউজার আইডি জানা না থাকে (বর্তমান আইডি: {'এখনও জানা যায়নি' if not isp_user_id else isp_user_id}) এবং সে বিল, পেমেন্ট বা ব্যক্তিগত কোনো তথ্য জানতে চায়, তাহলে তাকে বিনয়ের সাথে তার ইউজার আইডি জিজ্ঞেস করবে। যেমন: 'আপনার বিল চেক করার জন্য অনুগ্রহ করে আপনার ইউজার আইডিটি দিন।' \n"


        f"--- ডেটা সেকশন ---\n"
        f"### তথ্য (Knowledge Base):\n"
        f"{dynamic_context}\n\n"
        
        f"### পূর্ববর্তী আলোচনার সারাংশ (Previous Conversation Summary):\n"
        f"{summary}\n"
        f"--- ডেটা সেকশন সমাপ্ত ---"
    )

    # সিস্টেম প্রম্পট এবং সাম্প্রতিক আলোচনা দিয়ে মেসেজ লিস্ট তৈরি
    messages = [{"role": "system", "content": system_prompt}]
    # messages.extend(history) # কনভারসেশনাল সামারি ব্যবহারের জন্য সম্পূর্ণ হিস্ট্রি পাঠানো বন্ধ করা হয়েছে।
    messages.append({"role": "user", "content": user_question})

    try:
        completion = client.chat.completions.create(
            messages=messages,
            model="llama-3.1-8b-instant",
            max_tokens=450,
            temperature=0.5
        )
        return completion.choices[0].message.content
    except Exception as e:
        logging.error(f"Groq API Error: {e}")
        # ৪. ফলব্যাক লজিক: এআই রেসপন্স ফেইল করলে বিকল্প উত্তর
        return "দুঃখিত, আমি এই মুহূর্তে একটু বেশি ব্যস্ত। জরুরি প্রয়োজনে আমাদের হটলাইনে (09639333111) কল করুন অথবা আপনার নম্বরটি দিন, আমরা কল ব্যাক করছি।"

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
    """Handles incoming messages with throttling, keyword routing, and AI processing."""
    # ৫. থ্রোটলিং: ব্যবহারকারীকে দ্রুত মেসেজ পাঠানো থেকে বিরত রাখা
    current_time = time.time()
    if sender_id in user_last_message_time and current_time - user_last_message_time[sender_id] < THROTTLE_SECONDS:
        logging.warning(f"Throttling user {sender_id}. Ignoring message.")
        return # কোনো রিপ্লাই না দিয়ে тихо থাকা
    user_last_message_time[sender_id] = current_time

    # ২. ইউজার প্রোফাইলিং: ইউজার আইডি শনাক্তকরণ এবং সেভ করা
    # উদাহরণ: "আমার আইডি xyz123" বা "id: xyz123"
    match = re.search(r'(?i)(id|আইডি)\s*[:is\s]*([a-zA-Z0-9\-_]+)', message_text)
    if match:
        isp_id = match.group(2)
        save_isp_user_id(sender_id, isp_id)
        response_text = f"ধন্যবাদ! আপনার ইউজার আইডি '{isp_id}' সেভ করা হয়েছে। এখন থেকে আপনার অ্যাকাউন্টের বিষয়ে দ্রুত সহায়তা করতে পারব।"
        add_message_to_history(sender_id, "user", message_text)
        add_message_to_history(sender_id, "assistant", response_text)
        send_message_with_quick_replies(sender_id, response_text)
        return

    # ৪. সাধারণ সম্ভাষণ ফিল্টার
    GREETINGS = {
        "hi": "হ্যালো! স্পিডনেট খুলনায় আপনাকে স্বাগতম। আমি স্পিডি, আপনার ডিজিটাল অ্যাসিস্ট্যান্ট।",
        "hello": "জি, হ্যালো! আমি স্পিডি। কীভাবে আপনাকে সাহায্য করতে পারি?",
        "কেমন আছেন": "ধন্যবাদ, আমি ভালো আছি। আপনার সেবায় আমি حاضر।",
    }
    if message_text.lower() in GREETINGS:
        response_text = GREETINGS[message_text.lower()]
        add_message_to_history(sender_id, "user", message_text)
        add_message_to_history(sender_id, "assistant", response_text)
        send_message_with_quick_replies(sender_id, response_text)
        return

    # ১. কীওয়ার্ড-বেজড রাউটিং এবং ইমেজ সাপোর্ট
    message_lower = message_text.lower()

    # প্যাকেজের জন্য ইমেজ পাঠানো
    if any(keyword in message_lower for keyword in ["প্যাকেজ", "দাম", "price", "package"]):
        # ৩. ইমেজ এবং ডকুমেন্ট সাপোর্ট: প্যাকেজের জন্য ইমেজ পাঠানো
        # এই URLটি আপনার প্যাকেজ চার্টের আসল URL দিয়ে পরিবর্তন করতে হবে
        package_image_url = "https://i.ibb.co/tQk3VzW/speednet-packages.png" # Placeholder URL
        send_image(sender_id, package_image_url, "আমাদের প্যাকেজগুলো সম্পর্কে আরও জানতে চান?")
        add_message_to_history(sender_id, "user", message_text)
        add_message_to_history(sender_id, "assistant", "[প্যাকেজের ছবি পাঠানো হয়েছে]")
        return

    # অন্যান্য কীওয়ার্ডের জন্য ফিক্সড উত্তর
    FIXED_RESPONSES = {
        "বিল দেওয়ার নিয়ম": "আমাদের বিল বিকাশে অথবা নগদে পেমেন্ট করতে পারেন।\n\nbKash Payment:\n1. bKash App থেকে Pay Bill সিলেক্ট করুন\n2. Merchant No: 01400003070\n3. Amount + 1.5% চার্জ দিন\n4. Reference-এ আপনার Billing ID দিন\n5. PIN দিয়ে কনফার্ম করুন।",
        "অফিস কোথায়?": "আমাদের অফিস ৮৩/৩, গগন বাবু রোড, খুলনা। যেকোনো প্রয়োজনে অফিস চলাকালীন সময়ে আসতে পারেন।",
    }
    for keyword, response in FIXED_RESPONSES.items():
        if keyword in message_text:
            add_message_to_history(sender_id, "user", message_text)
            add_message_to_history(sender_id, "assistant", response)
            send_message_with_quick_replies(sender_id, response)
            return

    try:
        # টাইপিং ইন্ডিকেটর চালু করা
        send_action(sender_id, "typing_on")
        
        # প্রোফাইল থেকে সামারি এবং ইউজার আইডি নেওয়া
        user_profile = get_user_profile(sender_id)
        summary = user_profile.get("summary", "")
        isp_user_id = user_profile.get("isp_user_id")
        
        # ২. ডাইনামিক কন্টেক্সট লোডিং
        dynamic_context = get_dynamic_context(message_text, PARSED_ISP_CONTEXT)
        
        # AI থেকে উত্তর নেওয়া
        response_text = ask_speednet_ai(message_text, summary, dynamic_context, isp_user_id)
        
        # বর্তমান ইউজারের মেসেজ এবং AI-এর উত্তর ডাটাবেসে সেভ করা
        add_message_to_history(sender_id, "user", message_text)
        add_message_to_history(sender_id, "assistant", response_text)
        
        # টাইপিং ইন্ডিকেটর বন্ধ করা
        send_action(sender_id, "typing_off")

        # ফেসবুকে রিপ্লাই পাঠানো
        send_message_with_quick_replies(sender_id, response_text)
    
        # পুরনো মেসেজ সামারি এবং ক্লিনআপ (ব্যাকগ্রাউন্ডে চলবে)
        prune_and_summarize(sender_id)

    except Exception as e:
        logging.error(f"Error in process_message AI block: {e}")
        send_action(sender_id, "typing_off")
        # ৪. ফলব্যাক লজিক: এআই রেসপন্স ফেইল করলে বিকল্প উত্তর
        fallback_message = "দুঃখিত, আমি এই মুহূর্তে একটু বেশি ব্যস্ত। জরুরি প্রয়োজনে আমাদের হটলাইনে (09639333111) কল করুন অথবা আপনার নম্বরটি দিন, আমরা কল ব্যাক করছি।"
        send_message_with_quick_replies(sender_id, fallback_message)

def send_message_with_quick_replies(recipient_id, message_text):
    """কুইক রিপ্লাই বাটনসহ মেসেজ পাঠায়"""
    quick_replies = [
        {
            "content_type": "text",
            "title": "📦 প্যাকেজ দেখুন",
            "payload": "প্যাকেজ",
        },
        {
            "content_type": "text",
            "title": "💳 বিল দেওয়ার নিয়ম",
            "payload": "বিল দেওয়ার নিয়ম",
        },
        {
            "content_type": "text",
            "title": "🏢 অফিস কোথায়?",
            "payload": "অফিস কোথায়?",
        },
        {
            "content_type": "text",
            "title": "� কাস্টমার সাপোর্ট",
            "payload": "কাস্টমার সাপোর্টে কথা বলতে চাই",
        }
    ]
    send_message(recipient_id, message_text, quick_replies)

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

def send_image(recipient_id, image_url, text_after_image=None):
    """ফেসবুক মেসেঞ্জারে ছবি পাঠায়"""
    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}
    
    image_data = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "image",
                "payload": { "url": image_url, "is_reusable": True }
            }
        }
    }
    try:
        response = requests.post(f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/me/messages", params=params, headers=headers, json=image_data)
        response.raise_for_status()
        logging.info(f"Image sent to {recipient_id}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error sending image to {recipient_id}: {e}")
        if 'response' in locals() and response.text:
            logging.error(f"Response Body: {response.text}")

    if text_after_image:
        send_message_with_quick_replies(recipient_id, text_after_image)

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