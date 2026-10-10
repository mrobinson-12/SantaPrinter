"""Twilio SMS transport. Secrets stay in the environment, never in saved settings."""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def credentials():
    account = os.getenv('TWILIO_ACCOUNT_SID', '').strip()
    key = os.getenv('TWILIO_API_KEY_SID', '').strip()
    secret = os.getenv('TWILIO_API_KEY_SECRET', '').strip()
    token = os.getenv('TWILIO_AUTH_TOKEN', '').strip()
    if not re.fullmatch(r'AC[0-9a-fA-F]{32}', account):
        return None
    if key or secret:
        if not re.fullmatch(r'SK[0-9a-fA-F]{32}', key) or not secret:
            return None
        return account, key, secret
    return (account, account, token) if token else None


def configured():
    return credentials() is not None


def send_sms(sender, recipient, message):
    auth = credentials()
    if not auth:
        return {'state': 'failed', 'detail': 'Twilio credentials are missing or invalid.', 'sid': None}
    account, username, password = auth
    data = urllib.parse.urlencode({'From': sender, 'To': recipient, 'Body': message}).encode()
    request = urllib.request.Request('https://api.twilio.com/2010-04-01/Accounts/' + account + '/Messages.json',
        data=data, method='POST', headers={'Content-Type': 'application/x-www-form-urlencoded',
        'Authorization': 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
            result = json.loads(response.read(16384))
            sid = result.get('sid') if isinstance(result, dict) else None
            if response.status not in (200, 201, 202) or not isinstance(sid, str) or not re.fullmatch(r'SM[0-9a-fA-F]{32}', sid):
                return {'state': 'uncertain', 'detail': 'Twilio response did not confirm message acceptance. Check Twilio logs before retrying.', 'sid': None}
            return {'state': 'accepted', 'detail': 'Accepted by Twilio; handset delivery is not confirmed.', 'sid': sid}
    except urllib.error.HTTPError as error:
        code = None
        try:
            payload = json.loads(error.read(16384))
            value = payload.get('code') if isinstance(payload, dict) else None
            if isinstance(value, int):
                code = value
        except (ValueError, OSError):
            pass
        detail = 'Twilio HTTP {}'.format(error.code) + (' (error {})'.format(code) if code else '')
        state = 'retry' if error.code == 429 else 'uncertain' if error.code >= 500 else 'failed'
        return {'state': state, 'detail': detail, 'sid': None}
    except (OSError, urllib.error.URLError, ValueError):
        return {'state': 'uncertain', 'detail': 'Twilio connection or response failed. Check Twilio logs before retrying; the SMS may have been accepted.', 'sid': None}
