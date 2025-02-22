import praw
import pymongo
import os
from flask import Flask, jsonify
from dotenv import load_dotenv
import requests
import time
from threading import Thread
import logging
from prawcore.exceptions import PrawcoreException, ResponseException, RequestException
from pymongo.errors import PyMongoError
from datetime import datetime, timedelta
from urllib.parse import quote_plus

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

# MongoDB connection with proper retry logic
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
            
            # Explicitly set database name
            db_name = os.getenv('MONGODB_DB_NAME', 'cricket_bot')
            db = client[db_name]
            
            # Test the connection
            client.admin.command('ping')
            logger.info("Successfully connected to MongoDB")
            return client, db
        except Exception as e:
            retries += 1
            logger.error(f"MongoDB connection attempt {retries} failed: {e}")
            if retries == max_retries:
                raise
            time.sleep(5)

# Initialize MongoDB client and collections
try:
    mongo_client, db = connect_to_mongodb()
    posts_collection = db['processed_posts']
    comments_collection = db['processed_comments']
    rate_limits_collection = db['rate_limits']
    
    # Create indexes for better performance
    posts_collection.create_index('post_id', unique=True)
    comments_collection.create_index('comment_id', unique=True)
    rate_limits_collection.create_index('timestamp', expireAfterSeconds=3600)
    
except Exception as e:
    logger.error(f"Failed to connect to MongoDB after all retries: {e}")
    raise

# Initialize Reddit client
reddit = praw.Reddit(
    client_id=os.getenv('REDDIT_CLIENT_ID'),
    client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
    user_agent=os.getenv('REDDIT_USER_AGENT'),
    username=os.getenv('REDDIT_USERNAME'),
    password=os.getenv('REDDIT_PASSWORD')
)

class RateLimiter:
    def __init__(self, collection, limit_per_minute=30):
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
    
def process_post(post):
   try:
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

   except Exception as e:
       logger.error(f"Error in process_post: {e}")
       time.sleep(60)

def process_comment(comment):
   try:
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

   except Exception as e:
       logger.error(f"Error in process_comment: {e}")
       time.sleep(60)

def monitor_posts(subreddit):
   while True:
       try:
           for post in subreddit.stream.submissions(skip_existing=True):
               process_post(post)
       except Exception as e:
           logger.error(f"Error in post stream: {e}")
           time.sleep(60)

def monitor_comments(subreddit):
   while True:
       try:
           for comment in subreddit.stream.comments(skip_existing=True):
               if 'u/cricket-bot' in comment.body.lower():
                   process_comment(comment)
               
               try:
                   parent = comment.parent()
                   if isinstance(parent, praw.models.Comment) and parent.author and parent.author.name == os.getenv('REDDIT_USERNAME'):
                       process_comment(comment)
               except Exception as e:
                   logger.error(f"Error checking comment parent: {e}")
       except Exception as e:
           logger.error(f"Error in comment stream: {e}")
           time.sleep(60)

def start_bot():
   while True:
       try:
           subreddit = reddit.subreddit('cricket2')
           
           posts_thread = Thread(target=monitor_posts, args=(subreddit,))
           comments_thread = Thread(target=monitor_comments, args=(subreddit,))
           
           posts_thread.daemon = True
           comments_thread.daemon = True
           
           posts_thread.start()
           comments_thread.start()
           
           # Keep threads alive and monitor their health
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
       # Test MongoDB connection
       mongo_client.admin.command('ping')
       db_status = 'connected'
   except Exception as e:
       db_status = f'error: {str(e)}'
       logger.error(f"MongoDB health check failed: {e}")

   try:
       # Test Reddit connection
       reddit.user.me()
       reddit_status = 'connected'
   except Exception as e:
       reddit_status = f'error: {str(e)}'
       logger.error(f"Reddit health check failed: {e}")

   # Test bot threads
   threads_status = 'running' if Thread.active_count() > 1 else 'error: no bot threads'

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

if __name__ == '__main__':
   bot_thread = Thread(target=start_bot)
   bot_thread.daemon = True
   bot_thread.start()
   
   port = int(os.getenv('PORT', 5000))
   app.run(host='0.0.0.0', port=port)
