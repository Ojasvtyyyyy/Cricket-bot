import praw
import os
from dotenv import load_dotenv
import requests
import pymongo
from datetime import datetime
import time
import traceback

load_dotenv()

# Configuration
REDDIT_CLIENT_ID = os.getenv('REDDIT_CLIENT_ID')
REDDIT_CLIENT_SECRET = os.getenv('REDDIT_CLIENT_SECRET')
REDDIT_REFRESH_TOKEN = os.getenv('REDDIT_REFRESH_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
MONGO_URI = os.getenv('MONGO_URI')

# Initialize MongoDB
client = pymongo.MongoClient(MONGO_URI)
db = client['cricket_bot']
posts_collection = db['processed_posts']
comments_collection = db['processed_comments']

def init_reddit():
    try:
        return praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            refresh_token=REDDIT_REFRESH_TOKEN,
            user_agent="Cricket-Bot/1.0"
        )
    except Exception as e:
        print(f"Error initializing Reddit: {str(e)}")
        return None

def get_gemini_response(text):
    try:
        prompt = "You are the most funny person on r/cricket. Keep responses really short and quality humour. Be extremely relavent spontaneous savage, and use curses but don't overdo. If asked to choose between options, be extremely decisive and pick one. The message is: " + text
        
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}",
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
        print(f"Error getting Gemini response: {str(e)}")
        return None

def process_new_posts(reddit):
    try:
        subreddit = reddit.subreddit('cricket2')
        for post in subreddit.stream.submissions():
            if not posts_collection.find_one({'post_id': post.id}):
                response = get_gemini_response(f"{post.title}\n{post.selftext}")
                if response:
                    comment = post.reply(response)
                    posts_collection.insert_one({
                        'post_id': post.id,
                        'comment_id': comment.id,
                        'timestamp': datetime.utcnow()
                    })
    except Exception as e:
        print(f"Error processing posts: {str(e)}")

def process_comments(reddit):
    try:
        for comment in reddit.subreddit('cricket2').stream.comments():
            # Check for mentions
            if 'u/cricket-bot' in comment.body.lower():
                if not comments_collection.find_one({'comment_id': comment.id}):
                    response = get_gemini_response(comment.body)
                    if response:
                        reply = comment.reply(response)
                        comments_collection.insert_one({
                            'comment_id': comment.id,
                            'reply_id': reply.id,
                            'timestamp': datetime.utcnow()
                        })
            
            # Check for replies to bot's comments
            elif comment.parent_id:
                parent_comment = reddit.comment(comment.parent_id[3:])
                if comments_collection.find_one({'reply_id': parent_comment.id}):
                    if not comments_collection.find_one({'comment_id': comment.id}):
                        response = get_gemini_response(comment.body)
                        if response:
                            reply = comment.reply(response)
                            comments_collection.insert_one({
                                'comment_id': comment.id,
                                'reply_id': reply.id,
                                'timestamp': datetime.utcnow()
                            })
    except Exception as e:
        print(f"Error processing comments: {str(e)}")

def main():
    reddit = init_reddit()
    if not reddit:
        return
    
    while True:
        try:
            process_new_posts(reddit)
            process_comments(reddit)
        except Exception as e:
            print(f"Main loop error: {str(e)}")
            traceback.print_exc()
        time.sleep(5)

if __name__ == "__main__":
    main()
