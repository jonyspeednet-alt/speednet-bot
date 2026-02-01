import os
import requests
import logging
import re
import time
import threading
from flask import Flask, request, jsonify, render_template
import psycopg2
from psycopg2.extras import RealDictCursor
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

# টেমপ্লেট ফোল্ডার হিসেবে বর্তমান ডিরেক্টরি (.) সেট করা হচ্ছে যাতে dashboard.html খুঁজে পায়
app = Flask(__name__, template_folder='.')

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration from Environment Variables ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
# PAGE_ACCESS_TOKEN এখন ডাইনামিকালি ডাটাবেস থেকে আসবে, তবে সিডিংয়ের জন্য এনভায়রনমেন্ট থেকে নেওয়া হতে পারে
FACEBOOK_API_VERSION = os.getenv("FACEBOOK_API_VERSION", "v19.0")
FACEBOOK_APP_ID = os.getenv("FACEBOOK_APP_ID")
 # ড্যাশবোর্ডের জন্য অ্যাপ আইডি

# --- Globals for Throttling ---
user_last_message_time = {}
THROTTLE_SECONDS = 10

client = Groq(api_key=GROQ_API_KEY)

# --- Database Setup (PostgreSQL) ---
def get_db_connection():
    """PostgreSQL ডাটাবেস কানেকশন তৈরি করে"""
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    return conn

def init_db():
    """ডাটাবেস এবং টেবিল তৈরি করে"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # PostgreSQL সিনট্যাক্স ব্যবহার করা হয়েছে (SERIAL, TIMESTAMP)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            page_id TEXT,
            sender_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS summaries (
            page_id TEXT,
            sender_id TEXT PRIMARY KEY,
            summary TEXT NOT NULL DEFAULT '',
            isp_user_id TEXT
        )
    ''')
    # SaaS-এর জন্য কোম্পানি টেবিল
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS companies (
            id SERIAL PRIMARY KEY,
            page_id VARCHAR(255) UNIQUE NOT NULL,
            access_token TEXT NOT NULL,
            business_info TEXT,
            bot_name VARCHAR(255)
        )
    ''')

    # স্কিমা মাইগ্রেশন: কলাম যোগ করা (PostgreSQL স্টাইল)
    try:
        cursor.execute('ALTER TABLE summaries ADD COLUMN isp_user_id TEXT')
    except psycopg2.errors.DuplicateColumn:
        conn.rollback() # এরর হলে রোলব্যাক করতে হবে
    else:
        conn.commit()

    try:
        cursor.execute('ALTER TABLE messages ADD COLUMN page_id TEXT')
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()
    else:
        conn.commit()

    try:
        cursor.execute('ALTER TABLE summaries ADD COLUMN page_id TEXT')
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()
    else:
        conn.commit()

    try:
        cursor.execute('ALTER TABLE summaries ADD COLUMN user_name TEXT')
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    try:
        cursor.execute('ALTER TABLE companies ADD COLUMN page_name TEXT')
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()
    else:
        conn.commit()

    conn.commit()
    conn.close()

def seed_db():
    """এনভায়রনমেন্ট ভেরিয়েবল থেকে ডিফল্ট কোম্পানি সেটআপ করে (মাইগ্রেশনের সুবিধার্থে)"""
    default_token = os.getenv("PAGE_ACCESS_TOKEN")
    if default_token:
        try:
            # ফেসবুক গ্রাফ এপিআই থেকে পেজ আইডি বের করা
            resp = requests.get(f"https://graph.facebook.com/me?access_token={default_token}")
            if resp.status_code == 200:
                page_data = resp.json()
                page_id = page_data.get("id")
                page_name = page_data.get("name", "Unknown Page")
                
                # ট্রেনিং ডাটা লোড করা
                business_info = "স্পিড নেট সম্পর্কিত তথ্য পাওয়া যায়নি।"
                try:
                    with open("training_data.txt", "r", encoding="utf-8") as f:
                        business_info = f.read()
                except FileNotFoundError:
                    pass

                conn = get_db_connection()
                cursor = conn.cursor()
                # যদি কোম্পানি না থাকে তবেই ইনসার্ট করবে
                cursor.execute('''
                    INSERT INTO companies (page_id, access_token, business_info, bot_name, page_name)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (page_id) DO NOTHING
                ''', (page_id, default_token, business_info, "স্পিড নেট", page_name))
                conn.commit()
                conn.close()
                logging.info(f"Default company seeded: {page_data.get('name')} ({page_id})")
        except Exception as e:
            logging.error(f"Seeding failed: {e}")

def get_company_config(page_id):
    """ডাটাবেস থেকে নির্দিষ্ট কোম্পানির কনফিগারেশন নিয়ে আসে"""
    # সরাসরি ডাটাবেস থেকে কনফিগারেশন আনা (Redis ক্যাশ ছাড়া)
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute('SELECT access_token, business_info, bot_name FROM companies WHERE page_id = %s', (page_id,))
    row = cursor.fetchone()
    conn.close()
    
    return row

def add_message_to_history(page_id, sender_id, role, content):
    """ডাটাবেসে মেসেজ সংরক্ষণ করে"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO messages (page_id, sender_id, role, content) VALUES (%s, %s, %s, %s)', (page_id, sender_id, role, content))
    conn.commit()
    conn.close()

def get_conversation_history(page_id, sender_id, limit=10):
    """নির্দিষ্ট ইউজারের পুরনো মেসেজগুলো ডাটাবেস থেকে নিয়ে আসে"""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    # page_id ফিল্টার যোগ করা হয়েছে
    cursor.execute('SELECT role, content FROM messages WHERE sender_id = %s AND (page_id = %s OR page_id IS NULL) ORDER BY timestamp DESC LIMIT %s', (sender_id, page_id, limit))
    messages = cursor.fetchall()
    conn.close()
    return [{"role": msg["role"], "content": msg["content"]} for msg in reversed(messages)]

def get_user_profile(page_id, sender_id):
    """ডাটাবেস থেকে ইউজারের সামারি এবং ISP ইউজার আইডি নিয়ে আসে"""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute('SELECT summary, isp_user_id, user_name FROM summaries WHERE sender_id = %s', (sender_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"summary": row["summary"], "isp_user_id": row["isp_user_id"], "user_name": row.get("user_name")}
    return {"summary": "", "isp_user_id": None, "user_name": None}

def update_user_name(page_id, sender_id, user_name):
    """ডাটাবেসে ইউজারের নাম আপডেট করে"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO summaries (sender_id, page_id, user_name) VALUES (%s, %s, %s)
        ON CONFLICT (sender_id) DO UPDATE SET user_name = EXCLUDED.user_name, page_id = EXCLUDED.page_id
    ''', (sender_id, page_id, user_name))
    conn.commit()
    conn.close()

def save_summary(page_id, sender_id, summary):
    """ডাটাবেসে শুধুমাত্র সামারি আপডেট করে"""
    conn = get_db_connection()
    cursor = conn.cursor()
    # PostgreSQL Upsert (ON CONFLICT)
    cursor.execute('''
        INSERT INTO summaries (sender_id, page_id, summary) VALUES (%s, %s, %s)
        ON CONFLICT (sender_id) DO UPDATE SET summary = EXCLUDED.summary, page_id = EXCLUDED.page_id
    ''', (sender_id, page_id, summary))
    conn.commit()
    conn.close()

def save_isp_user_id(page_id, sender_id, isp_user_id):
    """ডাটাবেসে শুধুমাত্র ISP ইউজার আইডি আপডেট করে"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO summaries (sender_id, page_id, isp_user_id) VALUES (%s, %s, %s)
        ON CONFLICT (sender_id) DO UPDATE SET isp_user_id = EXCLUDED.isp_user_id, page_id = EXCLUDED.page_id
    ''', (sender_id, page_id, isp_user_id))
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

def prune_and_summarize(page_id, sender_id):
    """মেসেজ সংখ্যা বেশি হলে পুরনো মেসেজ সামারি করে ডিলিট করে"""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute('SELECT COUNT(*) as count FROM messages WHERE sender_id = %s AND page_id = %s', (sender_id, page_id))
    count = cursor.fetchone()['count']
    
    if count > 10:  # যদি ১০টির বেশি মেসেজ থাকে
        cursor.execute('SELECT id, role, content FROM messages WHERE sender_id = %s AND page_id = %s ORDER BY timestamp ASC LIMIT 5', (sender_id, page_id))
        old_msgs = cursor.fetchall()
        if old_msgs:
            ids_to_delete = [msg['id'] for msg in old_msgs]
            text_to_summarize = "\n".join([f"{msg['role']}: {msg['content']}" for msg in old_msgs])
            current_summary = get_user_profile(page_id, sender_id).get("summary", "")
            new_summary = generate_summary(current_summary, text_to_summarize)
            save_summary(page_id, sender_id, new_summary)
            # PostgreSQL এ tuple ব্যবহার করে IN ক্লজ
            cursor.execute('DELETE FROM messages WHERE id IN %s', (tuple(ids_to_delete),))
            conn.commit()
            logging.info(f"Summarized and pruned {len(ids_to_delete)} messages for {sender_id}")
    conn.close()

# --- Context Parsing Logic (Now Dynamic) ---

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

def get_facebook_user_name(sender_id, access_token):
    """ফেসবুক গ্রাফ এপিআই থেকে ইউজারের নাম সংগ্রহ করে"""
    try:
        url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/{sender_id}?fields=first_name,last_name&access_token={access_token}"
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            first_name = data.get('first_name', '')
            last_name = data.get('last_name', '')
            return f"{first_name} {last_name}".strip()
    except Exception as e:
        logging.error(f"Failed to fetch user name: {e}")
    return None

def ask_speednet_ai(user_question, summary, dynamic_context, bot_name, isp_user_id=None, user_name=None):
    # টোকেন ম্যানেজমেন্ট নোট:
    # এখন ব্যবহারকারীর প্রশ্নের উপর ভিত্তি করে ডাটাবেস থেকে শুধুমাত্র প্রাসঙ্গিক অংশ (Dynamic Context) পাঠানো হচ্ছে।
    # এটি টোকেন ব্যবহার কমায় এবং অপ্রাসঙ্গিক তথ্য পাঠানো থেকে বিরত থাকে।
    # --- বিস্তারিত সিস্টেম প্রম্পট ---
    
    greeting_instruction = ""
    if user_name:
        greeting_instruction = f"তুমি এখন কথা বলছ '{user_name}'-এর সাথে। উত্তরের শুরুতে বা প্রয়োজনে তাকে নাম ধরে সম্বোধন করবে (খুব বেশি বার নয়, স্বাভাবিকভাবে)।\n"

    system_prompt = (
        f"### পার্সোনা (Persona)\n"
        f"তুমি একজন দক্ষ ও বিনয়ী এআই অ্যাসিস্ট্যান্ট। তোমার নাম '{bot_name}'।\n"
        f"{greeting_instruction}"
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
                    # ১. মাল্টি-টেন্যান্ট হ্যান্ডলিং: recipient_id (Page ID) চেক করা
                    recipient_id = messaging_event.get("recipient", {}).get("id")
                    sender_id = messaging_event["sender"]["id"]
                    message = messaging_event["message"]

                    # কোম্পানি কনফিগারেশন লোড করা
                    company_config = get_company_config(recipient_id)
                    if not company_config:
                        logging.warning(f"Unknown Page ID: {recipient_id}. Ignoring message.")
                        continue

                    # কুইক রিপ্লাই বাটন ক্লিক হলে payload থেকে টেক্সট নেওয়া হয়
                    if message.get("quick_reply"):
                        message_text = message["quick_reply"]["payload"]
                    else:
                        message_text = message.get("text")

                    if message_text:
                        # ব্যাকগ্রাউন্ডে মেসেজ প্রসেস করার জন্য থ্রেড তৈরি
                        thread = threading.Thread(target=process_message, args=(recipient_id, sender_id, message_text, company_config))
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
                        send_message(sender_id, "দুঃখিত, আমি শুধু টেক্সট মেসেজ বুঝতে পারি।", company_config['access_token'], quick_replies)

    return "EVENT_RECEIVED", 200

def process_message(page_id, sender_id, message_text, company_config):
    """Handles incoming messages with throttling, keyword routing, and AI processing."""
    access_token = company_config['access_token']
    business_info = company_config['business_info']
    bot_name = company_config['bot_name']

    # ৫. থ্রোটলিং: ইন-মেমোরি ডিকশনারি ব্যবহার করে
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
        save_isp_user_id(page_id, sender_id, isp_id)
        response_text = f"ধন্যবাদ! আপনার ইউজার আইডি '{isp_id}' সেভ করা হয়েছে। এখন থেকে আপনার অ্যাকাউন্টের বিষয়ে দ্রুত সহায়তা করতে পারব।"
        add_message_to_history(page_id, sender_id, "user", message_text)
        add_message_to_history(page_id, sender_id, "assistant", response_text)
        send_message_with_quick_replies(sender_id, response_text, access_token)
        return

    # ৪. সাধারণ সম্ভাষণ ফিল্টার
    GREETINGS = {
        "hi": "হ্যালো! স্পিডনেট খুলনায় আপনাকে স্বাগতম। আমি স্পিডি, আপনার ডিজিটাল অ্যাসিস্ট্যান্ট।",
        "hello": "জি, হ্যালো! আমি স্পিডি। কীভাবে আপনাকে সাহায্য করতে পারি?",
        "কেমন আছেন": "ধন্যবাদ, আমি ভালো আছি। আপনার সেবায় আমি حاضر।",
    }
    if message_text.lower() in GREETINGS:
        response_text = GREETINGS[message_text.lower()]
        add_message_to_history(page_id, sender_id, "user", message_text)
        add_message_to_history(page_id, sender_id, "assistant", response_text)
        send_message_with_quick_replies(sender_id, response_text, access_token)
        return

    # ১. কীওয়ার্ড-বেজড রাউটিং এবং ইমেজ সাপোর্ট
    message_lower = message_text.lower()

    # প্যাকেজের জন্য ইমেজ পাঠানো
    if any(keyword in message_lower for keyword in ["প্যাকেজ", "দাম", "price", "package"]):
        # ৩. ইমেজ এবং ডকুমেন্ট সাপোর্ট: প্যাকেজের জন্য ইমেজ পাঠানো (টেক্সট ফলব্যাক সহ)
        # নিচের URLটি একটি ব্রোকেন প্লেসহোল্ডার। আপনার প্যাকেজ চার্টের সঠিক URL দিয়ে এটি পরিবর্তন করুন।
        # package_image_url = "https://your-new-image-url.com/packages.png"
        
        # # --- ইমেজ পাঠানোর কোড (সঠিক URL পেলে এই অংশটি আনকমেন্ট করুন এবং নিচের টেক্সট অংশটি মুছে দিন) ---
        # send_image(sender_id, package_image_url, "আমাদের প্যাকেজগুলো সম্পর্কে আরও জানতে চান?")
        # add_message_to_history(sender_id, "user", message_text)
        # add_message_to_history(sender_id, "assistant", "[প্যাকেজের ছবি পাঠানো হয়েছে]")

        # --- টেক্সট-ভিত্তিক ফলব্যাক (যেহেতু ইমেজ URL কাজ করছে না) ---
        package_text = (
            "আমাদের প্যাকেজগুলো নিচে দেওয়া হলো:\n"
            "- 20 Mbps ➝ মাত্র 525 টাকা (ভ্যাট সহ)\n- 30 Mbps ➝ মাত্র 630 টাকা (ভ্যাট সহ)\n- 50 Mbps ➝ মাত্র 785 টাকা (ভ্যাট সহ)\n- 80 Mbps ➝ মাত্র 1050 টাকা (ভ্যাট সহ)\n- 100 Mbps ➝ মাত্র 1205 টাকা (ভ্যাট সহ)\n- 150 Mbps ➝ মাত্র 1730 টাকা (ভ্যাট সহ)\n\n"
            "সব প্যাকেজে YouTube/BDIX/Facebook/FTP স্পিড 100 Mbps পর্যন্ত পাওয়া যায়।"
        )
        send_message_with_quick_replies(sender_id, package_text, access_token)
        add_message_to_history(page_id, sender_id, "user", message_text)
        add_message_to_history(page_id, sender_id, "assistant", package_text)
        return

    # অন্যান্য কীওয়ার্ডের জন্য ফিক্সড উত্তর
    FIXED_RESPONSES = {
        "বিল দেওয়ার নিয়ম": "আমাদের বিল বিকাশে অথবা নগদে পেমেন্ট করতে পারেন।\n\nbKash Payment:\n1. bKash App থেকে Pay Bill সিলেক্ট করুন\n2. Merchant No: 01400003070\n3. Amount + 1.5% চার্জ দিন\n4. Reference-এ আপনার Billing ID দিন\n5. PIN দিয়ে কনফার্ম করুন।",
        "অফিস কোথায়?": "আমাদের অফিস ৮৩/৩, গগন বাবু রোড, খুলনা। যেকোনো প্রয়োজনে অফিস চলাকালীন সময়ে আসতে পারেন।",
    }
    for keyword, response in FIXED_RESPONSES.items():
        if keyword in message_text:
            add_message_to_history(page_id, sender_id, "user", message_text)
            add_message_to_history(page_id, sender_id, "assistant", response)
            send_message_with_quick_replies(sender_id, response, access_token)
            return

    try:
        # টাইপিং ইন্ডিকেটর চালু করা
        send_action(sender_id, "typing_on", access_token)
        
        # প্রোফাইল থেকে সামারি এবং ইউজার আইডি নেওয়া
        user_profile = get_user_profile(page_id, sender_id)
        summary = user_profile.get("summary", "")
        isp_user_id = user_profile.get("isp_user_id")
        user_name = user_profile.get("user_name")

        # নাম না থাকলে ফেসবুক থেকে আনা
        if not user_name:
            user_name = get_facebook_user_name(sender_id, access_token)
            if user_name:
                update_user_name(page_id, sender_id, user_name)
        
        # ২. ডাইনামিক কন্টেক্সট লোডিং
        # কোম্পানির বিজনেস ইনফো পার্স করা (SaaS-এর জন্য এটি প্রতি রিকোয়েস্টে বা ক্যাশ থেকে হতে পারে)
        parsed_context = parse_isp_context(business_info)
        dynamic_context = get_dynamic_context(message_text, parsed_context)
        
        # AI থেকে উত্তর নেওয়া
        response_text = ask_speednet_ai(message_text, summary, dynamic_context, bot_name, isp_user_id, user_name)
        
        # বর্তমান ইউজারের মেসেজ এবং AI-এর উত্তর ডাটাবেসে সেভ করা
        add_message_to_history(page_id, sender_id, "user", message_text)
        add_message_to_history(page_id, sender_id, "assistant", response_text)
        
        # টাইপিং ইন্ডিকেটর বন্ধ করা
        send_action(sender_id, "typing_off", access_token)

        # ফেসবুকে রিপ্লাই পাঠানো
        send_message_with_quick_replies(sender_id, response_text, access_token)
    
        # পুরনো মেসেজ সামারি এবং ক্লিনআপ (ব্যাকগ্রাউন্ডে চলবে)
        prune_and_summarize(page_id, sender_id)

    except Exception as e:
        logging.error(f"Error in process_message AI block: {e}")
        send_action(sender_id, "typing_off", access_token)
        # ৪. ফলব্যাক লজিক: এআই রেসপন্স ফেইল করলে বিকল্প উত্তর
        fallback_message = "দুঃখিত, আমি এই মুহূর্তে একটু বেশি ব্যস্ত। জরুরি প্রয়োজনে আমাদের হটলাইনে (09639333111) কল করুন অথবা আপনার নম্বরটি দিন, আমরা কল ব্যাক করছি।"
        send_message_with_quick_replies(sender_id, fallback_message, access_token)

def send_message_with_quick_replies(recipient_id, message_text, access_token):
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
    send_message(recipient_id, message_text, access_token, quick_replies)

def send_message(recipient_id, message_text, access_token, quick_replies=None):
    params = {"access_token": access_token}
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

def send_image(recipient_id, image_url, access_token, text_after_image=None):
    """ফেসবুক মেসেঞ্জারে ছবি পাঠায়"""
    params = {"access_token": access_token}
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
        send_message_with_quick_replies(recipient_id, text_after_image, access_token)

def send_action(recipient_id, action, access_token):
    """Sender action (e.g., typing_on, typing_off) পাঠায়"""
    params = {"access_token": access_token}
    headers = {"Content-Type": "application/json"}
    data = {"recipient": {"id": recipient_id}, "sender_action": action}
    try:
        requests.post(f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/me/messages", params=params, headers=headers, json=data).raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error sending action to {recipient_id}: {e}")

@app.route("/", methods=["GET"])
def home():
    return "স্পিড নেট এআই সার্ভার সচল আছে!"

# --- Dashboard Route ---
@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html", app_id=FACEBOOK_APP_ID)

@app.route("/config")
def config():
    """Frontend-এর জন্য কনফিগারেশন প্রদান করে"""
    return jsonify({"facebook_app_id": os.getenv("FACEBOOK_APP_ID", "")})

@app.route("/test-chat", methods=["POST"])
def test_chat():
    """ডাটাবেসে সেভ করার আগে বটের উত্তর প্রিভিউ করার জন্য"""
    data = request.json
    message_text = data.get("message")
    business_info = data.get("business_info")
    bot_name = data.get("bot_name", "AI Assistant")

    if not message_text or not business_info:
        return jsonify({"error": "Message and Business Info required"}), 400

    # ডাইনামিক কন্টেক্সট পার্সিং (সরাসরি ইনপুট থেকে)
    parsed_context = parse_isp_context(business_info)
    dynamic_context = get_dynamic_context(message_text, parsed_context)
    
    # এআই রেসপন্স জেনারেট (সামারি ছাড়া, কারণ এটি টেস্ট)
    response_text = ask_speednet_ai(message_text, "", dynamic_context, bot_name, None, "Test User")
    
    return jsonify({"response": response_text})

@app.route("/disconnect", methods=["POST"])
def disconnect_page():
    """কানেক্ট করা পেজ ডাটাবেস থেকে মুছে ফেলে"""
    data = request.json
    page_id = data.get("page_id")
    
    if not page_id:
        return jsonify({"error": "Page ID required"}), 400
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM companies WHERE page_id = %s', (page_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "Page disconnected successfully."}), 200

@app.route("/manage/<page_id>")
def manage_page(page_id):
    """Specific dashboard page for a connected page"""
    return render_template("manage.html", app_id=FACEBOOK_APP_ID, page_id=page_id)

@app.route("/api/company/<page_id>")
def get_company_api(page_id):
    """Returns config for a specific page to populate the dashboard"""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute('SELECT business_info, bot_name, page_name FROM companies WHERE page_id = %s', (page_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return jsonify(row)
    return jsonify({}), 404

@app.route("/connected-pages")
def connected_pages():
    """Returns list of connected pages"""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute('SELECT page_id, page_name, bot_name FROM companies ORDER BY id DESC')
    pages = cursor.fetchall()
    conn.close()
    return jsonify(pages)

# --- Admin Route for SaaS (Optional) ---
@app.route("/register", methods=["POST"])
def register_company():
    """নতুন কোম্পানি রেজিস্ট্রেশন করার জন্য API"""
    data = request.json
    page_id = data.get("page_id")
    access_token = data.get("access_token")
    business_info = data.get("business_info")
    bot_name = data.get("bot_name", "AI Assistant")
    page_name = data.get("page_name", "Unknown Page")
    
    if not all([page_id, access_token, business_info]):
        return jsonify({"error": "Missing fields"}), 400
        
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO companies (page_id, access_token, business_info, bot_name, page_name) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (page_id) DO UPDATE SET access_token = EXCLUDED.access_token, business_info = EXCLUDED.business_info, bot_name = EXCLUDED.bot_name, page_name = EXCLUDED.page_name
        ''', (page_id, access_token, business_info, bot_name, page_name))
        conn.commit()

        # --- অটোমেটিক সাবস্ক্রিপশন লজিক ---
        # পেজটিকে অ্যাপের সাথে সাবস্ক্রাইব করা হচ্ছে যাতে মেসেজ ওয়েবহুকে আসে
        subscribe_url = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/{page_id}/subscribed_apps"
        subscribe_params = {
            "access_token": access_token,
            "subscribed_fields": "messages,messaging_postbacks"
        }
        sub_resp = requests.post(subscribe_url, params=subscribe_params)
        if sub_resp.status_code != 200:
            logging.error(f"Failed to subscribe page {page_id}: {sub_resp.text}")

        return jsonify({"status": "success", "message": f"Company {page_id} registered."}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# --- Database Initialization (Run on App Startup) ---
# Gunicorn বা প্রোডাকশন সার্ভারে __main__ ব্লক রান হয় না, তাই এখানে কল করতে হবে।
try:
    init_db()  # অ্যাপ চালু হওয়ার সময় ডাটাবেস ইনিশিয়ালাইজ করা
    seed_db()  # ডিফল্ট কোম্পানি সিড করা
    logging.info("Database initialized and seeded successfully.")
except Exception as e:
    logging.error(f"Startup DB Error: {e}")

if __name__ == "__main__":
    print("--- স্পিড নেট এআই সার্ভার (Local) চালু হচ্ছে ---")
    is_debug = os.getenv("FLASK_DEBUG", "False").lower() in ("true", "1", "t")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=is_debug)