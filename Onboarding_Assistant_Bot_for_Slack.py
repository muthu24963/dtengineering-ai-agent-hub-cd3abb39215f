import json
import logging
import requests
import os
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv
import openai
import difflib
import threading

# Load environment variables
load_dotenv()
openai.api_type = os.getenv("OPENAI_API_TYPE", "azure")
openai.api_key = os.getenv("OPENAI_API_KEY")
openai.api_base = os.getenv("OPENAI_API_BASE")
openai.api_version = os.getenv("OPENAI_API_VERSION", "2024-02-15-preview")

# Initialize Flask App
app = Flask(__name__)

# Configure Logging
logging.basicConfig(level=logging.INFO)

# Slack API Token & Client
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# Asana API Token & Project ID
ASANA_ACCESS_TOKEN = os.getenv("ASANA_ACCESS_TOKEN")
ASANA_PROJECT_ID = os.getenv("ASANA_PROJECT_ID")

# Dictionary to store user progress
user_progress = {}
# Track paused users after reset
paused_users = set()

# Dynamic Google Drive file links
shared_files = {
    "channel_guide": "https://drive.google.com/file/d/15bnY7hjZEIgO6eeCWhVZ-KHfVoBDMlqY/view?usp=drive_link",
    "employee_handbook": "https://drive.google.com/file/d/1YbCHxrPey97dCwih7fxvN0s7HHZJfnQh/view?usp=drive_link",
    "hr_meeting_info": "https://drive.google.com/file/d/1fHDwz1uC3q9nNRgJ18RgP9264V9VJt_o/view?usp=drive_link",
    "tool_setup": "https://drive.google.com/file/d/13wLQmRkx0PjsgszRSMz6gsvI_wdqL4cL/view?usp=drive_link",
    "welcome_message": "https://drive.google.com/file/d/1iAXp_2XHnHt6skPnYLiKd23zdQ2kFPzt/view?usp=drive_link"
}

# Onboarding steps
onboarding_steps = {
    1: {
        "step": "🔹 Step 1: Join #general and #team-updates channels.",
        "resources": [f"Channel Guide: {shared_files['channel_guide']}"]
    },
    2: {
        "step": "🔹 Step 2: Read the employee handbook.",
        "resources": [f"Employee Handbook: {shared_files['employee_handbook']}"]
    },
    3: {
        "step": "🔹 Step 3: Schedule an HR meeting.",
        "resources": [f"HR Meeting Info: {shared_files['hr_meeting_info']}"]
    },
    4: {
        "step": "🔹 Step 4: Set up your email and tools.",
        "resources": [f"Tool Setup Guide: {shared_files['tool_setup']}"]
    },
    5: {
        "step": "✅ Onboarding complete! Welcome aboard! 🎉",
        "resources": [f"Welcome Message PDF: {shared_files['welcome_message']}"],
        "final": True
    }
}

# FAQ dictionary
faq_data = {
    "what is the wifi password": "The office WiFi password is `Welcome@123`.",
    "who do i contact for hr related queries": "You can contact hr@bct.com for any HR-related queries.",
    "when is the next onboarding meeting": "The next onboarding meeting is every Monday at 10 AM.",
    "how do i access the employee handbook": f"The employee handbook is available at: {shared_files['employee_handbook']}",
    "who is my manager?": "Your manager is Pushpa. Contact him at pushpa@bct.com.",
    "how do i request time off?": "You can request time off through the 'Time-Off Requests' form on the internal portal.",
    "where is the company policy?": f"You can find the company policy in the Employee Handbook: {shared_files['employee_handbook']}"
}

def get_openai_answer(user_question):
    prompt = f"""You are a helpful HR assistant for a company. Answer the employee question clearly and concisely.\n\nQuestion: {user_question}\nAnswer:"""
    response = openai.ChatCompletion.create(
        engine="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        max_tokens=100
    )
    return response["choices"][0]["message"]["content"].strip()

def get_mixed_answer(user_question):
    user_question_lower = user_question.lower().strip()
    best_match = difflib.get_close_matches(user_question_lower, faq_data.keys(), n=1, cutoff=0.8)
    if best_match:
        logging.info(f"Matched FAQ: {best_match[0]}")
        return faq_data[best_match[0]]
    logging.info("No FAQ match found. Calling OpenAI for response.")
    return get_openai_answer(user_question)

def create_asana_task(user_id, task_name, task_desc):
    url = "https://app.asana.com/api/1.0/tasks"
    headers = {
        "Authorization": f"Bearer {ASANA_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "data": {
            "name": task_name,
            "notes": task_desc,
            "projects": [ASANA_PROJECT_ID]
        }
    }
    try:
        response = requests.post(url, json=data, headers=headers)
        if response.status_code == 201:
            logging.info(f"Asana Task Created: {response.json()}")
            return response.json()
        else:
            logging.warning(f"Asana Task Failed: {response.json()}")
            return None
    except Exception as e:
        logging.error(f"Asana API Error: {str(e)}")
        return None

def send_onboarding_steps(channel_id, user_id, original_ts=None):
    if user_id in paused_users:
        slack_client.chat_postMessage(
            channel=channel_id,
            text="⚠️ Onboarding is paused. Please type *start onboarding* to continue."
        )
        return
    try:
        current_step = user_progress.get(user_id, 0) + 1
        if current_step > len(onboarding_steps):
            return

        step_data = onboarding_steps[current_step]
        message = step_data["step"]
        resources = "\n".join(step_data["resources"])
        user_progress[user_id] = current_step

        create_asana_task(user_id, f"Onboarding Step {current_step}", message)

        is_final = step_data.get("final", False)

        attachments = [
            {
                "text": f"Resources:\n{resources}\n\nClick 'Next' to continue." if not is_final else f"Resources:\n{resources}\n\nYou're all set! 🎉",
                "fallback": "You cannot interact with this message.",
                "callback_id": "onboarding_steps",
                "color": "#3AA3E3",
                "actions": [] if is_final else [{"name": "next", "text": "Next", "type": "button", "value": "next_step"}]
            }
        ]

        slack_client.chat_postMessage(channel=channel_id, text=message, attachments=attachments)

    except SlackApiError as e:
        logging.error(f"Slack API Error: {e.response['error']}")

@app.route("/slack/events", methods=["POST"])
def slack_events():
    data = request.json
    logging.info(f"Slack Event Received: {json.dumps(data, indent=2)}")

    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})

    event = data.get("event", {})
    user_id = event.get("user")
    channel_id = event.get("channel")
    text = event.get("text", "").lower()

    if event.get("subtype") in ["message_changed", "bot_message"]:
        return jsonify({"status": "ignored"})

    if event.get("type") == "message" and "bot_id" not in event:
        if "reset onboarding" in text:
            send_reset_button(channel_id)
            paused_users.add(user_id)
            return jsonify({"status": "reset_triggered"})

        if "start onboarding" in text:
            paused_users.discard(user_id)
            send_onboarding_steps(channel_id, user_id)
            return jsonify({"status": "onboarding_restarted"})

        answer = get_mixed_answer(text)
        slack_client.chat_postMessage(channel=channel_id, text=answer)

    return jsonify({"status": "ok"})

def send_reset_button(channel_id):
    slack_client.chat_postMessage(
        channel=channel_id,
        text="Are you sure you want to reset onboarding?",
        blocks=[
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Confirm Reset"},
                        "style": "danger",
                        "value": "confirm_reset"
                    }
                ]
            }
        ]
    )

@app.route("/slack/interactions", methods=["POST"])
def slack_interactions():
    payload = json.loads(request.form["payload"])
    logging.info(f"Slack Interaction Payload: {json.dumps(payload, indent=2)}")

    if "actions" not in payload:
        return jsonify({"status": "error", "message": "No actions found"})

    action = payload["actions"][0]
    channel_id = payload["channel"]["id"]
    user_id = payload["user"]["id"]

    if action["value"] == "next_step":
        completed_step = user_progress.get(user_id, 0)
        is_last = completed_step == len(onboarding_steps)
        

        response_text = "✅ Onboarding complete! 🎉" if is_last else f"Step {completed_step} completed ✅"

        threading.Thread(target=send_onboarding_steps, args=(channel_id, user_id)).start()
        return jsonify({"text": response_text})

    elif action["value"] == "confirm_reset":
        user_progress[user_id] = 0
        paused_users.add(user_id)
        slack_client.chat_postMessage(
            channel=channel_id,
            text="🚫 Onboarding progress reset. Type *start onboarding* to begin again."
        )
        return jsonify({"status": "reset_done"})

    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(port=5000, debug=True)