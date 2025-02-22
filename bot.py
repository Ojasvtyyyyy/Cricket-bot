import praw
import os
from flask import Flask, jsonify
from dotenv import load_dotenv
import requests
import time
from threading import Thread
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
app = Flask(__name__)

# Reddit setup
reddit = praw.Reddit(
    client_id=os.getenv('REDDIT_CLIENT_ID'),
    client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
    user_agent=os.getenv('REDDIT_USER_AGENT'),
    username=os.getenv('REDDIT_USERNAME'),
    password=os.getenv('REDDIT_PASSWORD')
)

def get_gemini_response(text):
    try:
        prompt = "You are the most funny person on r/cricket. Keep responses really short and quality humour. Be extremely relavent spontaneous savage, and use curses but don't overdo. If asked to choose between options, be extremely decisive and pick one. The message is: " + text
        
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={os.getenv('GEMINI_API_KEY')}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "safetySettings": [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
                ]
            }
        )
        return response.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return None

def process_post(post):
    try:
        text = f"{post.title}\n{post.selftext}"
        response = get_gemini_response(text)
        if response:
            post.reply(response)
            logger.info(f"Replied to post: {post.id}")
    except Exception as e:
        logger.error(f"Error processing post: {e}")

def process_comment(comment):
    try:
        response = get_gemini_response(comment.body)
        if response:
            comment.reply(response)
            logger.info(f"Replied to comment: {comment.id}")
    except Exception as e:
        logger.error(f"Error processing comment: {e}")

def run_bot():
    subreddit = reddit.subreddit('cricket2')
    
    def monitor_posts():
        for post in subreddit.stream.submissions(skip_existing=True):
            process_post(post)

    def monitor_comments():
        for comment in subreddit.stream.comments(skip_existing=True):
            if 'u/cricket-bot' in comment.body.lower():
                process_comment(comment)
            try:
                parent = comment.parent()
                if isinstance(parent, praw.models.Comment) and parent.author and parent.author.name == os.getenv('REDDIT_USERNAME'):
                    process_comment(comment)
            except Exception as e:
                logger.error(f"Error checking comment parent: {e}")

    post_thread = Thread(target=monitor_posts)
    comment_thread = Thread(target=monitor_comments)
    
    post_thread.daemon = True
    comment_thread.daemon = True
    
    post_thread.start()
    comment_thread.start()

@app.route('/health')
def health_check():
    try:
        reddit.user.me()
        return jsonify({'status': 'healthy'}), 200
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 503

if __name__ == '__main__':
    bot_thread = Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
