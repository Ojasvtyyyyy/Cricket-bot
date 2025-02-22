from flask import Flask, request, redirect
import requests
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__)

CLIENT_ID = os.getenv('REDDIT_CLIENT_ID')
CLIENT_SECRET = os.getenv('REDDIT_CLIENT_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI')
SCOPE = 'read submit'

@app.route('/')
def homepage():
    return f'<a href="/authorize">Authorize with Reddit</a>'

@app.route('/authorize')
def authorize():
    auth_url = f'https://www.reddit.com/api/v1/authorize?client_id={CLIENT_ID}&response_type=code&state=STATE&redirect_uri={REDIRECT_URI}&duration=permanent&scope={SCOPE}'
    return redirect(auth_url)

@app.route('/callback')
def callback():
    error = request.args.get('error')
    if error:
        return f"Error: {error}"
    
    code = request.args.get('code')
    
    auth = requests.auth.HTTPBasicAuth(CLIENT_ID, CLIENT_SECRET)
    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT_URI
    }
    
    response = requests.post(
        'https://www.reddit.com/api/v1/access_token',
        auth=auth,
        data=data
    )
    
    tokens = response.json()
    return f'Access Token: {tokens.get("access_token")}<br>Refresh Token: {tokens.get("refresh_token")}'

if __name__ == '__main__':
    app.run(port=3000)
