import requests
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlparse
from typing import Optional, List, Dict, Any, Tuple
import re


class WebDAVClient:
    def __init__(
        self,
        base_url: str,
        username: str = '',
        password: str = '',
        download_base_url: str = '',
    ):
        self.base_url = base_url.rstrip('/')
        # download_base_url: separate host/URL for actual file transfers.
        # Useful when browsing goes through an external address but downloads
        # should hit a local IP directly (e.g. NAS LAN vs. internet domain).
        self.download_base_url = (
            download_base_url.rstrip('/') if download_base_url.strip() else self.base_url
        )
        self.auth = (username, password) if username else None
        self.session = requests.Session()
        if self.auth:
            self.session.auth = self.auth
        self.session.headers['User-Agent'] = 'WebDAVDownloader/1.0'

    def test_connection(self) -> Tuple[bool, str]:
        try:
            resp = self.session.request('OPTIONS', self.base_url + '/', timeout=10)
            if resp.status_code < 400:
                return True, 'Connected successfully'
            # Try PROPFIND on root as fallback
            resp2 = self.session.request(
                'PROPFIND', self.base_url + '/',
                headers={'Depth': '0', 'Content-Type': 'application/xml'},
                timeout=10
            )
            if resp2.status_code < 400:
                return True, 'Connected successfully'
            return False, f'Server returned HTTP {resp.status_code}'
        except requests.exceptions.ConnectionError:
            return False, 'Connection refused or host unreachable'
        except requests.exceptions.Timeout:
            return False, 'Connection timed out'
        except Exception as e:
            return False, str(e)

    def list_directory(self, path: str = '/') -> List[Dict[str, Any]]:
        url = self._path_to_url(path)
        body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<propfind xmlns="DAV:"><prop>'
            b'<displayname/><getcontentlength/><getcontenttype/>'
            b'<getlastmodified/><resourcetype/>'
            b'</prop></propfind>'
        )
        resp = self.session.request(
            'PROPFIND', url,
            headers={'Depth': '1', 'Content-Type': 'application/xml; charset=utf-8'},
            data=body,
            timeout=30,
        )
        resp.raise_for_status()
        return self._parse_propfind(resp.content, path)

    def get_download_url(self, path: str) -> str:
        segments = path.split('/')
        encoded = '/'.join(quote(s, safe='') for s in segments)
        return self.download_base_url + encoded

    def get_auth(self) -> Optional[tuple]:
        return self.auth

    def _path_to_url(self, path: str) -> str:
        segments = path.split('/')
        encoded = '/'.join(quote(s, safe='') for s in segments)
        return self.base_url + encoded

    def _parse_propfind(self, content: bytes, current_path: str) -> List[Dict[str, Any]]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            # Some servers emit non-standard XML; strip namespaces and retry
            text = content.decode('utf-8', errors='replace')
            text = re.sub(r'xmlns(?::\w+)?="[^"]*"', '', text)
            root = ET.fromstring(text.encode())

        ns = self._detect_namespace(root)
        base_server_path = urlparse(self.base_url).path.rstrip('/')
        items = []

        for response_elem in root.iter(f'{ns}response'):
            href_elem = response_elem.find(f'{ns}href')
            if href_elem is None or not href_elem.text:
                continue

            href_raw = href_elem.text.strip()
            href_decoded = unquote(href_raw)

            # Convert full URL hrefs to path-only
            if href_decoded.startswith('http'):
                href_path = urlparse(href_decoded).path
            else:
                href_path = href_decoded

            # Remove the WebDAV base path prefix to get a relative path
            if base_server_path and href_path.startswith(base_server_path):
                rel_path = href_path[len(base_server_path):]
                if rel_path and not rel_path.startswith('/'):
                    rel_path = '/' + rel_path
                if not rel_path:
                    rel_path = '/'
            else:
                rel_path = href_path

            # Find props from the 200-OK propstat
            prop = None
            for propstat in response_elem.findall(f'{ns}propstat'):
                status_elem = propstat.find(f'{ns}status')
                if status_elem is not None and '200' in (status_elem.text or ''):
                    prop = propstat.find(f'{ns}prop')
                    break
            if prop is None:
                first_ps = response_elem.find(f'{ns}propstat')
                if first_ps is not None:
                    prop = first_ps.find(f'{ns}prop')
            if prop is None:
                continue

            # Determine if this is a directory
            resource_type = prop.find(f'{ns}resourcetype')
            is_dir = (
                resource_type is not None
                and resource_type.find(f'{ns}collection') is not None
            )

            # Skip the current-directory entry itself
            if rel_path.rstrip('/') == current_path.rstrip('/'):
                continue

            # Display name
            dn_elem = prop.find(f'{ns}displayname')
            name = (dn_elem.text or '').strip() if dn_elem is not None else ''
            if not name:
                name = unquote(href_raw.rstrip('/').split('/')[-1])
            if not name:
                continue

            # File size
            cl_elem = prop.find(f'{ns}getcontentlength')
            try:
                size = int(cl_elem.text) if cl_elem is not None and cl_elem.text else 0
            except ValueError:
                size = 0

            # MIME type
            ct_elem = prop.find(f'{ns}getcontenttype')
            content_type = ''
            if ct_elem is not None and ct_elem.text:
                content_type = ct_elem.text.split(';')[0].strip()

            # Last-modified date
            lm_elem = prop.find(f'{ns}getlastmodified')
            last_modified = (lm_elem.text or '') if lm_elem is not None else ''

            items.append({
                'name': name,
                'path': rel_path,
                'is_dir': is_dir,
                'size': size,
                'content_type': content_type,
                'last_modified': last_modified,
            })

        # Directories first, then alphabetical
        items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        return items

    @staticmethod
    def _detect_namespace(root) -> str:
        tag = root.tag
        if tag.startswith('{'):
            return '{' + tag[1:tag.index('}')] + '}'
        return '{DAV:}'
