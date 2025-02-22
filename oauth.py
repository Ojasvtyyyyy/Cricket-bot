from flask import Flask, request, redirect, session
import requests
from dotenv import load_dotenv
import os
import secrets

load_dotenv()

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# Configuration
CLIENT_ID = os.getenv('REDDIT_CLIENT_ID')
CLIENT_SECRET = os.getenv('REDDIT_CLIENT_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI')
SCOPE = 'read submit identity'

@app.route('/')
def homepage():
    return '''
    <h1>Reddit Bot OAuth Setup</h1>
    <a href="/authorize" style="
        display: inline-block;
        padding: 10px 20px;
        background-color: #FF4500;
        color: white;
        text-decoration: none;
        border-radius: 5px;
        font-family: Arial, sans-serif;">
        Authorize with Reddit
    </a>
    '''

@app.route('/authorize')
def authorize():
    # Generate a random state value for security
    state = secrets.token_hex(16)
    session['state'] = state
    
    auth_url = f'https://www.reddit.com/api/v1/authorize?client_id={CLIENT_ID}&response_type=code&state={state}&redirect_uri={REDIRECT_URI}&duration=permanent&scope={SCOPE}'
    return redirect(auth_url)

@app.route('/callback')
def callback():
    error = request.args.get('error')
    if error:
        return f'''
        <h1>Error</h1>
        <p style="color: red;">Error during authorization: {error}</p>
        '''
    
    # Verify state to prevent CSRF attacks
    state = request.args.get('state')
    if state != session.get('state'):
        return '''
        <h1>Error</h1>
        <p style="color: red;">State mismatch. Possible CSRF attack.</p>
        '''
    
    code = request.args.get('code')
    
    # Exchange code for tokens
    try:
        auth = requests.auth.HTTPBasicAuth(CLIENT_ID, CLIENT_SECRET)
        data = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': REDIRECT_URI
        }
        
        headers = {'User-Agent': 'CricketBot/1.0'}
        
        response = requests.post(
            'https://www.reddit.com/api/v1/access_token',
            auth=auth,
            data=data,
            headers=headers
        )
        
        tokens = response.json()
        
        if 'error' in tokens:
            return f'''
            <h1>Error</h1>
            <p style="color: red;">Error getting tokens: {tokens['error']}</p>
            '''
        
        # Display tokens with better formatting
        return f'''
        <h1>Authorization Successful!</h1>
        <div style="
            background-color: #f5f5f5;
            padding: 20px;
            border-radius: 5px;
            font-family: monospace;">
            <p><strong>Access Token:</strong><br> {tokens.get('access_token')}</p>
            <p><strong>Refresh Token:</strong><br> {tokens.get('refresh_token')}</p>
            <p><strong>Token Type:</strong> {tokens.get('token_type')}</p>
            <p><strong>Expires In:</strong> {tokens.get('expires_in')} seconds</p>
        </div>
        <p style="color: #666;">
            Save these tokens safely. You'll need the refresh token for your bot.
        </p>
        '''
        
    except Exception as e:
        return f'''
        <h1>Error</h1>
        <p style="color: red;">Error during token exchange: {str(e)}</p>
        '''

@app.route('/health')
def health_check():
    return 'OK', 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
