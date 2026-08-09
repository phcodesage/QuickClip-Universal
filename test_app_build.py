import unittest
import urllib.request
import json

BASE_URL = "http://127.0.0.1:5050"

class TestQuickClipBuild(unittest.TestCase):

    def test_home_page(self):
        req = urllib.request.Request(f"{BASE_URL}/")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode('utf-8')
            self.assertIn("QuickClip Universal", html)

    def test_style_css_font(self):
        req = urllib.request.Request(f"{BASE_URL}/static/css/style.css")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            css = resp.read().decode('utf-8')
            self.assertIn("Inter", css)

    def test_downloader_page(self):
        req = urllib.request.Request(f"{BASE_URL}/downloader")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode('utf-8')
            self.assertIn("Video & Audio Downloader", html)

    def test_audio_cutter_page(self):
        req = urllib.request.Request(f"{BASE_URL}/audio-cutter")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode('utf-8')
            self.assertIn("Audio Cutter & Trimmer", html)

    def test_video_info_api(self):
        data = json.dumps({"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}).encode('utf-8')
        req = urllib.request.Request(f"{BASE_URL}/video-info", data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            res_json = json.loads(resp.read().decode('utf-8'))
            self.assertIn("title", res_json)
            self.assertEqual(res_json.get("platform"), "youtube")

if __name__ == "__main__":
    unittest.main()
