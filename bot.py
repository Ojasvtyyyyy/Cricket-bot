import praw
import pymongo
import os
from flask import Flask, jsonify
from dotenv import load_dotenv
import requests
import time
from threading import Thread, active_count, Lock, Event
import logging
from prawcore.exceptions import PrawcoreException, ResponseException, RequestException
from pymongo.errors import PyMongoError
from datetime import datetime, timedelta
import random
import backoff

# Enhanced logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()
app = Flask(__name__)

# Global shutdown event
shutdown_event = Event()

class DatabaseManager:
    def __init__(self):
        self.client = None
        self.db = None
        self.posts_collection = None
        self.comments_collection = None
        self.connect()

    @backoff.on_exception(backoff.expo, Exception, max_tries=5)
    def connect(self):
        try:
            mongodb_uri = os.getenv('MONGODB_URI')
            if not mongodb_uri:
                raise Exception("MONGODB_URI environment variable not set")

            self.client = pymongo.MongoClient(
                mongodb_uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=20000
            )
            
            db_name = os.getenv('MONGODB_DB_NAME', 'cricket_bot')
            self.db = self.client[db_name]
            
            self.posts_collection = self.db['processed_posts']
            self.comments_collection = self.db['processed_comments']
            
            # Create indexes
            self.posts_collection.create_index('post_id', unique=True)
            self.comments_collection.create_index('comment_id', unique=True)
            self.comments_collection.create_index([('timestamp', pymongo.ASCENDING)])
            
            self.client.admin.command('ping')
            logger.info("Successfully connected to MongoDB")
            
        except Exception as e:
            logger.error(f"MongoDB connection failed: {e}")
            raise

    def reconnect_if_needed(self):
        try:
            self.client.admin.command('ping')
        except:
            logger.warning("MongoDB connection lost, reconnecting...")
            self.connect()

class RedditManager:
    def __init__(self):
        self.reddit = None
        self.connect()

    @backoff.on_exception(backoff.expo, Exception, max_tries=5)
    def connect(self):
        self.reddit = praw.Reddit(
            client_id=os.getenv('REDDIT_CLIENT_ID'),
            client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
            user_agent=os.getenv('REDDIT_USER_AGENT'),
            refresh_token=os.getenv('REDDIT_REFRESH_TOKEN')
        )
        # Verify connection
        self.reddit.user.me()
        logger.info("Successfully connected to Reddit")

    def reconnect_if_needed(self):
        try:
            self.reddit.user.me()
        except:
            logger.warning("Reddit connection lost, reconnecting...")
            self.connect()

class ContentProcessor:
    def __init__(self, db_manager, reddit_manager):
        self.db_manager = db_manager
        self.reddit_manager = reddit_manager

    @backoff.on_exception(backoff.expo, 
                         (requests.exceptions.RequestException, 
                          ResponseException, 
                          PrawcoreException), 
                         max_tries=3)
    def get_gemini_response(self, text):
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

    def is_valid_post(self, post):
        return (
            not post.stickied and
            hasattr(post, 'author') and
            post.author is not None and
            not post.locked and
            not hasattr(post, 'removed_by_category')
        )

    @backoff.on_exception(backoff.expo, Exception, max_tries=3)
    def process_post(self, post):
        try:
            self.db_manager.reconnect_if_needed()
            self.reddit_manager.reconnect_if_needed()

            if self.db_manager.posts_collection.find_one({'post_id': post.id}):
                return

            post_age = datetime.utcnow() - datetime.fromtimestamp(post.created_utc)
            if post_age > timedelta(hours=24):
                return

            if not self.is_valid_post(post):
                return

            text = f"{post.title}\n{post.selftext if post.selftext else ''}"
            response = self.get_gemini_response(text)
            
            if response:
                comment = post.reply(response)
                self.db_manager.posts_collection.insert_one({
                    'post_id': post.id,
                    'comment_id': comment.id,
                    'timestamp': datetime.utcnow(),
                    'success': True
                })
                logger.info(f"Successfully commented on post: {post.id}")

        except Exception as e:
            logger.error(f"Error processing post {post.id}: {e}")
            self.db_manager.posts_collection.insert_one({
                'post_id': post.id,
                'timestamp': datetime.utcnow(),
                'success': False,
                'error': str(e)
            })

    @backoff.on_exception(backoff.expo, Exception, max_tries=3)
    def process_comment(self, comment):
        try:
            self.db_manager.reconnect_if_needed()
            self.reddit_manager.reconnect_if_needed()

            if comment.author and comment.author.name == self.reddit_manager.reddit.user.me().name:
                return

            if self.db_manager.comments_collection.find_one({'comment_id': comment.id}):
                return

            text = comment.body
            if 'u/cricket_bot' in text.lower():
                text = text.lower().replace('u/cricket_bot', '').strip()
                if not text:
                    text = "Hey"

            response = self.get_gemini_response(text)
            if response:
                reply = comment.reply(response)
                self.db_manager.comments_collection.insert_one({
                    'comment_id': comment.id,
                    'reply_id': reply.id,
                    'timestamp': datetime.utcnow(),
                    'success': True
                })
                logger.info(f"Successfully replied to comment: {comment.id}")

        except Exception as e:
            logger.error(f"Error processing comment {comment.id}: {e}")

class Bot:
    def __init__(self):
        self.db_manager = DatabaseManager()
        self.reddit_manager = RedditManager()
        self.processor = ContentProcessor(self.db_manager, self.reddit_manager)
        
    def monitor_posts(self, subreddit):
        while not shutdown_event.is_set():
            try:
                logger.info(f"Starting to monitor posts in r/{subreddit.display_name}")
                for post in subreddit.stream.submissions(skip_existing=True):
                    if shutdown_event.is_set():
                        break
                    self.processor.process_post(post)
            except Exception as e:
                logger.error(f"Error in post stream: {e}")
                time.sleep(random.uniform(5, 8))

    def monitor_comments(self, subreddit):
        while not shutdown_event.is_set():
            try:
                for comment in subreddit.stream.comments(skip_existing=True):
                    if shutdown_event.is_set():
                        break
                    if ('u/cricket_bot' in comment.body.lower() or 
                        (comment.parent() and isinstance(comment.parent(), praw.models.Comment) and 
                         comment.parent().author and comment.parent().author.name == self.reddit_manager.reddit.user.me().name)):
                        self.processor.process_comment(comment)
            except Exception as e:
                logger.error(f"Error in comment stream: {e}")
                time.sleep(random.uniform(5, 8))

    def cleanup_old_records(self):
        while not shutdown_event.is_set():
            try:
                cutoff_date = datetime.utcnow() - timedelta(days=7)
                self.db_manager.posts_collection.delete_many({'timestamp': {'$lt': cutoff_date}})
                self.db_manager.comments_collection.delete_many({'timestamp': {'$lt': cutoff_date}})
                time.sleep(86400)
            except Exception as e:
                logger.error(f"Error in cleanup: {e}")
                time.sleep(3600)

    def start(self):
        try:
            subreddit_name = os.getenv('SUBREDDIT_NAME', 'cricket')
            subreddit = self.reddit_manager.reddit.subreddit(subreddit_name)
            
            threads = [
                Thread(target=self.monitor_posts, args=(subreddit,), name="PostMonitor"),
                Thread(target=self.monitor_comments, args=(subreddit,), name="CommentMonitor"),
                Thread(target=self.cleanup_old_records, name="Cleanup")
            ]
            
            for thread in threads:
                thread.daemon = True
                thread.start()
            
            # Monitor threads
            while not shutdown_event.is_set():
                for thread in threads:
                    if not thread.is_alive():
                        logger.error(f"{thread.name} died, restarting...")
                        new_thread = Thread(target=getattr(self, thread._target.__name__), 
                                         args=thread._args,
                                         name=thread.name)
                        new_thread.daemon = True
                        new_thread.start()
                        threads[threads.index(thread)] = new_thread
                time.sleep(60)
                
        except Exception as e:
            logger.error(f"Error in main bot loop: {e}")
            raise

def create_app():
    bot = Bot()
    
    @app.route('/')
    def home():
        return jsonify({
            'status': 'running',
            'timestamp': datetime.utcnow().isoformat()
        })

    @app.route('/health')
    def health_check():
        try:
            bot.db_manager.client.admin.command('ping')
            db_status = 'connected'
        except Exception as e:
            db_status = f'error: {str(e)}'

        try:
            bot.reddit_manager.reddit.user.me()
            reddit_status = 'connected'
        except Exception as e:
            reddit_status = f'error: {str(e)}'

        threads_status = 'running' if active_count() > 1 else 'error: no bot threads'

        status = all(x == 'connected' for x in [db_status, reddit_status]) and threads_status == 'running'

        return jsonify({
            'status': 'healthy' if status else 'unhealthy',
            'timestamp': datetime.utcnow().isoformat(),
            'details': {
                'mongodb': db_status,
                'reddit': reddit_status,
                'bot_threads': threads_status
            }
        }), 200 if status else 503

    @app.route('/stats')
    def stats():
        try:
            total_posts = bot.db_manager.posts_collection.count_documents({})
            successful_posts = bot.db_manager.posts_collection.count_documents({'success': True})
            total_comments = bot.db_manager.comments_collection.count_documents({})
            
            last_24h = datetime.utcnow() - timedelta(hours=24)
            posts_24h = bot.db_manager.posts_collection.count_documents({'timestamp': {'$gte': last_24h}})
            comments_24h = bot.db_manager.comments_collection.count_documents({'timestamp': {'$gte': last_24h}})
            
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

    return app, bot

if __name__ == '__main__':
    app, bot = create_app()
    
    bot_thread = Thread(target=bot.start)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
