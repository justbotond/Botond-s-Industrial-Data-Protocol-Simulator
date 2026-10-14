"""Capture README screenshots of a running simulator UI with headless Edge/Chrome (DevTools protocol).

    python tools/screenshots.py --url http://127.0.0.1:8088 --out docs/screenshots

Start the simulator first and press "Start all enabled" so the pages show live data.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import itertools
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import aiohttp

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]

# small helpers injected into the page
HELPERS = """
window.__click = async (sel, text) => {  // waits up to 15 s for the element (e.g. after a slow connect)
  for (let i = 0; i < 150; i++) {
    // table cells: exact name ignoring a leading folder icon; everything else: label prefix
    const el = [...document.querySelectorAll(sel)].find(e => {
      const t = e.textContent.trim();
      return sel === 'td' ? t.replace(/^[^\w]+/u, '') === text : t.startsWith(text);
    });
    if (el) { el.click(); return true; }
    await new Promise(r => setTimeout(r, 100));
  }
  throw new Error('not found: ' + sel + ' ' + text);
};
"""

# (file name, hash page, list of actions); action = ("click", selector, text) | ("wait", seconds)
SHOTS = [
    ("01-overview", "overview", [("wait", 2)]),
    ("02-modbus-servers", "modbus", [("wait", 1.5)]),
    ("03-modbus-test-client", "modbus", [("click", ".tab", "Test client"), ("wait", 0.5),
                                         ("click", "button", "Read"), ("wait", 2.5)]),
    ("04-opcua-browse", "opcua", [("click", ".tab", "Test client"), ("wait", 0.5),
                                  ("click", "button", "Connect & browse"), ("wait", 1.5),
                                  ("click", "td", "opcua1"), ("wait", 1),
                                  ("click", "td", "Random"), ("wait", 1),
                                  ("click", "td", "Random_0-999"), ("wait", 2)]),
    ("05-mqtt-test-client", "mqtt", [("click", ".tab", "Test client"), ("wait", 0.5),
                                     ("click", "button", "Connect & subscribe"), ("wait", 3)]),
    ("06-bacnet-test-client", "bacnet", [("click", ".tab", "Test client"), ("wait", 0.5),
                                         ("click", "button", "Read present-value"), ("wait", 3)]),
    ("07-opcda-server", "opcda", [("wait", 1.5)]),
    ("08-modbus-guide", "modbus", [("click", ".tab", "Guide"), ("wait", 1)]),
    ("09-logs", "logs", [("wait", 2.5)]),
]


class Page:
    def __init__(self, ws):
        self.ws, self.ids = ws, itertools.count(1)

    async def send(self, method: str, **params):
        i = next(self.ids)
        await self.ws.send_json({"id": i, "method": method, "params": params})
        while True:
            msg = await self.ws.receive_json()
            if msg.get("id") == i:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr: str):
        r = await self.send("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", "JS error"))
        return r.get("result", {}).get("value")


async def run(url: str, out: Path, width: int, height: int, theme: str) -> None:
    exe = next((b for b in BROWSERS if Path(b).exists()), None) or shutil.which("msedge") or shutil.which("chrome")
    if not exe:
        raise SystemExit("Edge or Chrome is required")
    out.mkdir(parents=True, exist_ok=True)
    port = 9333
    with tempfile.TemporaryDirectory() as profile:
        proc = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                                 "--no-first-run", "--hide-scrollbars", f"--window-size={width},{height}",
                                 "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            async with aiohttp.ClientSession() as http:
                for _ in range(50):
                    try:
                        async with http.get(f"http://127.0.0.1:{port}/json") as r:
                            targets = await r.json(content_type=None)
                        break
                    except aiohttp.ClientError:
                        await asyncio.sleep(0.2)
                target = next(t for t in targets if t["type"] == "page")
                async with http.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                    page = Page(ws)
                    await page.send("Emulation.setDeviceMetricsOverride", width=width, height=height,
                                    deviceScaleFactor=1, mobile=False)
                    await page.send("Emulation.setEmulatedMedia",
                                    features=[{"name": "prefers-color-scheme", "value": theme}])
                    for name, hash_, actions in SHOTS:
                        await page.send("Page.navigate", url=f"{url}/?shot={name}#{hash_}")
                        for _ in range(100):  # wait until the UI has booted (host info line is filled in)
                            await asyncio.sleep(0.2)
                            if await page.js("(document.querySelector('#hostinfo')?.textContent || '')"
                                             ".includes('·')"):
                                break
                        await asyncio.sleep(0.8)
                        await page.js(HELPERS)
                        # keep the PC name, LAN IP and user path out of published screenshots
                        await page.js("document.querySelector('#hostinfo').textContent = "
                                      "document.querySelector('#hostinfo').textContent.split(' · ')[0]")
                        for a in actions:
                            if a[0] == "wait":
                                await asyncio.sleep(a[1])
                            else:
                                await page.js(f"window.__click({json.dumps(a[1])}, {json.dumps(a[2])})")
                        shot = await page.send("Page.captureScreenshot", format="png")
                        path = out / f"{name}.png"
                        path.write_bytes(base64.b64decode(shot["data"]))
                        print("saved", path)
        finally:
            proc.terminate()
            proc.wait(10)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8088")
    ap.add_argument("--out", default="docs/screenshots")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--theme", choices=["dark", "light"], default="dark")
    args = ap.parse_args()
    asyncio.run(run(args.url.rstrip("/"), Path(args.out), args.width, args.height, args.theme))


if __name__ == "__main__":
    main()
