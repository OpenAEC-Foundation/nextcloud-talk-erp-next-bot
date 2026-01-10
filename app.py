#!/usr/bin/env python3
"""
Nextcloud Talk Bot for Claude Code
Supports multiple users: Maarten, Albert, Freek
Each user has their own bot with separate secret and ERPNext credentials
Maintains conversation history per chat
Includes WhisperFlow audio transcription
"""

import os
import hmac
import hashlib
import json
import subprocess
import requests
import threading
import tempfile
import re
import urllib.parse
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify

# Task Bot Database
TASK_DB_PATH = '/opt/deck-bot-poc/data/deck_bot.db'

def get_task_bot_by_token(conversation_token):
    """Get task bot info from deck-bot-poc database"""
    try:
        if not os.path.exists(TASK_DB_PATH):
            return None
        conn = sqlite3.connect(TASK_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM task_bots WHERE conversation_token = ?', (conversation_token,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"Error getting task bot: {e}")
        return None


def detect_completion_intent(message):
    """
    Detect if user wants to complete/close the task via natural language
    Returns: 'confirm' if needs confirmation, 'complete' if explicit, None if no intent
    """
    message_lower = message.lower().strip()

    # Explicit completion phrases - complete immediately
    explicit_phrases = [
        'taak afronden', 'taak afsluiten', 'taak voltooien',
        'sluit de taak', 'rond de taak af', 'voltooi de taak',
        'markeer als klaar', 'markeer als voltooid', 'markeer als afgerond',
        'zet op klaar', 'zet op done', 'naar klaar verplaatsen',
        'taak is klaar', 'taak is af', 'taak voltooid',
        'dit is klaar', 'alles is klaar', 'alles afgerond',
        'we zijn klaar', 'ik ben klaar', 'klaar met de taak'
    ]

    for phrase in explicit_phrases:
        if phrase in message_lower:
            return 'complete'

    # Confirmation phrases - ask for confirmation first
    confirm_phrases = [
        'kunnen we afronden', 'kunnen we afsluiten',
        'mag de taak dicht', 'taak dicht', 'afronden?',
        'is de taak klaar', 'ben je klaar', 'zijn we klaar',
        'kan dit dicht', 'sluiten we af'
    ]

    for phrase in confirm_phrases:
        if phrase in message_lower:
            return 'confirm'

    return None


def complete_task(token, bot_config, task_bot):
    """Complete a task - mark in DB and move card to Klaar"""
    try:
        conn = sqlite3.connect(TASK_DB_PATH)
        cursor = conn.cursor()
        cursor.execute('UPDATE task_bots SET status = ?, completed_at = datetime("now") WHERE conversation_token = ?',
                      ('completed', token))
        conn.commit()
        conn.close()

        # Move card to "Klaar" stack in Deck
        move_card_to_done(task_bot['board_id'], task_bot['stack_id'], task_bot['card_id'])

        return True
    except Exception as e:
        print(f"Error completing task: {e}")
        return False


def close_conversation(token, nc_user, nc_password):
    """
    Close/delete a Talk conversation after task completion.
    Uses Nextcloud Talk API to delete the room.
    """
    try:
        headers = {
            'OCS-APIRequest': 'true',
            'Accept': 'application/json'
        }
        auth = (nc_user, nc_password)

        # Delete the conversation (this removes it for everyone)
        url = f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v4/room/{token}"
        resp = requests.delete(url, auth=auth, headers=headers, timeout=30)

        print(f"Close conversation response: {resp.status_code}")
        return resp.status_code in [200, 204]
    except Exception as e:
        print(f"Error closing conversation: {e}")
        return False


def move_card_to_done(board_id, current_stack_id, card_id):
    """Move a Deck card to the 'Klaar' stack"""
    try:
        # First, find the "Klaar" or "Done" stack
        headers = {'OCS-APIRequest': 'true', 'Content-Type': 'application/json'}
        auth = (BOTS['maarten']['nextcloud_user'], BOTS['maarten']['nextcloud_password'])

        # Get all stacks
        resp = requests.get(
            f"{NEXTCLOUD_URL}/index.php/apps/deck/api/v1.0/boards/{board_id}/stacks",
            auth=auth, headers=headers, timeout=30
        )

        if resp.status_code != 200:
            print(f"Failed to get stacks: {resp.status_code}")
            return False

        stacks = resp.json()
        done_stack = None
        for stack in stacks:
            title = stack.get('title', '').lower()
            if 'klaar' in title or 'done' in title or 'afgerond' in title:
                done_stack = stack
                break

        if not done_stack:
            print("No 'Klaar' stack found")
            return False

        # Move card to done stack using reorder
        move_url = f"{NEXTCLOUD_URL}/index.php/apps/deck/api/v1.0/boards/{board_id}/stacks/{current_stack_id}/cards/{card_id}/reorder"
        move_data = {'stackId': done_stack['id'], 'order': 0}

        move_resp = requests.put(move_url, auth=auth, headers=headers, json=move_data, timeout=30)
        print(f"Move card response: {move_resp.status_code}")

        return move_resp.status_code == 200
    except Exception as e:
        print(f"Error moving card: {e}")
        return False

app = Flask(__name__)

# ERPNext base URL (from environment or config)
ERPNEXT_URL = os.environ.get('ERPNEXT_URL', 'https://your-erpnext.example.com')

# Load bot configurations from JSON file
BOTS_CONFIG_FILE = os.environ.get('BOTS_CONFIG_FILE', '/opt/nextcloud-claude-bot/bots_config.json')

def load_bots_config():
    """Load bot configurations from JSON file"""
    if os.path.exists(BOTS_CONFIG_FILE):
        try:
            with open(BOTS_CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading bots config: {e}")

    # Return empty config if file doesn't exist
    print(f"Warning: Bots config file not found at {BOTS_CONFIG_FILE}")
    print("Please create bots_config.json based on bots_config.json.example")
    return {}

BOTS = load_bots_config()

NEXTCLOUD_URL = os.environ.get('NEXTCLOUD_URL', 'https://your-nextcloud.example.com')
CLAUDE_PATH = os.environ.get('CLAUDE_PATH', 'claude')
INSTALL_DIR = os.environ.get('INSTALL_DIR', '/opt/nextcloud-claude-bot')
HISTORY_FILE = os.path.join(INSTALL_DIR, 'conversation_history.json')
KEY_FACTS_FILE = os.path.join(INSTALL_DIR, 'key_facts.json')
MAX_HISTORY_MESSAGES = 50  # Verhoogd voor beter geheugen
MAX_MESSAGE_LENGTH_IN_HISTORY = 500  # Truncate lange berichten in history

# WhisperFlow configuration
WHISPER_PYTHON = os.environ.get('WHISPER_PYTHON', '/opt/whisperflow/bin/python')
WHISPER_MODEL = os.environ.get('WHISPER_MODEL', 'base')  # Options: tiny, base, small, medium, large
AUDIO_EXTENSIONS = ['.mp3', '.wav', '.ogg', '.m4a', '.flac', '.webm', '.opus']

# Default Nextcloud credentials for file download (fallback, prefer per-user config)
NEXTCLOUD_USER = os.environ.get('NEXTCLOUD_USER', '')
NEXTCLOUD_PASSWORD = os.environ.get('NEXTCLOUD_PASSWORD', '')

# Conversation history storage
conversation_history = {}
key_facts = {}  # Per-conversation key facts that always get included
history_lock = threading.Lock()
facts_lock = threading.Lock()


# ============== ERPNext API Helper Functions ==============

def erpnext_request(bot_config, method, endpoint, data=None):
    """Make an authenticated request to ERPNext API"""
    url = f"{ERPNEXT_URL}/api/{endpoint}"
    headers = {
        'Authorization': f"token {bot_config['erpnext_api_key']}:{bot_config['erpnext_api_secret']}",
        'Content-Type': 'application/json'
    }

    try:
        if method == 'GET':
            resp = requests.get(url, headers=headers, params=data, timeout=30)
        elif method == 'POST':
            resp = requests.post(url, headers=headers, json=data, timeout=30)
        elif method == 'PUT':
            resp = requests.put(url, headers=headers, json=data, timeout=30)
        else:
            return None

        if resp.status_code == 200:
            return resp.json()
        else:
            print(f"ERPNext API error: {resp.status_code} - {resp.text}")
            return None
    except Exception as e:
        print(f"ERPNext request error: {e}")
        return None


def get_erpnext_documents(bot_config, doctype, filters=None, fields=None, limit=10):
    """Get documents from ERPNext"""
    params = {
        'doctype': doctype,
        'limit_page_length': limit
    }
    if filters:
        params['filters'] = json.dumps(filters)
    if fields:
        params['fields'] = json.dumps(fields)

    result = erpnext_request(bot_config, 'GET', 'resource/' + doctype, params)
    if result and 'data' in result:
        return result['data']
    return []


def create_erpnext_document(bot_config, doctype, data):
    """Create a document in ERPNext"""
    result = erpnext_request(bot_config, 'POST', f'resource/{doctype}', data)
    return result


def update_erpnext_document(bot_config, doctype, name, data):
    """Update a document in ERPNext"""
    result = erpnext_request(bot_config, 'PUT', f'resource/{doctype}/{name}', data)
    return result


def add_comment_to_erpnext(bot_config, doctype, docname, content, comment_by=None):
    """Add a comment to an ERPNext document (for task conversation logging)"""
    comment_data = {
        'doctype': 'Comment',
        'comment_type': 'Comment',
        'reference_doctype': doctype,
        'reference_name': docname,
        'content': content,
        'comment_email': bot_config['erpnext_user'],
        'comment_by': comment_by or bot_config['erpnext_user']
    }
    result = erpnext_request(bot_config, 'POST', 'resource/Comment', comment_data)
    if result:
        print(f"Added comment to {doctype}/{docname}")
    else:
        print(f"Failed to add comment to {doctype}/{docname}")
    return result


def add_comment_to_deck_card(bot_config, board_id, stack_id, card_id, content):
    """Add a comment to a Nextcloud Deck card"""
    try:
        headers = {
            'OCS-APIRequest': 'true',
            'Content-Type': 'application/json'
        }
        auth = (bot_config['nextcloud_user'], bot_config['nextcloud_password'])

        # Use OCS API endpoint - card_id is sufficient, board/stack not needed
        url = f"{NEXTCLOUD_URL}/ocs/v2.php/apps/deck/api/v1.0/cards/{card_id}/comments"
        data = {'message': content}

        resp = requests.post(url, auth=auth, headers=headers, json=data, timeout=30)

        if resp.status_code in [200, 201]:
            print(f"Added comment to Deck card {card_id}")
            return True
        else:
            print(f"Failed to add Deck comment: {resp.status_code} - {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"Error adding Deck comment: {e}")
        return False


# ============== Nextcloud File Sharing Functions ==============

def share_file_to_conversation(bot_config, file_path, conversation_token, caption=None):
    """
    Share a Nextcloud file to a Talk conversation.

    Args:
        bot_config: Bot configuration with credentials
        file_path: Path to file in Nextcloud (e.g., "/Documents/report.pdf")
        conversation_token: Talk conversation token
        caption: Optional text caption for the file

    Returns:
        dict with share info on success, None on failure
    """
    try:
        headers = {
            'OCS-APIRequest': 'true',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        auth = (bot_config['nextcloud_user'], bot_config['nextcloud_password'])

        # Build share data
        data = {
            'shareType': 10,  # 10 = share to conversation
            'shareWith': conversation_token,
            'path': file_path
        }

        # Add caption if provided
        if caption:
            import json
            data['talkMetaData'] = json.dumps({'caption': caption})

        url = f"{NEXTCLOUD_URL}/ocs/v2.php/apps/files_sharing/api/v1/shares"
        resp = requests.post(url, auth=auth, headers=headers, data=data, timeout=30)

        print(f"Share file response: {resp.status_code}")

        if resp.status_code in [200, 201]:
            print(f"Successfully shared {file_path} to conversation {conversation_token}")
            return {'success': True, 'file': file_path, 'conversation': conversation_token}
        elif resp.status_code == 403 and 'al gedeeld' in resp.text.lower():
            # File already shared - treat as success
            print(f"File already shared: {file_path}")
            return {'success': True, 'file': file_path, 'conversation': conversation_token, 'already_shared': True}
        else:
            print(f"Failed to share file: {resp.status_code} - {resp.text[:500]}")
            return None
    except Exception as e:
        print(f"Error sharing file: {e}")
        import traceback
        traceback.print_exc()
        return None


def search_nextcloud_files(bot_config, search_query, limit=10):
    """
    Search for files in Nextcloud using WebDAV PROPFIND with client-side filtering.

    Args:
        bot_config: Bot configuration
        search_query: Filename or search term
        limit: Maximum results to return

    Returns:
        list of dicts with file info (path, name, size, type)
    """
    try:
        import xml.etree.ElementTree as ET

        auth = (bot_config['nextcloud_user'], bot_config['nextcloud_password'])
        propfind_url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{bot_config['nextcloud_user']}/"

        propfind_body = '''<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:displayname/>
    <d:getcontentlength/>
    <d:getcontenttype/>
    <d:resourcetype/>
  </d:prop>
</d:propfind>'''

        headers = {'Content-Type': 'application/xml', 'Depth': 'infinity'}
        resp = requests.request('PROPFIND', propfind_url, auth=auth, headers=headers,
                               data=propfind_body, timeout=60)

        results = []
        if resp.status_code == 207:  # Multi-Status
            root = ET.fromstring(resp.text)
            ns = {'d': 'DAV:'}
            search_lower = search_query.lower()

            for response in root.findall('.//d:response', ns):
                href = response.find('d:href', ns)
                if href is not None:
                    path = urllib.parse.unquote(href.text)
                    user_prefix = f"/remote.php/dav/files/{bot_config['nextcloud_user']}"
                    if user_prefix in path:
                        file_path = path.replace(user_prefix, '')

                        # Check if matches search term
                        if search_lower in file_path.lower():
                            propstat = response.find('d:propstat', ns)
                            if propstat:
                                prop = propstat.find('d:prop', ns)
                                resourcetype = prop.find('d:resourcetype', ns) if prop is not None else None
                                is_folder = resourcetype is not None and len(resourcetype) > 0

                                if not is_folder and file_path:  # Skip folders
                                    displayname = prop.find('d:displayname', ns) if prop is not None else None
                                    contentlength = prop.find('d:getcontentlength', ns) if prop is not None else None
                                    contenttype = prop.find('d:getcontenttype', ns) if prop is not None else None

                                    results.append({
                                        'path': file_path,
                                        'name': displayname.text if displayname is not None and displayname.text else os.path.basename(file_path),
                                        'size': int(contentlength.text) if contentlength is not None and contentlength.text else 0,
                                        'type': contenttype.text if contenttype is not None else 'unknown'
                                    })

                                    if len(results) >= limit:
                                        break

        return results[:limit]

    except Exception as e:
        print(f"Error searching files: {e}")
        import traceback
        traceback.print_exc()
        return []


def search_and_share_file(bot_config, search_query, conversation_token, caption=None):
    """
    Search for a file in Nextcloud and share it to a conversation.

    Args:
        bot_config: Bot configuration
        search_query: Filename or search term
        conversation_token: Talk conversation token
        caption: Optional caption

    Returns:
        dict with result info
    """
    try:
        auth = (bot_config['nextcloud_user'], bot_config['nextcloud_password'])

        # First try exact path
        if search_query.startswith('/'):
            webdav_url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{bot_config['nextcloud_user']}{search_query}"
            resp = requests.head(webdav_url, auth=auth, timeout=10)

            if resp.status_code == 200:
                # File exists at exact path
                result = share_file_to_conversation(bot_config, search_query, conversation_token, caption)
                if result:
                    return {'success': True, 'shared': search_query}
                return {'success': False, 'error': 'Delen mislukt'}

        # Search for file
        results = search_nextcloud_files(bot_config, search_query, limit=1)

        if results:
            file_path = results[0]['path']
            result = share_file_to_conversation(bot_config, file_path, conversation_token, caption)
            if result:
                return {'success': True, 'shared': file_path, 'name': results[0]['name']}
            return {'success': False, 'error': 'Delen mislukt'}

        return {'success': False, 'error': 'Geen bestanden gevonden'}

    except Exception as e:
        print(f"Error searching for file: {e}")
        return {'success': False, 'error': str(e)}


def upload_file_to_nextcloud(bot_config, local_path, nc_path=None):
    """
    Upload a local file to Nextcloud via WebDAV.

    Args:
        bot_config: Bot configuration with credentials
        local_path: Path to local file (e.g., "/home/maarten/OpenBooks/index.html")
        nc_path: Target path in Nextcloud (default: /Bot-Uploads/<filename>)

    Returns:
        dict with upload info on success, None on failure
    """
    try:
        if not os.path.exists(local_path):
            print(f"Local file not found: {local_path}")
            return None

        filename = os.path.basename(local_path)

        # Default to Bot-Uploads folder if no path specified
        if nc_path is None:
            nc_path = f"/Bot-Uploads/{filename}"
        elif not nc_path.startswith('/'):
            nc_path = '/' + nc_path

        # Ensure target directory exists
        nc_dir = os.path.dirname(nc_path)
        if nc_dir and nc_dir != '/':
            create_nextcloud_folder(bot_config, nc_dir)

        auth = (bot_config['nextcloud_user'], bot_config['nextcloud_password'])
        webdav_url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{bot_config['nextcloud_user']}{nc_path}"

        # Read file content
        with open(local_path, 'rb') as f:
            content = f.read()

        # Determine content type
        import mimetypes
        content_type, _ = mimetypes.guess_type(local_path)
        if not content_type:
            content_type = 'application/octet-stream'

        headers = {'Content-Type': content_type}

        # Upload via WebDAV PUT
        resp = requests.put(webdav_url, auth=auth, headers=headers, data=content, timeout=60)

        print(f"Upload file response: {resp.status_code} for {nc_path}")

        if resp.status_code in [200, 201, 204]:
            print(f"Successfully uploaded {local_path} to {nc_path}")
            return {
                'success': True,
                'local_path': local_path,
                'nc_path': nc_path,
                'filename': filename,
                'size': len(content)
            }
        else:
            print(f"Failed to upload file: {resp.status_code} - {resp.text[:500]}")
            return None
    except Exception as e:
        print(f"Error uploading file: {e}")
        import traceback
        traceback.print_exc()
        return None


def create_nextcloud_folder(bot_config, folder_path):
    """Create a folder in Nextcloud via WebDAV MKCOL"""
    try:
        auth = (bot_config['nextcloud_user'], bot_config['nextcloud_password'])

        # Create folders recursively
        parts = folder_path.strip('/').split('/')
        current_path = ''

        for part in parts:
            current_path += '/' + part
            webdav_url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{bot_config['nextcloud_user']}{current_path}"

            # Try to create folder
            resp = requests.request('MKCOL', webdav_url, auth=auth, timeout=10)
            # 201 = created, 405 = already exists
            if resp.status_code not in [201, 405]:
                print(f"Warning: Could not create folder {current_path}: {resp.status_code}")

        return True
    except Exception as e:
        print(f"Error creating folder: {e}")
        return False


def upload_and_share_file(bot_config, local_path, conversation_token, nc_path=None, caption=None):
    """
    Upload a local file to Nextcloud and share it to a Talk conversation.

    Args:
        bot_config: Bot configuration
        local_path: Path to local file
        conversation_token: Talk conversation token
        nc_path: Optional target path in Nextcloud
        caption: Optional caption for the file

    Returns:
        dict with result info
    """
    # First upload the file
    upload_result = upload_file_to_nextcloud(bot_config, local_path, nc_path)

    if not upload_result or not upload_result.get('success'):
        return {'success': False, 'error': 'Upload failed'}

    # Then share it to the conversation
    share_result = share_file_to_conversation(
        bot_config,
        upload_result['nc_path'],
        conversation_token,
        caption
    )

    if share_result and share_result.get('success'):
        return {
            'success': True,
            'uploaded': upload_result['nc_path'],
            'shared': True,
            'filename': upload_result['filename']
        }
    else:
        return {
            'success': True,
            'uploaded': upload_result['nc_path'],
            'shared': False,
            'error': 'File uploaded but sharing failed'
        }


def detect_files_in_response(response_text):
    """
    Detect file paths mentioned in Claude's response that might be new files.
    Looks for patterns like "created file X" or paths to common file types.

    Returns:
        list of potential file paths
    """
    import re

    files = []

    # Common patterns for file creation
    patterns = [
        r'(?:created?|wrote|saved|generated?|made)\s+(?:the\s+)?(?:file\s+)?[`"\']?(/[^\s`"\']+\.[a-zA-Z0-9]+)[`"\']?',
        r'(?:file|bestand)\s+[`"\']?(/[^\s`"\']+\.[a-zA-Z0-9]+)[`"\']?\s+(?:is\s+)?(?:created?|aangemaakt|geschreven)',
        r'[`"\'](/home/[^\s`"\']+\.[a-zA-Z0-9]+)[`"\']',
        r'[`"\'](/opt/[^\s`"\']+\.[a-zA-Z0-9]+)[`"\']',
        r'[`"\'](/tmp/[^\s`"\']+\.[a-zA-Z0-9]+)[`"\']',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, response_text, re.IGNORECASE)
        for match in matches:
            if match not in files and os.path.exists(match):
                files.append(match)

    return files


# ============== Document Preview Functions ==============

def extract_pdf_text(file_path, max_pages=5, max_chars=3000):
    """
    Extract text from a PDF file for preview.

    Args:
        file_path: Path to PDF file
        max_pages: Maximum number of pages to extract
        max_chars: Maximum characters to return

    Returns:
        dict with text content and metadata
    """
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(file_path)
        total_pages = len(doc)
        text_parts = []

        for page_num in range(min(max_pages, total_pages)):
            page = doc[page_num]
            text = page.get_text()
            if text.strip():
                text_parts.append(f"--- Pagina {page_num + 1} ---\n{text.strip()}")

        doc.close()

        full_text = "\n\n".join(text_parts)
        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n\n... (afgekapt)"

        return {
            'success': True,
            'text': full_text,
            'total_pages': total_pages,
            'pages_extracted': min(max_pages, total_pages),
            'type': 'pdf'
        }
    except ImportError:
        return {'success': False, 'error': 'PyMuPDF niet geïnstalleerd'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def extract_odt_text(file_path, max_chars=3000):
    """
    Extract text from an ODT (OpenDocument Text) file for preview.

    Args:
        file_path: Path to ODT file
        max_chars: Maximum characters to return

    Returns:
        dict with text content and metadata
    """
    try:
        from odf import text as odf_text
        from odf.opendocument import load

        doc = load(file_path)
        paragraphs = doc.getElementsByType(odf_text.P)

        text_parts = []
        for para in paragraphs:
            # Extract text from paragraph
            para_text = ""
            for node in para.childNodes:
                if node.nodeType == node.TEXT_NODE:
                    para_text += node.data
                elif hasattr(node, 'childNodes'):
                    for child in node.childNodes:
                        if child.nodeType == child.TEXT_NODE:
                            para_text += child.data
            if para_text.strip():
                text_parts.append(para_text.strip())

        full_text = "\n\n".join(text_parts)
        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n\n... (afgekapt)"

        return {
            'success': True,
            'text': full_text,
            'paragraphs': len(text_parts),
            'type': 'odt'
        }
    except ImportError:
        return {'success': False, 'error': 'odfpy niet geïnstalleerd'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def extract_docx_text(file_path, max_chars=3000):
    """
    Extract text from a DOCX file for preview.

    Args:
        file_path: Path to DOCX file
        max_chars: Maximum characters to return

    Returns:
        dict with text content and metadata
    """
    try:
        from docx import Document

        doc = Document(file_path)
        text_parts = []

        for para in doc.paragraphs:
            if para.text.strip():
                text_parts.append(para.text.strip())

        full_text = "\n\n".join(text_parts)
        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n\n... (afgekapt)"

        return {
            'success': True,
            'text': full_text,
            'paragraphs': len(text_parts),
            'type': 'docx'
        }
    except ImportError:
        return {'success': False, 'error': 'python-docx niet geïnstalleerd'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def preview_document(file_path, max_chars=3000):
    """
    Preview a document file (PDF, ODT, DOCX).

    Args:
        file_path: Path to document file
        max_chars: Maximum characters to return

    Returns:
        dict with preview text and metadata
    """
    if not os.path.exists(file_path):
        return {'success': False, 'error': 'Bestand niet gevonden'}

    filename = os.path.basename(file_path).lower()

    if filename.endswith('.pdf'):
        return extract_pdf_text(file_path, max_chars=max_chars)
    elif filename.endswith('.odt'):
        return extract_odt_text(file_path, max_chars=max_chars)
    elif filename.endswith('.docx'):
        return extract_docx_text(file_path, max_chars=max_chars)
    else:
        return {'success': False, 'error': f'Niet-ondersteund bestandstype: {filename}'}


def is_previewable_document(filename):
    """Check if a file is a previewable document"""
    if not filename:
        return False
    lower = filename.lower()
    return lower.endswith('.pdf') or lower.endswith('.odt') or lower.endswith('.docx') or lower.endswith('.html') or lower.endswith('.htm')


def screenshot_html(file_path, output_path=None, width=1200, height=800):
    """
    Take a screenshot of an HTML file using Playwright.

    Args:
        file_path: Path to HTML file (local or URL)
        output_path: Path to save screenshot (default: temp file)
        width: Viewport width
        height: Viewport height

    Returns:
        dict with screenshot path on success
    """
    try:
        from playwright.sync_api import sync_playwright

        if output_path is None:
            output_path = tempfile.mktemp(suffix='.png')

        # Convert local path to file:// URL if needed
        if file_path.startswith('/'):
            url = f"file://{file_path}"
        elif file_path.startswith('http'):
            url = file_path
        else:
            url = f"file://{os.path.abspath(file_path)}"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': width, 'height': height})
            page.goto(url, wait_until='networkidle', timeout=30000)

            # Wait a bit for any animations/rendering
            page.wait_for_timeout(500)

            # Take full page screenshot or viewport screenshot
            page.screenshot(path=output_path, full_page=True)
            browser.close()

        return {
            'success': True,
            'screenshot_path': output_path,
            'url': url,
            'type': 'html_screenshot'
        }
    except ImportError:
        return {'success': False, 'error': 'Playwright niet geïnstalleerd'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def download_and_preview_document(file_url, nc_user, nc_password, max_chars=3000):
    """
    Download a document from Nextcloud and extract preview text.

    Args:
        file_url: WebDAV URL to the file
        nc_user: Nextcloud username
        nc_password: Nextcloud password
        max_chars: Maximum characters to return

    Returns:
        dict with preview text and metadata
    """
    try:
        # Download the file
        local_path = download_nextcloud_file(file_url, nc_user, nc_password)
        if not local_path:
            return {'success': False, 'error': 'Kon bestand niet downloaden'}

        # Extract text
        result = preview_document(local_path, max_chars)

        # Clean up temp file
        try:
            os.unlink(local_path)
        except:
            pass

        return result
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ============== Deck API Functions ==============

def get_deck_boards(bot_config):
    """Get all Deck boards for the user"""
    try:
        headers = {'OCS-APIRequest': 'true', 'Content-Type': 'application/json'}
        auth = (bot_config['nextcloud_user'], bot_config['nextcloud_password'])

        url = f"{NEXTCLOUD_URL}/index.php/apps/deck/api/v1.0/boards"
        resp = requests.get(url, auth=auth, headers=headers, timeout=30)

        if resp.status_code == 200:
            return resp.json()
        return []
    except Exception as e:
        print(f"Error getting boards: {e}")
        return []


def get_deck_stacks(bot_config, board_id):
    """Get all stacks (columns) for a board"""
    try:
        headers = {'OCS-APIRequest': 'true', 'Content-Type': 'application/json'}
        auth = (bot_config['nextcloud_user'], bot_config['nextcloud_password'])

        url = f"{NEXTCLOUD_URL}/index.php/apps/deck/api/v1.0/boards/{board_id}/stacks"
        resp = requests.get(url, auth=auth, headers=headers, timeout=30)

        if resp.status_code == 200:
            return resp.json()
        return []
    except Exception as e:
        print(f"Error getting stacks: {e}")
        return []


def create_deck_card(bot_config, board_id, stack_id, title, description=None, due_date=None):
    """Create a new card in Deck"""
    try:
        headers = {'OCS-APIRequest': 'true', 'Content-Type': 'application/json'}
        auth = (bot_config['nextcloud_user'], bot_config['nextcloud_password'])

        data = {
            'title': title,
            'type': 'plain',
            'order': 999
        }
        if description:
            data['description'] = description
        if due_date:
            data['duedate'] = due_date

        url = f"{NEXTCLOUD_URL}/index.php/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards"
        resp = requests.post(url, auth=auth, headers=headers, json=data, timeout=30)

        print(f"Create card response: {resp.status_code}")

        if resp.status_code in [200, 201]:
            return resp.json()
        else:
            print(f"Failed to create card: {resp.text[:500]}")
            return None
    except Exception as e:
        print(f"Error creating card: {e}")
        return None


def find_or_create_task(bot_config, title, description=None, board_name=None):
    """
    Find a board (by name or first available) and create a task in the first stack.
    Returns info about created card or error message.
    """
    try:
        boards = get_deck_boards(bot_config)
        if not boards:
            return {'error': 'Geen Deck boards gevonden'}

        # Find board by name or use first one
        target_board = None
        if board_name:
            for board in boards:
                if board_name.lower() in board.get('title', '').lower():
                    target_board = board
                    break

        if not target_board:
            # Use first board (usually personal board)
            target_board = boards[0]

        board_id = target_board['id']
        board_title = target_board.get('title', 'Unknown')

        # Get stacks for this board
        stacks = get_deck_stacks(bot_config, board_id)
        if not stacks:
            return {'error': f'Geen kolommen gevonden in board "{board_title}"'}

        # Find "To Do" or "Backlog" stack, or use first one
        target_stack = None
        for stack in stacks:
            stack_title = stack.get('title', '').lower()
            if 'to do' in stack_title or 'todo' in stack_title or 'backlog' in stack_title or 'nieuw' in stack_title:
                target_stack = stack
                break

        if not target_stack:
            target_stack = stacks[0]

        stack_id = target_stack['id']
        stack_title = target_stack.get('title', 'Unknown')

        # Create the card
        card = create_deck_card(bot_config, board_id, stack_id, title, description)

        if card:
            return {
                'success': True,
                'card_id': card.get('id'),
                'card_title': title,
                'board': board_title,
                'stack': stack_title,
                'url': f"{NEXTCLOUD_URL}/apps/deck/#/board/{board_id}/card/{card.get('id')}"
            }
        else:
            return {'error': 'Kon kaart niet aanmaken'}

    except Exception as e:
        print(f"Error in find_or_create_task: {e}")
        return {'error': str(e)}


# ============== WhisperFlow Transcription Functions ==============

def is_audio_file(filename):
    """Check if filename has an audio extension"""
    if not filename:
        return False
    return any(filename.lower().endswith(ext) for ext in AUDIO_EXTENSIONS)


def download_nextcloud_file(file_url, nc_user=None, nc_password=None):
    """Download a file from Nextcloud and return local path"""
    try:
        print(f"[DEBUG] Downloading file from: {file_url}")

        # Use provided credentials or defaults
        user = nc_user or NEXTCLOUD_USER
        password = nc_password or NEXTCLOUD_PASSWORD
        print(f"[DEBUG] Using credentials for user: {user}")

        # Check if this is a public share link (no auth needed)
        if '/s/' in file_url:
            # Public share link - no authentication needed
            response = requests.get(file_url, timeout=60, allow_redirects=True)
        else:
            # WebDAV URL - needs authentication
            auth = (user, password)
            response = requests.get(file_url, auth=auth, timeout=60)

        print(f"[DEBUG] Download response status: {response.status_code}")

        if response.status_code != 200:
            print(f"Failed to download file: {response.status_code}")
            print(f"[DEBUG] Response: {response.text[:500] if response.text else 'empty'}")
            return None

        # Determine extension from URL or content type
        ext = os.path.splitext(file_url.split('?')[0].split('/download')[0])[1]
        if not ext:
            content_type = response.headers.get('Content-Type', '')
            if 'audio/mpeg' in content_type or 'audio/mp3' in content_type:
                ext = '.mp3'
            elif 'audio/ogg' in content_type:
                ext = '.ogg'
            elif 'audio/wav' in content_type:
                ext = '.wav'
            else:
                ext = '.mp3'  # Default for Talk recordings

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(response.content)
            print(f"[DEBUG] Downloaded {len(response.content)} bytes to {f.name}")
            return f.name
    except Exception as e:
        print(f"Error downloading file: {e}")
        import traceback
        traceback.print_exc()
        return None


def transcribe_audio(audio_path):
    """Transcribe audio file using WhisperFlow/Whisper"""
    try:
        # Use whisper directly for transcription
        script = f'''
import whisper
import sys
model = whisper.load_model("{WHISPER_MODEL}")
result = model.transcribe("{audio_path}", language="nl")
print(result["text"])
'''
        result = subprocess.run(
            [WHISPER_PYTHON, '-c', script],
            capture_output=True,
            text=True,
            timeout=300  # 5 minutes max
        )

        if result.returncode == 0:
            return result.stdout.strip()
        else:
            print(f"Whisper error: {result.stderr}")
            return None
    except subprocess.TimeoutExpired:
        return None
    except Exception as e:
        print(f"Transcription error: {e}")
        return None
    finally:
        # Clean up temp file
        try:
            if audio_path and os.path.exists(audio_path):
                os.unlink(audio_path)
        except:
            pass


def extract_file_info(message_data, message_parameters=None):
    """Extract file information from a Nextcloud Talk message"""
    # Debug: log the full webhook data structure
    print(f"[DEBUG] Full webhook data keys: {message_data.keys()}")
    print(f"[DEBUG] Message parameters passed: {message_parameters}")

    obj = message_data.get('object', {})
    print(f"[DEBUG] Object keys: {obj.keys()}")

    # Check for voice message (Talk voice recordings)
    # Voice messages have messageType = 'voice-message' or 'record-audio'
    message_type = obj.get('messageType', '')
    print(f"[DEBUG] Message type: {message_type}")

    if message_type in ['voice-message', 'record-audio']:
        # Voice message - get the file URL
        file_url = obj.get('id', '')
        file_name = obj.get('name', 'voice.ogg')
        print(f"[DEBUG] Voice message detected: {file_url}")
        return {'url': file_url, 'name': file_name, 'type': 'audio'}

    # Check mediaType field
    media_type = obj.get('mediaType', '')
    print(f"[DEBUG] Media type: {media_type}")

    if media_type and media_type.startswith('audio/'):
        file_url = obj.get('id', '')
        file_name = obj.get('name', '')
        print(f"[DEBUG] Audio media type detected: {file_url}")
        return {'url': file_url, 'name': file_name, 'type': 'audio'}

    # Check for file share in message_parameters (parsed from JSON content)
    if message_parameters:
        print(f"[DEBUG] Checking message_parameters: {message_parameters}")

        # Direct check for 'file' key (Talk voice recordings format)
        if isinstance(message_parameters, dict) and 'file' in message_parameters:
            file_data = message_parameters['file']
            print(f"[DEBUG] Found 'file' parameter: {file_data}")
            if isinstance(file_data, dict):
                file_name = file_data.get('name', '')
                file_path = file_data.get('path', '')
                file_link = file_data.get('link', '')
                mimetype = file_data.get('mimetype', '')
                file_id = file_data.get('id', '')

                print(f"[DEBUG] File data: name={file_name}, path={file_path}, link={file_link}, mimetype={mimetype}")

                # Check if it's an audio file
                is_audio = (
                    (mimetype and mimetype.startswith('audio/')) or
                    is_audio_file(file_name)
                )

                if is_audio:
                    # Get the file owner from the message parameters (actor who shared the file)
                    file_owner = message_parameters.get('actor', {}).get('id', NEXTCLOUD_USER)

                    # Talk recordings are stored in /Talk/ folder
                    # URL encode the filename for WebDAV
                    encoded_name = urllib.parse.quote(file_name)

                    # Use WebDAV URL with Talk folder path
                    file_url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{file_owner}/Talk/{encoded_name}"
                    print(f"[DEBUG] Audio from file parameter: {file_url}")
                    return {'url': file_url, 'name': file_name, 'type': 'audio', 'mimetype': mimetype, 'link': file_link, 'id': file_id, 'owner': file_owner}

        # message_parameters can be a dict or list - iterate through other params
        params_to_check = message_parameters
        if isinstance(message_parameters, list):
            params_to_check = {str(i): v for i, v in enumerate(message_parameters)}

        for key, value in (params_to_check.items() if isinstance(params_to_check, dict) else []):
            if key == 'file' or key == 'actor':
                continue  # Already handled above
            print(f"[DEBUG] Checking param {key}: {value}")
            if isinstance(value, dict):
                param_type = value.get('type', '')
                file_name = value.get('name', '')
                file_path = value.get('path', '')
                file_link = value.get('link', '')
                mimetype = value.get('mimetype', '')

                print(f"[DEBUG] Param type={param_type}, name={file_name}, mimetype={mimetype}")

                # Check if it's an audio file
                is_audio = (
                    (mimetype and mimetype.startswith('audio/')) or
                    is_audio_file(file_name) or
                    param_type == 'voice-message'
                )

                if is_audio and (file_path or file_link or file_name):
                    # Construct WebDAV URL
                    if file_path:
                        file_url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{NEXTCLOUD_USER}/{file_path}"
                    elif file_link:
                        file_url = f"{file_link}/download"
                    else:
                        file_url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{NEXTCLOUD_USER}/{file_name}"
                    print(f"[DEBUG] Audio from parameters: {file_url}")
                    return {'url': file_url, 'name': file_name, 'type': 'audio', 'mimetype': mimetype}

    # Check for file mention in content
    content = obj.get('content', '')
    print(f"[DEBUG] Content: {content[:200] if content else 'empty'}")

    # Look for file patterns in the message
    # Pattern: {file:XXX|name:filename.mp3}
    file_match = re.search(r'\{file:(\d+)\|name:([^}]+)\}', content)
    if file_match:
        file_id = file_match.group(1)
        file_name = file_match.group(2)
        print(f"[DEBUG] File pattern found: id={file_id}, name={file_name}")
        if is_audio_file(file_name):
            # Construct the download URL for the file
            file_url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{NEXTCLOUD_USER}/{file_name}"
            print(f"[DEBUG] Audio file from pattern: {file_url}")
            return {'url': file_url, 'name': file_name, 'type': 'audio', 'id': file_id}

    # Check for file share parameters in object (fallback)
    parameters = obj.get('parameters', {})
    if parameters:
        print(f"[DEBUG] Object parameters: {parameters}")
        for key, value in parameters.items() if isinstance(parameters, dict) else []:
            if isinstance(value, dict):
                if value.get('type') == 'file':
                    file_name = value.get('name', '')
                    file_path = value.get('path', '')
                    file_link = value.get('link', '')
                    print(f"[DEBUG] File share found: {file_name}, path={file_path}, link={file_link}")
                    if is_audio_file(file_name):
                        # Construct WebDAV URL
                        if file_path:
                            file_url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{NEXTCLOUD_USER}{file_path}"
                        elif file_link:
                            file_url = file_link
                        else:
                            file_url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{NEXTCLOUD_USER}/{file_name}"
                        print(f"[DEBUG] Audio from file share: {file_url}")
                        return {'url': file_url, 'name': file_name, 'type': 'audio'}

    return None


# ============== History Functions ==============

def load_history():
    """Load conversation history from file"""
    global conversation_history, key_facts
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f:
                conversation_history = json.load(f)
            print(f"Loaded history for {len(conversation_history)} conversations")
    except Exception as e:
        print(f"Error loading history: {e}")
        conversation_history = {}

    # Load key facts
    try:
        if os.path.exists(KEY_FACTS_FILE):
            with open(KEY_FACTS_FILE, 'r') as f:
                key_facts = json.load(f)
            print(f"Loaded key facts for {len(key_facts)} conversations")
    except Exception as e:
        print(f"Error loading key facts: {e}")
        key_facts = {}


def save_history():
    """Save conversation history to file"""
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump(conversation_history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving history: {e}")


def save_key_facts():
    """Save key facts to file"""
    global key_facts
    try:
        print(f"[DEBUG] Saving key facts: {key_facts}")
        with open(KEY_FACTS_FILE, 'w') as f:
            json.dump(key_facts, f, indent=2, ensure_ascii=False)
        print(f"[DEBUG] Key facts saved to {KEY_FACTS_FILE}")
    except Exception as e:
        print(f"Error saving key facts: {e}")
        import traceback
        traceback.print_exc()


def add_key_fact(token, fact):
    """Add a key fact to remember for this conversation"""
    global key_facts
    with facts_lock:
        if token not in key_facts:
            key_facts[token] = []

        # Avoid duplicates
        if fact not in key_facts[token]:
            key_facts[token].append(fact)
            # Keep max 20 facts per conversation
            if len(key_facts[token]) > 20:
                key_facts[token] = key_facts[token][-20:]
            save_key_facts()
            print(f"[DEBUG] Saved key fact for {token}: {fact}")
            return True
    return False


def get_key_facts(token):
    """Get key facts for a conversation"""
    with facts_lock:
        return key_facts.get(token, [])


def add_to_history(token, role, name, content):
    """Add a message to conversation history"""
    with history_lock:
        if token not in conversation_history:
            conversation_history[token] = []

        conversation_history[token].append({
            'role': role,
            'name': name,
            'content': content,
            'timestamp': datetime.now().isoformat()
        })

        if len(conversation_history[token]) > MAX_HISTORY_MESSAGES * 2:
            conversation_history[token] = conversation_history[token][-(MAX_HISTORY_MESSAGES * 2):]

        save_history()


def parse_message_content(content):
    """Parse message content, extracting text from JSON if needed"""
    if not content:
        return ""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and 'message' in parsed:
            return parsed['message']
    except (json.JSONDecodeError, TypeError):
        pass
    return content


def truncate_message(content, max_length=500):
    """Truncate a message for history display, keeping key info"""
    if len(content) <= max_length:
        return content

    # For long messages, keep beginning and end
    half = (max_length - 20) // 2
    return content[:half] + "\n...[ingekort]...\n" + content[-half:]


def get_history_context(token):
    """Get formatted conversation history for context with key facts"""
    lines = []

    # First add key facts if available
    facts = get_key_facts(token)
    if facts:
        lines.append("=== BELANGRIJKE FEITEN (onthoud dit) ===")
        for i, fact in enumerate(facts, 1):
            lines.append(f"{i}. {fact}")
        lines.append("")

    # Then add conversation history
    with history_lock:
        if token not in conversation_history:
            if lines:
                lines.append("=== Nieuw gesprek ===")
                return "\n".join(lines)
            return ""

        messages = conversation_history[token]
        if not messages:
            if lines:
                lines.append("=== Nieuw gesprek ===")
                return "\n".join(lines)
            return ""

        lines.append("=== GESPREKSGESCHIEDENIS ===")

        # Show last N messages (prioritize recent)
        recent_messages = messages[-MAX_HISTORY_MESSAGES:]

        for msg in recent_messages:
            # Parse JSON content if needed
            content = parse_message_content(msg['content'])
            # Truncate long messages
            content = truncate_message(content, MAX_MESSAGE_LENGTH_IN_HISTORY)

            timestamp = msg.get('timestamp', '')
            if timestamp:
                # Extract just date and time
                try:
                    dt = datetime.fromisoformat(timestamp)
                    time_str = dt.strftime("%d/%m %H:%M")
                except:
                    time_str = ""
            else:
                time_str = ""

            if msg['role'] == 'user':
                prefix = f"[{time_str}] {msg['name']}" if time_str else msg['name']
                lines.append(f"**{prefix}:** {content}")
            else:
                prefix = f"[{time_str}] Claude" if time_str else "Claude"
                lines.append(f"**{prefix}:** {content}")

        lines.append("")
        lines.append("=== NIEUW BERICHT ===")

        return "\n".join(lines)


# ============== Nextcloud & Claude Functions ==============

def verify_signature(secret, random_header, body, signature):
    """Verify the HMAC-SHA256 signature from Nextcloud"""
    if not secret:
        return False
    expected = hmac.new(
        secret.encode(),
        (random_header + body).encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected.lower(), signature.lower())


def send_message(secret, token, message, reply_to=None):
    """Send a message back to Nextcloud Talk"""
    url = f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v1/bot/{token}/message"

    random_str = os.urandom(32).hex()
    data = {"message": message}
    if reply_to:
        data["replyTo"] = reply_to
    body = json.dumps(data)

    signature = hmac.new(
        secret.encode(),
        (random_str + message).encode(),
        hashlib.sha256
    ).hexdigest()

    headers = {
        'Content-Type': 'application/json',
        'OCS-APIRequest': 'true',
        'X-Nextcloud-Talk-Bot-Random': random_str,
        'X-Nextcloud-Talk-Bot-Signature': signature
    }

    try:
        resp = requests.post(url, headers=headers, data=body, timeout=30)
        print(f"Send message response: {resp.status_code}")
        return resp.status_code == 201
    except Exception as e:
        print(f"Error sending message: {e}")
        return False


def call_claude(prompt, working_dir, config_dir, bot_user, erpnext_user, task_context=None):
    """Call Claude CLI with user-specific configuration"""
    try:
        env = os.environ.copy()
        env['HOME'] = config_dir  # Use user-specific config directory

        # Check if this is a task-specific conversation
        if task_context:
            system_context = f"""Je bent een taak-specifieke AI assistent voor Impertio.

**HUIDIGE TAAK:** {task_context['card_title']}
{f"**Beschrijving:** {task_context.get('card_description', '')}" if task_context.get('card_description') else ""}

Je focus is volledig op het voltooien van deze specifieke taak.
Wees proactief: stel vragen als je meer informatie nodig hebt.
Rapporteer je voortgang duidelijk.
Vraag om goedkeuring voor belangrijke acties (emails versturen, offertes maken, etc.)

**GEHEUGEN:**
Als de gebruiker iets belangrijks deelt voor deze taak, suggereer /remember te gebruiken.
Key facts worden ALTIJD bovenaan de context getoond - je vergeet ze nooit.

**BESTANDEN ZOEKEN EN DELEN:**
/zoek zoekterm - Zoek bestanden in Nextcloud
/vind zoekterm - Zoek en deel automatisch eerste resultaat
/share /pad/naar/bestand.pdf - Deel bestand uit Nextcloud
/upload /lokaal/pad/bestand - Upload lokaal bestand en deel in chat
/preview /pad/document - Preview PDF, ODT, DOCX of HTML

Als je een bestand hebt aangemaakt (HTML, PDF, etc.), kan de gebruiker het delen met:
/upload /home/maarten/OpenBooks/index.html

**TAAK AFRONDEN:**
Wanneer de taak voltooid is, kan de gebruiker dit doen door:
- Te typen: "taak afronden", "taak is klaar", "we zijn klaar", etc.
- Het commando /done te gebruiken
Als je denkt dat de taak klaar is, vraag dan proactief of de gebruiker de taak wil afronden.
Na het afronden wordt de kaart verplaatst naar "Klaar" en deze chat wordt gesloten.

Je werkt namens {bot_user} (ERPNext account: {erpnext_user}).

Beschikbare MCP tools:
- erpnext: Voor klanten, offertes, facturen, items, projecten
- nextcloud: Voor bestanden, agenda, notities, delen, EN Deck taken (create_card, list_boards, get_board)
- mailcow: Voor email beheer

BELANGRIJK: Gebruik de MCP tools proactief! Als de gebruiker iets vraagt wat je kunt doen met MCP tools, doe het dan direct.

Antwoord altijd in het Nederlands, tenzij anders gevraagd.

"""
        else:
            # Add context about which user is making the request
            system_context = f"""Je bent een behulpzame AI assistent voor Impertio.
Je werkt namens {bot_user} (ERPNext account: {erpnext_user}).
Alle ERPNext acties worden uitgevoerd met de credentials van {erpnext_user}.

**GEHEUGEN:**
Als de gebruiker iets belangrijks deelt dat je moet onthouden (namen, voorkeuren, projectdetails, etc.),
suggereer dan om /remember te gebruiken. Bijvoorbeeld:
"Dat is handig om te weten! Typ `/remember Klant X heeft voorkeur voor email contact` zodat ik dit onthoud."

Key facts die zijn opgeslagen worden ALTIJD bovenaan de context getoond, dus je vergeet ze nooit.

**TAKEN AANMAKEN IN NEXTCLOUD DECK:**
Wanneer de gebruiker vraagt om een taak aan te maken (bijv. "voeg toe aan Deck", "maak een taak", "zet op de todo lijst"):
- Gebruik DIRECT de nextcloud MCP tool `create_card` met:
  - boardId: 3 (Impertio hoofdbord)
  - stackId: 8 (Te doen lijst)
  - title: de titel van de taak
  - description: optionele beschrijving
- Bevestig daarna dat de taak is aangemaakt.
- Alternatief: gebruiker kan ook /task <titel> | <beschrijving> gebruiken.

**BESTANDEN DELEN:**
Je kunt bestanden uit Nextcloud delen in deze chat:
/share /pad/naar/bestand.pdf
Voorbeeld: /share /Documents/Offertes/offerte-2024.pdf

Je kunt ook lokale bestanden uploaden en delen:
/upload /lokaal/pad/bestand
Voorbeeld: /upload /home/maarten/rapport.pdf

Beschikbare MCP tools:
- erpnext: Voor klanten, offertes, facturen, items, etc. (draait als {erpnext_user})
- nextcloud: Voor bestanden, agenda, notities, delen, EN Deck taken (create_card, list_boards, get_board)
- mailcow: Voor email beheer

BELANGRIJK: Gebruik de MCP tools proactief! Als de gebruiker iets vraagt wat je kunt doen met MCP tools, doe het dan direct.

"""
        full_prompt = system_context + prompt

        cmd = [CLAUDE_PATH, '--permission-mode', 'bypassPermissions', '-p', full_prompt]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minutes
            cwd=working_dir,
            env=env
        )

        response = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            response = f"Error: {result.stderr.strip()}"

        if len(response) > 30000:
            response = response[:30000] + "\n\n... (afgekapt)"

        return response if response else "Geen antwoord van Claude."

    except subprocess.TimeoutExpired:
        return "Claude timeout - het verzoek duurde te lang (max 10 min)."
    except Exception as e:
        return f"Fout: {str(e)}"


def handle_webhook(user):
    """Generic webhook handler for any user"""
    bot_config = BOTS.get(user)
    if not bot_config:
        return jsonify({'error': 'Unknown bot'}), 404

    signature = request.headers.get('X-Nextcloud-Talk-Signature', '')
    random_header = request.headers.get('X-Nextcloud-Talk-Random', '')
    body = request.get_data(as_text=True)

    print(f"[{user}] Received webhook, signature present: {bool(signature)}")

    if not verify_signature(bot_config['secret'], random_header, body, signature):
        print(f"[{user}] Invalid signature!")
        return jsonify({'error': 'Invalid signature'}), 401

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return jsonify({'error': 'Invalid JSON'}), 400

    activity_type = data.get('type')
    print(f"[{user}] Activity type: {activity_type}")
    print(f"[{user}] Full webhook data: {json.dumps(data, indent=2, ensure_ascii=False)[:2000]}")

    # Handle both 'Create' (normal messages) and 'Activity' (voice recordings, file shares)
    if activity_type not in ['Create', 'Activity']:
        return jsonify({'status': 'ignored'}), 200

    actor = data.get('actor', {})
    obj = data.get('object', {})
    target = data.get('target', {})

    message_content_raw = obj.get('content', '')
    message_id = obj.get('id', '').split('/')[-1] if obj.get('id') else None
    token = target.get('id', '').split('/')[-1] if target.get('id') else None
    actor_name = actor.get('name', 'Unknown')
    reply_to = int(message_id) if message_id and message_id.isdigit() else None

    # Skip bot's own messages
    if actor.get('type') == 'Application':
        return jsonify({'status': 'ignored'}), 200

    if not message_content_raw or not token:
        return jsonify({'status': 'ignored'}), 200

    # Parse the message content - it's usually a JSON string
    message_content = message_content_raw
    message_parameters = {}
    try:
        parsed_content = json.loads(message_content_raw)
        if isinstance(parsed_content, dict):
            message_content = parsed_content.get('message', message_content_raw)
            message_parameters = parsed_content.get('parameters', {})
            print(f"[{user}] Parsed message: {message_content[:100]}")
            print(f"[{user}] Message parameters: {message_parameters}")
    except (json.JSONDecodeError, TypeError):
        # Not JSON, use as-is
        pass

    print(f"[{user}] Message from {actor_name} in {token}: {message_content[:100]}")

    # Check for special commands
    if message_content.strip().lower() == '/reset':
        with history_lock:
            if token in conversation_history:
                del conversation_history[token]
                save_history()
        success = send_message(bot_config['secret'], token, "Gespreksgeschiedenis gewist. We beginnen opnieuw!")
        return jsonify({'status': 'ok' if success else 'failed'}), 200

    if message_content.strip().lower() == '/history':
        with history_lock:
            count = len(conversation_history.get(token, []))
        success = send_message(bot_config['secret'], token, f"Dit gesprek bevat {count} berichten in de geschiedenis.")
        return jsonify({'status': 'ok' if success else 'failed'}), 200

    if message_content.strip().lower() == '/whoami':
        info = f"Bot: {user}\nERPNext user: {bot_config['erpnext_user']}\nConfig: {bot_config['config_dir']}"
        success = send_message(bot_config['secret'], token, info)
        return jsonify({'status': 'ok' if success else 'failed'}), 200

    # Remember command - save key facts
    if message_content.strip().lower().startswith('/remember '):
        fact = message_content.strip()[10:].strip()
        if not fact:
            success = send_message(bot_config['secret'], token, "Gebruik: /remember <feit om te onthouden>")
            return jsonify({'status': 'ok' if success else 'failed'}), 200

        if add_key_fact(token, fact):
            success = send_message(bot_config['secret'], token, f"✅ Onthouden: {fact}")
        else:
            success = send_message(bot_config['secret'], token, f"Dit feit was al opgeslagen.")
        return jsonify({'status': 'ok' if success else 'failed'}), 200

    # Show saved facts
    if message_content.strip().lower() == '/facts':
        facts = get_key_facts(token)
        if facts:
            facts_text = "**Opgeslagen feiten voor dit gesprek:**\n\n"
            for i, fact in enumerate(facts, 1):
                facts_text += f"{i}. {fact}\n"
            facts_text += "\nGebruik /forget <nummer> om een feit te verwijderen."
        else:
            facts_text = "Geen opgeslagen feiten voor dit gesprek.\n\nGebruik /remember <feit> om iets te onthouden."
        success = send_message(bot_config['secret'], token, facts_text)
        return jsonify({'status': 'ok' if success else 'failed'}), 200

    # Forget a fact
    if message_content.strip().lower().startswith('/forget '):
        try:
            fact_num = int(message_content.strip()[8:].strip()) - 1
            with facts_lock:
                if token in key_facts and 0 <= fact_num < len(key_facts[token]):
                    removed = key_facts[token].pop(fact_num)
                    save_key_facts()
                    success = send_message(bot_config['secret'], token, f"✅ Vergeten: {removed}")
                else:
                    success = send_message(bot_config['secret'], token, "Ongeldig nummer. Gebruik /facts om de lijst te zien.")
        except ValueError:
            success = send_message(bot_config['secret'], token, "Gebruik: /forget <nummer>")
        return jsonify({'status': 'ok' if success else 'failed'}), 200

    # Task creation command
    if message_content.strip().lower().startswith('/task '):
        task_text = message_content.strip()[6:].strip()
        if not task_text:
            success = send_message(bot_config['secret'], token, "Gebruik: /task <taak titel>\n\nVoorbeeld: /task Offerte maken voor klant X")
            return jsonify({'status': 'ok' if success else 'failed'}), 200

        send_message(bot_config['secret'], token, f"Taak aanmaken: {task_text}...")

        # Parse optional description (after |)
        title = task_text
        description = None
        if '|' in task_text:
            parts = task_text.split('|', 1)
            title = parts[0].strip()
            description = parts[1].strip()

        result = find_or_create_task(bot_config, title, description)

        if result.get('success'):
            success = send_message(bot_config['secret'], token,
                f"✅ **Taak aangemaakt!**\n\n**Titel:** {result['card_title']}\n**Board:** {result['board']}\n**Kolom:** {result['stack']}\n\n🔗 {result['url']}")
        else:
            success = send_message(bot_config['secret'], token, f"❌ Kon taak niet aanmaken: {result.get('error', 'Onbekende fout')}")
        return jsonify({'status': 'ok' if success else 'failed'}), 200

    # List boards command
    if message_content.strip().lower() == '/boards':
        boards = get_deck_boards(bot_config)
        if boards:
            boards_text = "**Jouw Deck boards:**\n\n"
            for board in boards:
                boards_text += f"- {board.get('title', 'Naamloos')} (ID: {board.get('id')})\n"
        else:
            boards_text = "Geen Deck boards gevonden."
        success = send_message(bot_config['secret'], token, boards_text)
        return jsonify({'status': 'ok' if success else 'failed'}), 200

    if message_content.strip().lower() == '/help':
        # Check if this is a task conversation
        task_bot = get_task_bot_by_token(token)
        if task_bot:
            help_text = f"""**Taak:** {task_bot['card_title']}

**Taak afronden:**
- Zeg "taak afronden", "we zijn klaar", "taak is af", etc.
- Of typ /done

**Commando's:**
/done - Markeer taak als afgerond
/status - Toon taak status
/share /pad/bestand - Deel bestand uit Nextcloud
/upload /lokaal/pad - Upload lokaal bestand naar chat
/preview /pad/document - Preview PDF, ODT, DOCX of HTML
/remember <feit> - Sla belangrijk feit op
/facts - Toon opgeslagen feiten
/forget <nr> - Vergeet een feit
/reset - Wis gespreksgeschiedenis
/help - Toon dit help bericht"""
        else:
            help_text = """**Commando's:**

**Geheugen:**
/remember <feit> - Sla een belangrijk feit op (ik onthoud dit!)
/facts - Toon opgeslagen feiten
/forget <nr> - Vergeet een feit

**Taken:**
/task <titel> - Maak nieuwe Deck taak aan
/task <titel> | <beschrijving> - Met beschrijving
/boards - Toon jouw Deck boards

**Bestanden:**
/zoek zoekterm - Zoek bestanden in Nextcloud
/vind zoekterm - Zoek en deel automatisch eerste resultaat
/share /pad/bestand - Deel bestand uit Nextcloud
/upload /lokaal/pad - Upload lokaal bestand naar chat
/preview /pad/document - Preview PDF, ODT, DOCX of HTML

**Audio:**
/transcribe - Transcribeer audio (stuur eerst audio)

**Overig:**
/reset - Wis gespreksgeschiedenis
/history - Toon aantal berichten
/whoami - Toon bot info
/help - Dit help bericht

💡 **Tip:** Gebruik /remember om belangrijke dingen te onthouden, dan vergeet ik ze niet!"""
        success = send_message(bot_config['secret'], token, help_text)
        return jsonify({'status': 'ok' if success else 'failed'}), 200

    # Share file command
    if message_content.strip().lower().startswith('/share '):
        file_path = message_content.strip()[7:].strip()  # Remove '/share ' prefix
        if not file_path:
            success = send_message(bot_config['secret'], token, "Gebruik: /share /pad/naar/bestand.pdf")
            return jsonify({'status': 'ok' if success else 'failed'}), 200

        # Ensure path starts with /
        if not file_path.startswith('/'):
            file_path = '/' + file_path

        send_message(bot_config['secret'], token, f"Bestand delen: {file_path}...")
        result = share_file_to_conversation(bot_config, file_path, token)

        if result and result.get('success'):
            success = send_message(bot_config['secret'], token, f"✅ Bestand gedeeld: {file_path}")
        else:
            success = send_message(bot_config['secret'], token, f"❌ Kon bestand niet delen: {file_path}\n\nControleer of het pad correct is en of je toegang hebt tot het bestand.")

        return jsonify({'status': 'ok' if success else 'failed'}), 200

    # Upload local file command
    if message_content.strip().lower().startswith('/upload '):
        local_path = message_content.strip()[8:].strip()  # Remove '/upload ' prefix
        if not local_path:
            success = send_message(bot_config['secret'], token, "Gebruik: /upload /pad/naar/lokaal/bestand.pdf\n\nVoorbeeld: /upload /home/maarten/OpenBooks/index.html")
            return jsonify({'status': 'ok' if success else 'failed'}), 200

        # Check if file exists
        if not os.path.exists(local_path):
            success = send_message(bot_config['secret'], token, f"❌ Bestand niet gevonden: {local_path}")
            return jsonify({'status': 'ok' if success else 'failed'}), 200

        send_message(bot_config['secret'], token, f"Uploaden en delen: {os.path.basename(local_path)}...")
        result = upload_and_share_file(bot_config, local_path, token)

        if result and result.get('success'):
            if result.get('shared'):
                success = send_message(bot_config['secret'], token, f"✅ Bestand geüpload en gedeeld: {result['filename']}\n📁 Nextcloud pad: {result['uploaded']}")
            else:
                success = send_message(bot_config['secret'], token, f"⚠️ Bestand geüpload maar delen mislukt: {result['filename']}\n📁 Nextcloud pad: {result['uploaded']}")
        else:
            success = send_message(bot_config['secret'], token, f"❌ Kon bestand niet uploaden: {local_path}\n\nFout: {result.get('error', 'Onbekende fout')}")

        return jsonify({'status': 'ok' if success else 'failed'}), 200

    # Search and share file command
    if message_content.strip().lower().startswith('/zoek '):
        search_query = message_content.strip()[6:].strip()  # Remove '/zoek ' prefix
        if not search_query:
            success = send_message(bot_config['secret'], token, "Gebruik: /zoek zoekterm\n\nVoorbeeld: /zoek offerte\nVoorbeeld: /zoek rapport.pdf")
            return jsonify({'status': 'ok' if success else 'failed'}), 200

        send_message(bot_config['secret'], token, f"🔍 Zoeken naar: {search_query}...")

        # Search for files
        results = search_nextcloud_files(bot_config, search_query, limit=5)

        if results:
            # Format results
            result_text = f"**🔍 Zoekresultaten voor '{search_query}':**\n\n"
            for i, file in enumerate(results, 1):
                size_kb = file['size'] / 1024 if file['size'] > 0 else 0
                if size_kb >= 1024:
                    size_str = f"{size_kb/1024:.1f} MB"
                else:
                    size_str = f"{size_kb:.0f} KB"
                result_text += f"**{i}.** `{file['path']}`\n    📄 {file['name']} ({size_str})\n\n"

            result_text += "---\n💡 **Gebruik** `/share /pad/naar/bestand` **om een bestand te delen**"
            success = send_message(bot_config['secret'], token, result_text)
        else:
            success = send_message(bot_config['secret'], token, f"❌ Geen bestanden gevonden voor: {search_query}")

        return jsonify({'status': 'ok' if success else 'failed'}), 200

    # Quick search and share (first result)
    if message_content.strip().lower().startswith('/vind '):
        search_query = message_content.strip()[6:].strip()  # Remove '/vind ' prefix
        if not search_query:
            success = send_message(bot_config['secret'], token, "Gebruik: /vind zoekterm\n\nZoekt en deelt automatisch het eerste resultaat.\nVoorbeeld: /vind offerte-2024.pdf")
            return jsonify({'status': 'ok' if success else 'failed'}), 200

        send_message(bot_config['secret'], token, f"🔍 Zoeken en delen: {search_query}...")

        # Search and share first result
        result = search_and_share_file(bot_config, search_query, token)

        if result and result.get('success'):
            success = send_message(bot_config['secret'], token, f"✅ Bestand gevonden en gedeeld:\n📄 {result.get('name', result.get('shared'))}")
        else:
            success = send_message(bot_config['secret'], token, f"❌ {result.get('error', 'Geen bestanden gevonden')}")

        return jsonify({'status': 'ok' if success else 'failed'}), 200

    # Preview document command
    if message_content.strip().lower().startswith('/preview '):
        file_path = message_content.strip()[9:].strip()  # Remove '/preview ' prefix
        if not file_path:
            success = send_message(bot_config['secret'], token, "Gebruik: /preview /pad/naar/document\n\nOndersteunde formaten: PDF, ODT, DOCX, HTML")
            return jsonify({'status': 'ok' if success else 'failed'}), 200

        filename_lower = os.path.basename(file_path).lower()
        is_html = filename_lower.endswith('.html') or filename_lower.endswith('.htm')

        # Check if it's a Nextcloud path or local path
        is_local = file_path.startswith('/home/') or file_path.startswith('/opt/') or file_path.startswith('/tmp/')

        if is_local:
            # Local file
            if not os.path.exists(file_path):
                success = send_message(bot_config['secret'], token, f"❌ Bestand niet gevonden: {file_path}")
                return jsonify({'status': 'ok' if success else 'failed'}), 200

            if is_html:
                # HTML file - upload and share so Talk shows native preview
                send_message(bot_config['secret'], token, f"📄 HTML delen: {os.path.basename(file_path)}...")
                share_result = upload_and_share_file(bot_config, file_path, token,
                                                     f"/Bot-Previews/{os.path.basename(file_path)}")

                if share_result and share_result.get('success'):
                    success = send_message(bot_config['secret'], token, f"📄 **Preview: {os.path.basename(file_path)}**\n\n*Klik op het bestand om te openen in Nextcloud*")
                else:
                    success = send_message(bot_config['secret'], token, f"❌ Kon bestand niet delen: {share_result.get('error', 'Onbekende fout')}")
            else:
                # PDF/ODT/DOCX - extract text
                send_message(bot_config['secret'], token, f"📄 Preview genereren: {os.path.basename(file_path)}...")
                result = preview_document(file_path)

                if result and result.get('success'):
                    preview_text = f"**📄 Preview: {os.path.basename(file_path)}**\n"
                    if result.get('total_pages'):
                        preview_text += f"*{result['total_pages']} pagina's*\n"
                    elif result.get('paragraphs'):
                        preview_text += f"*{result['paragraphs']} alinea's*\n"
                    preview_text += f"\n---\n\n{result['text']}"
                    success = send_message(bot_config['secret'], token, preview_text)
                else:
                    success = send_message(bot_config['secret'], token, f"❌ Kon preview niet maken: {result.get('error', 'Onbekende fout')}")
        else:
            # Nextcloud path - download first
            if not file_path.startswith('/'):
                file_path = '/' + file_path

            file_url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{bot_config['nextcloud_user']}{file_path}"

            if is_html:
                # HTML - share directly from Nextcloud so Talk shows native preview
                send_message(bot_config['secret'], token, f"📄 HTML delen: {os.path.basename(file_path)}...")

                # Share the existing Nextcloud file to the conversation
                share_result = share_file_to_conversation(bot_config, file_path, token)

                if share_result and share_result.get('success'):
                    success = send_message(bot_config['secret'], token, f"📄 **Preview: {os.path.basename(file_path)}**\n\n*Klik op het bestand om te openen in Nextcloud*")
                else:
                    success = send_message(bot_config['secret'], token, f"❌ Kon bestand niet delen: {share_result.get('error', 'Onbekende fout')}")
            else:
                # PDF/ODT/DOCX - extract text
                send_message(bot_config['secret'], token, f"📄 Preview genereren: {os.path.basename(file_path)}...")
                result = download_and_preview_document(file_url, bot_config['nextcloud_user'], bot_config['nextcloud_password'])

                if result and result.get('success'):
                    preview_text = f"**📄 Preview: {os.path.basename(file_path)}**\n"
                    if result.get('total_pages'):
                        preview_text += f"*{result['total_pages']} pagina's*\n"
                    elif result.get('paragraphs'):
                        preview_text += f"*{result['paragraphs']} alinea's*\n"
                    preview_text += f"\n---\n\n{result['text']}"
                    success = send_message(bot_config['secret'], token, preview_text)
                else:
                    success = send_message(bot_config['secret'], token, f"❌ Kon preview niet maken: {result.get('error', 'Onbekende fout')}")

        return jsonify({'status': 'ok' if success else 'failed'}), 200

    # Task-specific commands
    if message_content.strip().lower() == '/done':
        task_bot = get_task_bot_by_token(token)
        if task_bot:
            # Mark task as completed in database and move card
            if complete_task(token, bot_config, task_bot):
                send_message(bot_config['secret'], token,
                    f"✅ **Taak afgerond!**\n\nDe taak \"{task_bot['card_title']}\" is gemarkeerd als voltooid en verplaatst naar Klaar.\n\n*Deze conversatie wordt over 3 seconden gesloten...*")

                # Close conversation after short delay
                import time
                time.sleep(3)
                close_conversation(token, bot_config['nextcloud_user'], bot_config['nextcloud_password'])
                success = True
            else:
                success = send_message(bot_config['secret'], token, "Fout bij afronden van de taak.")
        else:
            success = send_message(bot_config['secret'], token, "Dit is geen taak-conversatie.")
        return jsonify({'status': 'ok' if success else 'failed'}), 200

    if message_content.strip().lower() == '/status':
        task_bot = get_task_bot_by_token(token)
        if task_bot:
            status_msg = f"""**Taak Status**

**Taak:** {task_bot['card_title']}
**Status:** {task_bot['status']}
**Card ID:** {task_bot['card_id']}
**Aangemaakt:** {task_bot['created_at']}
{f"**Afgerond:** {task_bot['completed_at']}" if task_bot.get('completed_at') else ""}

Typ /done om deze taak af te ronden."""
            success = send_message(bot_config['secret'], token, status_msg)
        else:
            success = send_message(bot_config['secret'], token, "Dit is geen taak-conversatie.")
        return jsonify({'status': 'ok' if success else 'failed'}), 200

    # Check for transcription request
    if message_content.strip().lower() == '/transcribe':
        # Check if this is a reply to an audio message or if there's a file attached
        file_info = extract_file_info(data, message_parameters)

        if file_info and file_info.get('url'):
            send_message(bot_config['secret'], token, f"Transcriberen van {file_info.get('name', 'audio')}...")

            # Download and transcribe
            local_path = download_nextcloud_file(file_info['url'], bot_config.get('nextcloud_user'), bot_config.get('nextcloud_password'))
            if local_path:
                transcription = transcribe_audio(local_path)
                if transcription:
                    response = f"**Transcriptie:**\n\n{transcription}"
                else:
                    response = "Kon het audio bestand niet transcriberen. Probeer een ander formaat."
            else:
                response = "Kon het audio bestand niet downloaden."

            success = send_message(bot_config['secret'], token, response, reply_to)
            return jsonify({'status': 'ok' if success else 'failed'}), 200
        else:
            success = send_message(bot_config['secret'], token, "Geen audio bestand gevonden. Stuur eerst een audio opname en reply dan met /transcribe")
            return jsonify({'status': 'ok' if success else 'failed'}), 200

    # Check for natural language completion intent in task conversations
    task_bot = get_task_bot_by_token(token)
    if task_bot and task_bot.get('status') == 'active':
        completion_intent = detect_completion_intent(message_content)
        if completion_intent == 'complete':
            # Explicit completion - complete immediately
            if complete_task(token, bot_config, task_bot):
                send_message(bot_config['secret'], token,
                    f"✅ **Taak afgerond!**\n\nDe taak \"{task_bot['card_title']}\" is gemarkeerd als voltooid en verplaatst naar Klaar.\n\n*Deze conversatie wordt over 3 seconden gesloten...*")

                import time
                time.sleep(3)
                close_conversation(token, bot_config['nextcloud_user'], bot_config['nextcloud_password'])
            else:
                send_message(bot_config['secret'], token, "Fout bij afronden van de taak.")
            return jsonify({'status': 'ok'}), 200
        elif completion_intent == 'confirm':
            # Ask for confirmation
            send_message(bot_config['secret'], token,
                f"Wil je de taak \"{task_bot['card_title']}\" afronden?\n\nTyp **ja** of **/done** om te bevestigen, of stel nog een vraag als je verder wilt werken.")
            add_to_history(token, 'user', actor_name, message_content)
            add_to_history(token, 'assistant', 'Claude', f"Vraag om bevestiging voor afronden taak")
            return jsonify({'status': 'ok'}), 200

    # Handle confirmation response "ja" for task completion
    if task_bot and message_content.strip().lower() in ['ja', 'yes', 'ok', 'oké', 'bevestig', 'akkoord']:
        # Check if last message was a completion confirmation request
        with history_lock:
            history = conversation_history.get(token, [])
            if history and 'bevestiging voor afronden' in history[-1].get('content', ''):
                if complete_task(token, bot_config, task_bot):
                    send_message(bot_config['secret'], token,
                        f"✅ **Taak afgerond!**\n\nDe taak \"{task_bot['card_title']}\" is gemarkeerd als voltooid en verplaatst naar Klaar.\n\n*Deze conversatie wordt over 3 seconden gesloten...*")

                    import time
                    time.sleep(3)
                    close_conversation(token, bot_config['nextcloud_user'], bot_config['nextcloud_password'])
                else:
                    send_message(bot_config['secret'], token, "Fout bij afronden van de taak.")
                return jsonify({'status': 'ok'}), 200

    # Check if message contains an audio file - auto transcribe
    file_info = extract_file_info(data, message_parameters)
    print(f"[{user}] File info extracted: {file_info}")

    if file_info and file_info.get('type') == 'audio' and file_info.get('url'):
        print(f"[{user}] Auto-transcribing audio: {file_info['url']}")
        send_message(bot_config['secret'], token, f"Audio gedetecteerd ({file_info.get('name', 'audio')}). Transcriberen...")

        local_path = download_nextcloud_file(file_info['url'], bot_config.get('nextcloud_user'), bot_config.get('nextcloud_password'))
        if local_path:
            transcription = transcribe_audio(local_path)
            if transcription:
                # Send transcription and also process with Claude
                send_message(bot_config['secret'], token, f"**Transcriptie:**\n{transcription}")
                message_content = f"[Audio transcriptie]: {transcription}"
            else:
                send_message(bot_config['secret'], token, "Transcriptie mislukt.")
                return jsonify({'status': 'failed'}), 200
        else:
            send_message(bot_config['secret'], token, "Kon audio niet downloaden.")
            return jsonify({'status': 'failed'}), 200

    # Add user message to history
    add_to_history(token, 'user', actor_name, message_content)

    # Log user message as comment on Deck card (max 1000 chars for Deck)
    if task_bot and task_bot.get('board_id') and task_bot.get('card_id'):
        # Account for prefix "**name:** " and "..."
        prefix_len = len(f"**{actor_name}:** ") + 3
        max_msg_len = 1000 - prefix_len
        truncated_msg = message_content[:max_msg_len] + '...' if len(message_content) > max_msg_len else message_content
        add_comment_to_deck_card(
            bot_config,
            task_bot['board_id'],
            task_bot['stack_id'],
            task_bot['card_id'],
            f"**{actor_name}:** {truncated_msg}"
        )

    # Build prompt with history context
    history_context = get_history_context(token)

    if history_context:
        full_prompt = f"""Hieronder staat de gespreksgeschiedenis gevolgd door een nieuw bericht.
Houd rekening met de context van eerdere berichten bij je antwoord.

{history_context}
[{actor_name}]: {message_content}"""
    else:
        full_prompt = f"[{actor_name}]: {message_content}"

    # Check if this is a task-specific conversation
    task_context = get_task_bot_by_token(token)
    if task_context:
        print(f"[{user}] Task conversation detected: {task_context['card_title']}")
        send_message(bot_config['secret'], token, f"Bezig met taak: {task_context['card_title']}...")
    else:
        send_message(bot_config['secret'], token, "Impertio AI is aan het nadenken...")

    # Call Claude with user-specific configuration (and task context if available)
    response = call_claude(
        full_prompt,
        bot_config['working_dir'],
        bot_config['config_dir'],
        user.capitalize(),
        bot_config['erpnext_user'],
        task_context=task_context
    )
    print(f"[{user}] Claude response length: {len(response)}")

    # Add assistant response to history
    add_to_history(token, 'assistant', 'Claude', response)

    # Log Claude response as comment on Deck card
    if task_context and task_context.get('board_id') and task_context.get('card_id'):
        # Truncate response for comment if too long (Deck has 1000 char limit)
        # Account for prefix "**Claude AI:** " (15 chars) + "..." (3 chars)
        max_comment_len = 1000 - 18
        comment_response = response[:max_comment_len] + '...' if len(response) > max_comment_len else response
        add_comment_to_deck_card(
            bot_config,
            task_context['board_id'],
            task_context['stack_id'],
            task_context['card_id'],
            f"**Claude AI:** {comment_response}"
        )

    # Send response
    success = send_message(bot_config['secret'], token, response, reply_to)

    return jsonify({'status': 'ok' if success else 'failed'}), 200


# Routes for each user's bot
@app.route('/webhook', methods=['POST'])
def webhook_maarten():
    """Maarten's bot (original)"""
    return handle_webhook('maarten')


@app.route('/webhook/albert', methods=['POST'])
def webhook_albert():
    """Albert's bot"""
    return handle_webhook('albert')


@app.route('/webhook/freek', methods=['POST'])
def webhook_freek():
    """Freek's bot"""
    return handle_webhook('freek')


@app.route('/health', methods=['GET'])
def health():
    with history_lock:
        conversation_count = len(conversation_history)
        total_messages = sum(len(msgs) for msgs in conversation_history.values())

    return jsonify({
        'status': 'healthy',
        'bots': list(BOTS.keys()),
        'erpnext_users': {k: v['erpnext_user'] for k, v in BOTS.items()},
        'conversations': conversation_count,
        'total_messages': total_messages
    }), 200


# Load history on startup
load_history()

if __name__ == '__main__':
    print(f"Starting multi-user bot with Claude at {CLAUDE_PATH}")
    print(f"Configured bots: {', '.join(BOTS.keys())}")
    for name, config in BOTS.items():
        print(f"  - {name}: ERPNext={config['erpnext_user']}, Config={config['config_dir']}")
    print(f"History file: {HISTORY_FILE}")
    app.run(host='0.0.0.0', port=8085)
