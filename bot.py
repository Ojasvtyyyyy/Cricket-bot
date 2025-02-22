import praw
import pymongo
import os
from flask import Flask
from dotenv import load_dotenv
import requests
import time
from threading import Thread
import logging
from prawcore.exceptions import PrawcoreException, ResponseException, RequestException
from pymongo.errors import PyMongoError
from datetime import datetime, timedelta

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()
app = Flask(__name__)

# MongoDB setup with error handling
try:
    client = pymongo.MongoClient(os.getenv('MONGODB_URI'), serverSelectionTimeoutMS=5000)
    client.server_info()  # Test connection
    db = client['cricket_bot']
    posts_collection = db['processed_posts']
    comments_collection = db['processed_comments']
    rate_limits_collection = db['rate_limits']
except PyMongoError as e:
    logger.error(f"MongoDB connection error: {e}")
    raise

# Reddit setup
reddit = praw.Reddit(
    client_id=os.getenv('REDDIT_CLIENT_ID'),
    client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
    user_agent=os.getenv('REDDIT_USER_AGENT'),
    username=os.getenv('REDDIT_USERNAME'),
    password=os.getenv('REDDIT_PASSWORD')
)

class RateLimiter:
    def __init__(self, collection, limit_per_minute=60):
        self.collection = collection
        self.limit_per_minute = limit_per_minute

    def can_proceed(self, action_type):
        try:
            now = datetime.utcnow()
            one_minute_ago = now - timedelta(minutes=1)
            
            # Clean old entries
            self.collection.delete_many({"timestamp": {"$lt": one_minute_ago}})
            
            # Count recent actions
            recent_count = self.collection.count_documents({
                "action_type": action_type,
                "timestamp": {"$gte": one_minute_ago}
            })
            
            if recent_count >= self.limit_per_minute:
                return False
            
            # Record new action
            self.collection.insert_one({
                "action_type": action_type,
                "timestamp": now
            })
            return True
        except PyMongoError as e:
            logger.error(f"Rate limiter error: {e}")
            return False

rate_limiter = RateLimiter(rate_limits_collection)

def get_gemini_response(text, max_retries=3):
    if not rate_limiter.can_proceed("gemini_api"):
        logger.warning("Rate limit reached for Gemini API")
        time.sleep(60)
        
    retries = 0
    while retries < max_retries:
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
                },
                timeout=10
            )
            response.raise_for_status()
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        
        except requests.exceptions.RequestException as e:
            logger.error(f"Gemini API request error: {e}")
            retries += 1
            time.sleep(2 ** retries)  # Exponential backoff
            
        except (KeyError, IndexError) as e:
            logger.error(f"Gemini API response parsing error: {e}")
            return None
    
    return None

def process_post(post):
    try:
        # Check if post was already processed
        if posts_collection.find_one({'post_id': post.id}):
            return

        if not rate_limiter.can_proceed("reddit_comment"):
            logger.warning("Rate limit reached for Reddit comments")
            time.sleep(60)
            return

        text = f"{post.title}\n{post.selftext}"
        response = get_gemini_response(text)
        
        if response:
            comment = post.reply(response)
            posts_collection.insert_one({
                'post_id': post.id,
                'comment_id': comment.id,
                'timestamp': datetime.utcnow()
            })
            logger.info(f"Successfully processed post: {post.id}")

    except (PrawcoreException, ResponseException, RequestException) as e:
        logger.error(f"Reddit API error while processing post: {e}")
        time.sleep(60)
    except Exception as e:
        logger.error(f"Unexpected error in post processing: {e}")

def process_comment(comment):
    try:
        # Check if comment was already processed
        if comments_collection.find_one({'comment_id': comment.id}):
            return

        if not rate_limiter.can_proceed("reddit_comment"):
            logger.warning("Rate limit reached for Reddit comments")
            time.sleep(60)
            return

        response = get_gemini_response(comment.body)
        
        if response:
            reply = comment.reply(response)
            comments_collection.insert_one({
                'comment_id': comment.id,
                'reply_id': reply.id,
                'timestamp': datetime.utcnow()
            })
            logger.info(f"Successfully processed comment: {comment.id}")

    except (PrawcoreException, ResponseException, RequestException) as e:
        logger.error(f"Reddit API error while processing comment: {e}")
        time.sleep(60)
    except Exception as e:
        logger.error(f"Unexpected error in comment processing: {e}")

def stream_with_error_handling(stream, process_func):
    while True:
        try:
            for item in stream:
                try:
                    process_func(item)
                except Exception as e:
                    logger.error(f"Error processing item: {e}")
                    continue
        except Exception as e:
            logger.error(f"Stream error: {e}")
            time.sleep(60)  # Wait before reconnecting

def monitor_subreddit():
    subreddit = reddit.subreddit('cricket2')
    
    # Create separate threads for posts and comments
    def monitor_posts():
        stream_with_error_handling(subreddit.stream.submissions(skip_existing=True), process_post)

    def monitor_comments():
        def comment_handler(comment):
            try:
                # Check for mentions
                if 'u/cricket-bot' in comment.body.lower():
                    process_comment(comment)
                
                # Check for replies to bot's comments
                parent = comment.parent()
                if isinstance(parent, praw.models.Comment) and parent.author == os.getenv('REDDIT_USERNAME'):
                    process_comment(comment)
            except Exception as e:
                logger.error(f"Error in comment handler: {e}")

        stream_with_error_handling(subreddit.stream.comments(skip_existing=True), comment_handler)

    # Start monitoring threads
    posts_thread = Thread(target=monitor_posts)
    comments_thread = Thread(target=monitor_comments)
    
    posts_thread.start()
    comments_thread.start()

@app.route('/health')
def health_check():
    return {'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()}

def run_bot():
    monitor_thread = Thread(target=monitor_subreddit)
    monitor_thread.daemon = True
    monitor_thread.start()
    
if __name__ == '__main__':
    run_bot()
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
