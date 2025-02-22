import praw
import os
from dotenv import load_dotenv
import requests
import pymongo
from datetime import datetime, timedelta
import time
import traceback
import logging
from ratelimit import limits, sleep_and_retry
import sys
from requests.exceptions import RequestException
from prawcore.exceptions import PrawcoreException
from pymongo.errors import PyMongoError

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

# Configuration
REDDIT_CLIENT_ID = os.getenv('REDDIT_CLIENT_ID')
REDDIT_CLIENT_SECRET = os.getenv('REDDIT_CLIENT_SECRET')
REDDIT_REFRESH_TOKEN = os.getenv('REDDIT_REFRESH_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
MONGO_URI = os.getenv('MONGO_URI')

# Rate limiting configurations
CALLS_PER_MINUTE = 30  # Reddit API limit
GEMINI_CALLS_PER_MINUTE = 60  # Gemini API limit

class DatabaseManager:
    def __init__(self, uri):
        try:
            self.client = pymongo.MongoClient(uri)
            self.db = self.client['cricket_bot']
            self.posts = self.db['processed_posts']
            self.comments = self.db['processed_comments']
            self.error_log = self.db['error_log']
            
            # Create indexes
            self.posts.create_index('post_id', unique=True)
            self.comments.create_index('comment_id', unique=True)
            
            logger.info("Successfully connected to MongoDB")
        except PyMongoError as e:
            logger.error(f"MongoDB connection error: {e}")
            raise

    def log_error(self, error_type, error_message, details=None):
        try:
            self.error_log.insert_one({
                'timestamp': datetime.utcnow(),
                'type': error_type,
                'message': str(error_message),
                'details': details
            })
        except PyMongoError as e:
            logger.error(f"Failed to log error to MongoDB: {e}")

class RedditBot:
    def __init__(self):
        if not all([REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_REFRESH_TOKEN, 
                   GEMINI_API_KEY, MONGO_URI]):
            raise ValueError("Missing required environment variables")
        
        self.db = DatabaseManager(MONGO_URI)
        self.reddit = self.init_reddit()
        self.setup_rate_limits()

    def init_reddit(self):
        try:
            reddit = praw.Reddit(
                client_id=REDDIT_CLIENT_ID,
                client_secret=REDDIT_CLIENT_SECRET,
                refresh_token=REDDIT_REFRESH_TOKEN,
                user_agent="Cricket-Bot/1.0"
            )
            logger.info("Successfully initialized Reddit instance")
            return reddit
        except Exception as e:
            logger.error(f"Failed to initialize Reddit: {e}")
            self.db.log_error("Reddit Init", str(e))
            raise

    def setup_rate_limits(self):
        self.get_gemini_response = sleep_and_retry(
            limits(calls=GEMINI_CALLS_PER_MINUTE, period=60)(self.get_gemini_response)
        )

    def get_gemini_response(self, text):
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
                },
                timeout=10
            )
            
            if response.status_code != 200:
                logger.error(f"Gemini API error: {response.status_code} - {response.text}")
                return None
                
            return response.json()['candidates'][0]['content']['parts'][0]['text']
            
        except RequestException as e:
            logger.error(f"Gemini API request error: {e}")
            self.db.log_error("Gemini API", str(e))
            return None
        except Exception as e:
            logger.error(f"Unexpected error in Gemini response: {e}")
            self.db.log_error("Gemini Unexpected", str(e))
            return None

    @sleep_and_retry
    @limits(calls=CALLS_PER_MINUTE, period=60)
    def process_new_posts(self):
        try:
            subreddit = self.reddit.subreddit('cricket2')
            for post in subreddit.stream.submissions(skip_existing>True):
                if not self.db.posts.find_one({'post_id': post.id}):
                    response = self.get_gemini_response(f"{post.title}\n{post.selftext}")
                    if response:
                        try:
                            comment = post.reply(response)
                            self.db.posts.insert_one({
                                'post_id': post.id,
                                'comment_id': comment.id,
                                'timestamp': datetime.utcnow()
                            })
                            logger.info(f"Successfully processed post {post.id}")
                        except PrawcoreException as e:
                            logger.error(f"Reddit API error while replying to post: {e}")
                            self.db.log_error("Reddit Reply", str(e))
                            time.sleep(60)  # Back off on API errors
                            
        except PrawcoreException as e:
            logger.error(f"Reddit API error in post stream: {e}")
            self.db.log_error("Reddit Stream", str(e))
            time.sleep(60)
        except Exception as e:
            logger.error(f"Unexpected error in post processing: {e}")
            self.db.log_error("Post Processing", str(e))

    @sleep_and_retry
    @limits(calls=CALLS_PER_MINUTE, period=60)
    def process_comments(self):
        try:
            for comment in self.reddit.subreddit('cricket2').stream.comments(skip_existing=True):
                # Handle mentions
                if 'u/cricket-bot' in comment.body.lower():
                    self.handle_mention(comment)
                # Handle replies
                elif comment.parent_id:
                    self.handle_reply(comment)
                    
        except PrawcoreException as e:
            logger.error(f"Reddit API error in comment stream: {e}")
            self.db.log_error("Comment Stream", str(e))
            time.sleep(60)
        except Exception as e:
            logger.error(f"Unexpected error in comment processing: {e}")
            self.db.log_error("Comment Processing", str(e))

    def handle_mention(self, comment):
        try:
            if not self.db.comments.find_one({'comment_id': comment.id}):
                response = self.get_gemini_response(comment.body)
                if response:
                    reply = comment.reply(response)
                    self.db.comments.insert_one({
                        'comment_id': comment.id,
                        'reply_id': reply.id,
                        'timestamp': datetime.utcnow()
                    })
                    logger.info(f"Successfully processed mention {comment.id}")
        except Exception as e:
            logger.error(f"Error handling mention: {e}")
            self.db.log_error("Mention Handler", str(e))

    def handle_reply(self, comment):
        try:
            parent_comment = self.reddit.comment(comment.parent_id[3:])
            if self.db.comments.find_one({'reply_id': parent_comment.id}):
                if not self.db.comments.find_one({'comment_id': comment.id}):
                    response = self.get_gemini_response(comment.body)
                    if response:
                        reply = comment.reply(response)
                        self.db.comments.insert_one({
                            'comment_id': comment.id,
                            'reply_id': reply.id,
                            'timestamp': datetime.utcnow()
                        })
                        logger.info(f"Successfully processed reply {comment.id}")
        except Exception as e:
            logger.error(f"Error handling reply: {e}")
            self.db.log_error("Reply Handler", str(e))

def main():
    max_retries = 3
    retry_delay = 300  # 5 minutes
    
    while True:
        try:
            bot = RedditBot()
            logger.info("Bot initialized successfully")
            
            while True:
                try:
                    bot.process_new_posts()
                    bot.process_comments()
                except Exception as e:
                    logger.error(f"Error in main loop: {e}")
                    time.sleep(30)
                    
        except Exception as e:
            logger.error(f"Critical error: {e}")
            if max_retries > 0:
                max_retries -= 1
                logger.info(f"Retrying in {retry_delay} seconds... ({max_retries} retries left)")
                time.sleep(retry_delay)
            else:
                logger.critical("Max retries exceeded. Shutting down.")
                sys.exit(1)

if __name__ == "__main__":
    main()
