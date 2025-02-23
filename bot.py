import praw
import pymongo
import os
from flask import Flask, jsonify
from dotenv import load_dotenv
import requests
import time
from threading import Thread, active_count, Lock
import logging
from prawcore.exceptions import PrawcoreException, ResponseException, RequestException
from pymongo.errors import PyMongoError
from datetime import datetime, timedelta
import random

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

def connect_to_mongodb():
    max_retries = 3
    retries = 0
    while retries < max_retries:
        try:
            mongodb_uri = os.getenv('MONGODB_URI')
            if not mongodb_uri:
                raise Exception("MONGODB_URI environment variable not set")

            client = pymongo.MongoClient(
                mongodb_uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=20000
            )
            
            db_name = os.getenv('MONGODB_DB_NAME', 'cricket_bot')
            db = client[db_name]
            
            client.admin.command('ping')
            logger.info("Successfully connected to MongoDB")
            return client, db
        except Exception as e:
            retries += 1
            logger.error(f"MongoDB connection attempt {retries} failed: {e}")
            if retries == max_retries:
                raise
            time.sleep(5)

try:
    mongo_client, db = connect_to_mongodb()
    posts_collection = db['processed_posts']
    comments_collection = db['processed_comments']
    
    posts_collection.create_index('post_id', unique=True)
    comments_collection.create_index('comment_id', unique=True)
    comments_collection.create_index([('timestamp', pymongo.ASCENDING)])
    
except Exception as e:
    logger.error(f"Failed to connect to MongoDB after all retries: {e}")
    raise

reddit = praw.Reddit(
    client_id=os.getenv('REDDIT_CLIENT_ID'),
    client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
    user_agent=os.getenv('REDDIT_USER_AGENT'),
    refresh_token=os.getenv('REDDIT_REFRESH_TOKEN')
)

class RateLimiter:
    def __init__(self):
        self.last_action_time = {}
        self.lock = Lock()
        self.cooldown_periods = {
            'reddit_comment': 5,    # 5 seconds between comments
            'reddit_post': 3,       # 3 seconds between post checks
            'gemini_api': 2         # 2 seconds between API calls
        }
        self.recent_actions = []
        self.max_actions_per_minute = 25  # Reddit's general limit is 30/minute

    def can_proceed(self, action_type):
        with self.lock:
            current_time = time.time()
            
            # Clean up old actions
            self.recent_actions = [t for t in self.recent_actions if current_time - t < 60]
            
            # Check if we're approaching rate limit
            if len(self.recent_actions) >= self.max_actions_per_minute:
                return False
            
            # Check cooldown period
            last_time = self.last_action_time.get(action_type, 0)
            if current_time - last_time < self.cooldown_periods.get(action_type, 3):
                return False
            
            # Update tracking
            self.last_action_time[action_type] = current_time
            self.recent_actions.append(current_time)
            return True

rate_limiter = RateLimiter()

def get_gemini_response(text, max_retries=3):
    if not rate_limiter.can_proceed("gemini_api"):
        time.sleep(random.uniform(2, 4))
        return None
        
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
            time.sleep(2 ** retries)
            
        except (KeyError, IndexError) as e:
            logger.error(f"Gemini API response parsing error: {e}")
            return None
    
    return None 

def is_valid_post(post):
    try:
        return (
            not post.stickied and
            hasattr(post, 'author') and
            post.author is not None and
            not post.locked and
            not hasattr(post, 'removed_by_category')
        )
    except Exception as e:
        logger.error(f"Error checking post validity: {e}")
        return False

def extract_wait_time_from_error(error_msg):
    try:
        # Look for numbers in the error message
        numbers = [int(s) for s in error_msg.split() if s.isdigit()]
        if numbers:
            # Convert to seconds
            return numbers[0] * 60
    except:
        pass
    return 240  # Default 4 minutes if we can't extract

def process_post(post):
    max_retries = 3
    
    for retry in range(max_retries):
        try:
            if posts_collection.find_one({'post_id': post.id}):
                return

            post_age = datetime.utcnow() - datetime.fromtimestamp(post.created_utc)
            if post_age > timedelta(hours=24):
                return

            if not is_valid_post(post):
                return

            if not rate_limiter.can_proceed("reddit_post"):
                time.sleep(random.uniform(3, 5))
                continue

            text = f"{post.title}\n{post.selftext if post.selftext else ''}"
            response = get_gemini_response(text)
            
            if response:
                try:
                    comment = post.reply(response)
                    posts_collection.insert_one({
                        'post_id': post.id,
                        'comment_id': comment.id,
                        'timestamp': datetime.utcnow(),
                        'success': True
                    })
                    logger.info(f"Successfully commented on post: {post.id}")
                    return
                    
                except ResponseException as e:
                    if 'RATELIMIT' in str(e):
                        wait_time = extract_wait_time_from_error(str(e))
                        logger.warning(f"Rate limit hit, waiting {wait_time} seconds before retry {retry + 1}")
                        time.sleep(wait_time)
                        continue
                    else:
                        raise

        except Exception as e:
            logger.error(f"Error processing post {post.id}: {e}")
            if retry < max_retries - 1:
                time.sleep(random.uniform(5, 8))
            else:
                logger.error(f"Failed to process post {post.id} after {max_retries} retries")
                posts_collection.insert_one({
                    'post_id': post.id,
                    'timestamp': datetime.utcnow(),
                    'success': False,
                    'error': str(e)
                })

def process_comment(comment):
    max_retries = 3
    
    for retry in range(max_retries):
        try:
            if comment.author and comment.author.name == reddit.user.me().name:
                return

            if comments_collection.find_one({'comment_id': comment.id}):
                return

            if not rate_limiter.can_proceed("reddit_comment"):
                time.sleep(random.uniform(3, 5))
                continue

            text = comment.body
            if 'u/cricket_bot' in text.lower():
                text = text.lower().replace('u/cricket_bot', '').strip()
                if not text:
                    text = "Hey"

            response = get_gemini_response(text)
            if response:
                try:
                    reply = comment.reply(response)
                    comments_collection.insert_one({
                        'comment_id': comment.id,
                        'reply_id': reply.id,
                        'timestamp': datetime.utcnow(),
                        'success': True
                    })
                    logger.info(f"Successfully replied to comment: {comment.id}")
                    return
                    
                except ResponseException as e:
                    if 'RATELIMIT' in str(e):
                        wait_time = extract_wait_time_from_error(str(e))
                        logger.warning(f"Rate limit hit, waiting {wait_time} seconds before retry {retry + 1}")
                        time.sleep(wait_time)
                        continue
                    else:
                        raise

        except Exception as e:
            logger.error(f"Error in process_comment: {e}")
            if retry < max_retries - 1:
                time.sleep(random.uniform(5, 8))
            else:
                logger.error(f"Failed to process comment {comment.id} after {max_retries} retries")

def monitor_posts(subreddit):
    while True:
        try:
            logger.info(f"Starting to monitor posts in r/{subreddit.display_name}")
            for post in subreddit.stream.submissions(skip_existing=True):
                process_post(post)
        except Exception as e:
            logger.error(f"Error in post stream: {e}")
            time.sleep(random.uniform(5, 8)) 

def monitor_comments(subreddit):
    while True:
        try:
            for comment in subreddit.stream.comments(skip_existing=True):
                if ('u/cricket_bot' in comment.body.lower() or 
                    (comment.parent() and isinstance(comment.parent(), praw.models.Comment) and 
                     comment.parent().author and comment.parent().author.name == reddit.user.me().name)):
                    process_comment(comment)
        except Exception as e:
            logger.error(f"Error in comment stream: {e}")
            time.sleep(random.uniform(5, 8))

def cleanup_old_records():
    while True:
        try:
            # Keep records for 7 days
            cutoff_date = datetime.utcnow() - timedelta(days=7)
            posts_collection.delete_many({'timestamp': {'$lt': cutoff_date}})
            comments_collection.delete_many({'timestamp': {'$lt': cutoff_date}})
            time.sleep(86400)  # Run once per day
        except Exception as e:
            logger.error(f"Error in cleanup: {e}")
            time.sleep(3600)  # Wait an hour on error

def start_bot():
    while True:
        try:
            subreddit_name = os.getenv('SUBREDDIT_NAME', 'cricket')
            subreddit = reddit.subreddit(subreddit_name)
            
            posts_thread = Thread(target=monitor_posts, args=(subreddit,))
            comments_thread = Thread(target=monitor_comments, args=(subreddit,))
            cleanup_thread = Thread(target=cleanup_old_records)
            
            posts_thread.daemon = True
            comments_thread.daemon = True
            cleanup_thread.daemon = True
            
            posts_thread.start()
            comments_thread.start()
            cleanup_thread.start()
            
            while True:
                if not posts_thread.is_alive():
                    logger.error("Posts thread died, restarting...")
                    posts_thread = Thread(target=monitor_posts, args=(subreddit,))
                    posts_thread.daemon = True
                    posts_thread.start()
                
                if not comments_thread.is_alive():
                    logger.error("Comments thread died, restarting...")
                    comments_thread = Thread(target=monitor_comments, args=(subreddit,))
                    comments_thread.daemon = True
                    comments_thread.start()
                
                if not cleanup_thread.is_alive():
                    logger.error("Cleanup thread died, restarting...")
                    cleanup_thread = Thread(target=cleanup_old_records)
                    cleanup_thread.daemon = True
                    cleanup_thread.start()
                
                time.sleep(60)
            
        except Exception as e:
            logger.error(f"Error in main bot loop: {e}")
            time.sleep(60)

@app.route('/')
def home():
    return jsonify({
        'status': 'running',
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/health')
def health_check():
    try:
        mongo_client.admin.command('ping')
        db_status = 'connected'
    except Exception as e:
        db_status = f'error: {str(e)}'
        logger.error(f"MongoDB health check failed: {e}")

    try:
        reddit.user.me()
        reddit_status = 'connected'
    except Exception as e:
        reddit_status = f'error: {str(e)}'
        logger.error(f"Reddit health check failed: {e}")

    threads_status = 'running' if active_count() > 1 else 'error: no bot threads'

    status = all(x == 'connected' for x in [db_status, reddit_status]) and threads_status == 'running'

    response = {
        'status': 'healthy' if status else 'unhealthy',
        'timestamp': datetime.utcnow().isoformat(),
        'details': {
            'mongodb': db_status,
            'reddit': reddit_status,
            'bot_threads': threads_status
        }
    }

    return jsonify(response), 200 if status else 503

@app.route('/stats')
def stats():
    try:
        total_posts = posts_collection.count_documents({})
        successful_posts = posts_collection.count_documents({'success': True})
        total_comments = comments_collection.count_documents({})
        
        last_24h = datetime.utcnow() - timedelta(hours=24)
        posts_24h = posts_collection.count_documents({'timestamp': {'$gte': last_24h}})
        comments_24h = comments_collection.count_documents({'timestamp': {'$gte': last_24h}})
        
        return jsonify({
            'total_posts_processed': total_posts,
            'successful_posts': successful_posts,
            'total_comments_processed': total_comments,
            'posts_last_24h': posts_24h,
            'comments_last_24h': comments_24h,
            'timestamp': datetime.utcnow().isoformat()
        })
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    bot_thread = Thread(target=start_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
