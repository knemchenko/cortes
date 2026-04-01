from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright
import os
import re
import json

app = FastAPI(title="Threads Video Extractor Service")

COOKIES_FILE = "cookies.json"

class URLRequest(BaseModel):
    url: str

async def extract_threads_video(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Using a reliable User-Agent
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        
        # Load cookies if they exist
        if os.path.exists(COOKIES_FILE):
            try:
                with open(COOKIES_FILE, "r") as f:
                    cookies = json.load(f)
                    
                    # Some exported cookies might need URL formatting or domain adjustments
                    valid_cookies = []
                    for cookie in cookies:
                        # Playwright expects specific dictionary keys for cookies (name, value, domain, path)
                        c = {
                            "name": cookie.get("name", cookie.get("Name", "")),
                            "value": cookie.get("value", cookie.get("Value", "")),
                            "domain": cookie.get("domain", cookie.get("Domain", ".threads.net")),
                            "path": cookie.get("path", cookie.get("Path", "/")),
                        }
                        if c["name"] and c["value"]:
                            valid_cookies.append(c)
                            
                    if valid_cookies:
                        await context.add_cookies(valid_cookies)
                        print(f"Loaded {len(valid_cookies)} cookies.")
            except Exception as e:
                print(f"Failed to load cookies: {e}")

        page = await context.new_page()
        
        print(f"[Playwright] Navigating to: {url}")
        try:
            await page.goto(url, wait_until="networkidle", timeout=15000)
        except Exception as e:
            print(f"[Playwright] Navigation finished or timed out: {e}")

        content = await page.content()
        await browser.close()
        
        media_results = []
        
        # Parse GraphQL state nested in scripts
        scripts = re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', content)
        for s in scripts:
            if '"thread_items"' in s:
                try:
                    data = json.loads(s)
                    def find_thread_items(obj):
                        if isinstance(obj, dict):
                            if "thread_items" in obj:
                                return obj["thread_items"]
                            for k, v in obj.items():
                                res = find_thread_items(v)
                                if res: return res
                        elif isinstance(obj, list):
                            for item in obj:
                                res = find_thread_items(item)
                                if res: return res
                        return None
                        
                    items = find_thread_items(data)
                    if items:
                        # Grab the first valid post object in the items
                        for item in items:
                            post = item.get("post", {})
                            if not post: continue
                            
                            found_any = False
                            
                            # Parse caption
                            if "caption" in post and isinstance(post["caption"], dict) and "text" in post["caption"]:
                                current_caption = post["caption"]["text"]
                                # We only want the caption of the MAIN targeted media/post, not comments.
                                if not any(m.get("is_caption") for m in media_results):
                                    media_results.append({"type": "caption", "text": current_caption, "is_caption": True})

                            if "carousel_media" in post and post["carousel_media"]:
                                for c in post["carousel_media"]:
                                    if "video_versions" in c and c["video_versions"]:
                                        media_results.append({"type": "video", "url": c["video_versions"][0]["url"]})
                                        found_any = True
                                    elif "image_versions2" in c and c["image_versions2"]:
                                        candidates = c["image_versions2"].get("candidates", [])
                                        if candidates:
                                            media_results.append({"type": "image", "url": candidates[0]["url"]})
                                            found_any = True
                            else:
                                if "video_versions" in post and post["video_versions"]:
                                    media_results.append({"type": "video", "url": post["video_versions"][0]["url"]})
                                    found_any = True
                                elif "image_versions2" in post and post["image_versions2"]:
                                    candidates = post["image_versions2"].get("candidates", [])
                                    if candidates:
                                        media_results.append({"type": "image", "url": candidates[0]["url"]})
                                        found_any = True
                                        
                            if found_any:
                                # Prioritize cleaning URLs
                                for m in media_results:
                                    m["url"] = m["url"].replace("\\/", "/")
                                break # Found the main post media, stop searching other thread items
                    if media_results:
                        break
                except Exception as e:
                    print(f"Error parsing script: {e}")

        # Fallback if standard parsing fails
        if not media_results:
            matches = re.findall(r'"video_versions":\[(.*?)\]', content)
            if matches:
                url_match = re.search(r'"url":"([^"]+)"', matches[0])
                if url_match:
                    media_results.append({"type": "video", "url": url_match.group(1).replace("\\/", "/")})
            else:
                match_og = re.search(r'og:video"[^>]+content="([^"]+)"', content)
                if match_og:
                    media_results.append({"type": "video", "url": match_og.group(1).replace("&amp;", "&")})
                else:
                    match_og_img = re.search(r'og:image"[^>]+content="([^"]+)"', content)
                    if match_og_img:
                        media_results.append({"type": "image", "url": match_og_img.group(1).replace("&amp;", "&")})
                        
            # Fallback for caption
            if not any(m["type"] == "caption" for m in media_results):
                match_desc = re.search(r'og:description"[^>]+content="([^"]+)"', content)
                if match_desc:
                    import html
                    media_results.append({"type": "caption", "text": html.unescape(match_desc.group(1)), "is_caption": True})

        if media_results:
            return media_results

        return None

@app.post("/api/extract_threads")
async def api_extract_threads(req: URLRequest):
    media = await extract_threads_video(req.url)
    if media:
        # Backward compatibility for single video
        vid_urls = [m["url"] for m in media if m["type"] == "video"]
        caption_items = [m["text"] for m in media if m["type"] == "caption"]
        
        # Only return actual media (video/images) in the 'media' array, so we don't break existing client loop
        actual_media = [m for m in media if m["type"] in ["video", "image"]]
        caption_text = caption_items[0] if caption_items else None

        return {
            "status": "success", 
            "media": actual_media,
            "video_url": vid_urls[0] if vid_urls else None,
            "caption": caption_text
        }
    else:
        raise HTTPException(status_code=404, detail="Media not found in Threads post")

async def extract_tiktok_video(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()
        
        print(f"[Playwright] Navigating to TikTok: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            # wait a bit for video to render
            await page.wait_for_timeout(3000)
        except Exception as e:
            print(f"[Playwright] Navigation finished or timed out: {e}")

        # Try to find a video tag source
        try:
            video_src = await page.evaluate('''() => {
                const video = document.querySelector('video');
                if (video && video.src) return video.src;
                const source = document.querySelector('video source');
                if (source && source.src) return source.src;
                return null;
            }''')
        except Exception as e:
            video_src = None
            
        print(f"Direct video element src: {video_src}")

        # If not found directly, parse universal data
        if not video_src:
            content = await page.content()
            try:
                import re
                import json
                match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">(.*?)</script>', content)
                if match:
                    data = json.loads(match.group(1))
                    def find_play_addr(obj):
                        if isinstance(obj, dict):
                            if "playAddr" in obj and obj["playAddr"]:
                                return obj["playAddr"]
                            for k, v in obj.items():
                                res = find_play_addr(v)
                                if res: return res
                        elif isinstance(obj, list):
                            for item in obj:
                                res = find_play_addr(item)
                                if res: return res
                        return None
                    
                    found_url = find_play_addr(data)
                    if found_url:
                        video_src = found_url
                        print("Found video url via __UNIVERSAL_DATA_FOR_REHYDRATION__")
            except Exception as e:
                print(f"Error parsing tiktok universal data: {e}")

        # As a fallback, check for window.SIGI_STATE
        if not video_src:
            try:
                match = re.search(r'window\["SIGI_STATE"\]=(.*?);window', content)
                if match:
                    data = json.loads(match.group(1))
                    # simple regex scan as fallback inside the json
                    urls = re.findall(r'"playAddr":"(https?[^"]+)"', match.group(1))
                    if urls:
                        video_src = urls[0].encode().decode('unicode_escape')
                        print("Found video url via SIGI_STATE regex")
            except Exception as e:
                print(f"Error parsing SIGI_STATE: {e}")

        await browser.close()
        
        # Clean URL if it's encoded or has escaped slashes
        if video_src:
            video_src = video_src.replace("\\u002F", "/").replace("\\/", "/")
            return video_src
            
        return None

@app.post("/api/extract_tiktok")
async def api_extract_tiktok(req: URLRequest):
    video_url = await extract_tiktok_video(req.url)
    if video_url:
        return {
            "status": "success",
            "video_url": video_url
        }
    else:
        raise HTTPException(status_code=404, detail="Video not found in TikTok post")

async def extract_instagram_video(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        
        # Load cookies if they exist
        if os.path.exists(COOKIES_FILE):
            try:
                with open(COOKIES_FILE, "r") as f:
                    cookies = json.load(f)
                    valid_cookies = []
                    for cookie in cookies:
                        c = {
                            "name": cookie.get("name", cookie.get("Name", "")),
                            "value": cookie.get("value", cookie.get("Value", "")),
                            "domain": cookie.get("domain", cookie.get("Domain", ".instagram.com")),
                            "path": cookie.get("path", cookie.get("Path", "/")),
                        }
                        if c["name"] and c["value"]:
                            valid_cookies.append(c)
                    if valid_cookies:
                        await context.add_cookies(valid_cookies)
            except Exception as e:
                print(f"Failed to load cookies: {e}")

        page = await context.new_page()
        
        print(f"[Playwright] Navigating to Instagram: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            print(f"[Playwright] Navigation finished or timed out: {e}")

        # Try to find a video tag source
        try:
            video_src = await page.evaluate('''() => {
                const video = document.querySelector('video');
                if (video && video.src) return video.src;
                return null;
            }''')
        except Exception:
            video_src = None
            
        print(f"Direct video element src: {video_src}")

        if not video_src or video_src.startswith("blob:"):
            content = await page.content()
            # fallback to parsing JSON from scripts
            # like in Threads
            matches = re.findall(r'"video_versions":\[(.*?)\]', content)
            if matches:
                url_match = re.search(r'"url":"([^"]+)"', matches[0])
                if url_match:
                    video_src = url_match.group(1).replace("\\/", "/").replace("\\u0026", "&")
            else:
                match_og = re.search(r'og:video"[^>]+content="([^"]+)"', content)
                if match_og:
                    video_src = match_og.group(1).replace("&amp;", "&")

        await browser.close()
        
        if video_src:
            return video_src.replace("\\/", "/")
            
        return None

@app.post("/api/extract_instagram")
async def api_extract_instagram(req: URLRequest):
    video_url = await extract_instagram_video(req.url)
    if video_url:
        return {
            "status": "success",
            "video_url": video_url
        }
    else:
        raise HTTPException(status_code=404, detail="Video not found in Instagram post")

# To run: uvicorn main:app --host 127.0.0.1 --port 8001

