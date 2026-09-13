"""
integrations/spotify.py — Spotify PKCE Integration
==================================================
Provides Spotify playback controls via OAuth 2.0 PKCE desktop flow,
securely storing tokens in Windows Credential Manager.
"""

import base64
import hashlib
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional

import keyring

log = logging.getLogger("harley.integrations.spotify")

# To be configured with the actual developer app ID later
CLIENT_ID = "YOUR_SPOTIFY_CLIENT_ID"
SERVICE_NAME = "Harley_Spotify_Token"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """
    Temporary HTTP handler to capture the Spotify OAuth callback containing the code.
    """
    def log_message(self, format, *args):
        # Suppress default HTTP server logging to keep Harley's logs clean
        pass

    def do_GET(self):
        query_components = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in query_components:
            self.server.auth_code = query_components["code"][0]  # type: ignore
            
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><head><title>Success</title></head>"
                b"<body style='font-family:sans-serif;text-align:center;padding:50px;'>"
                b"<h1>Authorization successful!</h1>"
                b"<p>You can close this window and return to Harley.</p>"
                b"</body></html>"
            )
        else:
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><head><title>Failed</title></head>"
                b"<body style='font-family:sans-serif;text-align:center;padding:50px;'>"
                b"<h1>Authorization failed.</h1>"
                b"<p>No code returned from Spotify.</p>"
                b"</body></html>"
            )


class SpotifyAdapter:
    """
    Spotify adapter implementing PKCE flow and Playback controls.
    """
    def __init__(self, client_id: str = CLIENT_ID):
        self.client_id = client_id
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self._load_token()

    def _generate_pkce_pair(self) -> tuple[str, str]:
        """Generates a PKCE code verifier and code challenge."""
        verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode('ascii')
        challenge_bytes = hashlib.sha256(verifier.encode('ascii')).digest()
        challenge = base64.urlsafe_b64encode(challenge_bytes).rstrip(b'=').decode('ascii')
        return verifier, challenge

    def authenticate(self) -> None:
        """
        Runs the OAuth 2.0 PKCE desktop flow.
        Spins up a temporary local server to catch the callback.
        """
        verifier, challenge = self._generate_pkce_pair()
        
        # Start local server on ephemeral port (port=0)
        server = HTTPServer(('127.0.0.1', 0), OAuthCallbackHandler)
        port = server.server_port
        redirect_uri = f"http://127.0.0.1:{port}"
        
        # Build auth URL
        auth_params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "scope": "user-modify-playback-state user-read-playback-state"
        }
        auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(auth_params)
        
        log.info("Launching browser for Spotify authentication on port %d...", port)
        webbrowser.open(auth_url)
        
        # Block and wait for a single callback request
        server.auth_code = None  # type: ignore
        while server.auth_code is None:  # type: ignore
            server.handle_request()
            
        auth_code = server.auth_code  # type: ignore
        server.server_close()
        log.info("Captured authorization code. Exchanging for token...")
        
        # Exchange code for token
        token_data = urllib.parse.urlencode({
            "client_id": self.client_id,
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier
        }).encode("utf-8")
        
        req = urllib.request.Request("https://accounts.spotify.com/api/token", data=token_data)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        
        try:
            with urllib.request.urlopen(req) as response:
                resp_json = json.loads(response.read().decode())
                self._save_token(resp_json)
        except urllib.error.URLError as e:
            log.error("Failed to exchange token: %s", e)
            raise

    def _refresh_access_token(self) -> None:
        """Exchanges the refresh token for a new access token."""
        if not self.refresh_token:
            log.info("No refresh token available. Triggering full re-authentication.")
            self.authenticate()
            return
            
        log.info("Refreshing Spotify access token...")
        token_data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id
        }).encode("utf-8")
        
        req = urllib.request.Request("https://accounts.spotify.com/api/token", data=token_data)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        
        try:
            with urllib.request.urlopen(req) as response:
                resp_json = json.loads(response.read().decode())
                # The response might not include a new refresh token, keep the old one if so
                if "refresh_token" not in resp_json:
                    resp_json["refresh_token"] = self.refresh_token
                self._save_token(resp_json)
        except urllib.error.HTTPError as e:
            log.error("Failed to refresh token (HTTP %d). Re-authenticating.", e.code)
            self.authenticate()

    def _save_token(self, token_response: Dict[str, Any]) -> None:
        """Saves token response securely in Windows Credential Manager."""
        # We store as a JSON string because we need both access and refresh tokens
        keyring.set_password(SERVICE_NAME, "token_data", json.dumps(token_response))
        self.access_token = token_response.get("access_token")
        self.refresh_token = token_response.get("refresh_token")
        log.info("Spotify tokens saved securely to Windows Credential Manager.")

    def _load_token(self) -> None:
        """Loads token from Windows Credential Manager."""
        token_str = keyring.get_password(SERVICE_NAME, "token_data")
        if token_str:
            try:
                token_data = json.loads(token_str)
                self.access_token = token_data.get("access_token")
                self.refresh_token = token_data.get("refresh_token")
                log.debug("Loaded Spotify tokens from keyring.")
            except json.JSONDecodeError:
                log.warning("Corrupted token data in keyring.")
                
    def _api_request(self, method: str, endpoint: str, data: Optional[Dict] = None, retry: bool = True) -> Optional[Dict]:
        """Makes an authenticated request to the Spotify API."""
        if not self.access_token:
            log.info("No access token found.")
            self.authenticate()
            
        url = f"https://api.spotify.com/v1/{endpoint}"
        req_data = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=req_data, method=method)
        req.add_header("Authorization", f"Bearer {self.access_token}")
        req.add_header("Content-Type", "application/json")
        
        try:
            with urllib.request.urlopen(req) as response:
                if response.status == 204:  # No Content
                    return None
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry:
                log.warning("Spotify token expired. Attempting refresh...")
                self._refresh_access_token()
                # Retry once
                return self._api_request(method, endpoint, data, retry=False)
            else:
                log.error("Spotify API Error (HTTP %d): %s", e.code, e.reason)
                raise

    def play(self, query: Optional[str] = None) -> None:
        """Resumes playback, or searches and plays a specific track context."""
        if query:
            # Simple fallback search to find the track URI first
            search_query = urllib.parse.urlencode({"q": query, "type": "track", "limit": 1})
            results = self._api_request("GET", f"search?{search_query}")
            if results and results.get("tracks", {}).get("items"):
                uri = results["tracks"]["items"][0]["uri"]
                self._api_request("PUT", "me/player/play", {"uris": [uri]})
                log.info("Playing specific Spotify track: %s", uri)
                return
            log.warning("Could not find Spotify results for query: %s", query)
            
        # If no query or search failed, just resume current playback
        self._api_request("PUT", "me/player/play")
        log.info("Resumed active Spotify playback.")
        
    def pause(self) -> None:
        """Pauses Spotify playback."""
        self._api_request("PUT", "me/player/pause")
        log.info("Paused Spotify playback.")
        
    def skip(self) -> None:
        """Skips to the next track in the queue."""
        self._api_request("POST", "me/player/next")
        log.info("Skipped to next Spotify track.")
        
    def get_playback_state(self) -> Dict[str, Any]:
        """Gets current playback state (currently playing track, progress, etc.)."""
        state = self._api_request("GET", "me/player")
        return state if state else {}
