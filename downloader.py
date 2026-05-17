"""
CyberGraph Dataset Aggregator — Downloader Script
================================================
Downloads public cybersecurity data feeds automatically from MITRE ATT&CK, CISA KEV, and CISA RSS feeds.
"""

import os
import urllib.request
import ssl

RAW_DIR = "data/raw"
FEEDS = {
    "mitre_attack.json": "https://raw.githubusercontent.com/mitre/cti/master/enterprise-attack/enterprise-attack.json",
    "cisa_kev.json": "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
    "cisa_advisories.xml": "https://www.cisa.gov/cybersecurity-advisories/all.xml"
}

def download_feeds():
    os.makedirs(RAW_DIR, exist_ok=True)
    print(f"\n========================================================")
    print(f"  CYBERGRAPH DATASET AGGREGATOR — DOWNLOADER")
    print(f"========================================================\n")
    
    # Bypass SSL verification if running behind corporate proxies or restricted environments
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    for fname, url in FEEDS.items():
        dest = os.path.join(RAW_DIR, fname)
        print(f"[*] Downloading {fname} from {url}...")
        try:
            # Simple streaming download with progress
            with urllib.request.urlopen(url, context=ctx) as response, open(dest, "wb") as out_file:
                meta = response.info()
                file_size = int(meta.get("Content-Length", 0))
                print(f"    Size: {file_size / (1024*1024):.2f} MB")
                
                block_size = 8192
                downloaded = 0
                while True:
                    buffer = response.read(block_size)
                    if not buffer:
                        break
                    downloaded += len(buffer)
                    out_file.write(buffer)
                    if file_size:
                        percent = downloaded * 100 / file_size
                        print(f"    Progress: {percent:.1f}% ({downloaded / (1024*1024):.2f} MB)", end="\r")
            print(f"\n[+] Successfully saved {fname} to {dest}\n")
        except Exception as e:
            print(f"\n[!] Error downloading {fname}: {e}\n")

if __name__ == "__main__":
    download_feeds()
