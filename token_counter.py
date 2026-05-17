"""
CyberGraph Dataset Aggregator — Token Counter Script
===================================================
Scans processed folder, counts files/documents, estimates token counts, and presents a rich statistical summary.
"""

import os
import json
import re
import sys

# --- Windows stdout fix ---
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

PROCESSED_DIR = "data/processed"
JSON_PATH = os.path.join(PROCESSED_DIR, "dataset_documents.json")
TXT_PATH = os.path.join(PROCESSED_DIR, "consolidated_cybersecurity_corpus.txt")

def count_tokens():
    print(f"\n========================================================")
    print(f"  CYBERGRAPH DATASET AGGREGATOR — TOKEN COUNTER")
    print(f"========================================================\n")
    
    if not os.path.exists(JSON_PATH):
        print(f"[!] Processed dataset file {JSON_PATH} not found. Run cleaner.py first.")
        return
        
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        documents = json.load(f)
        
    doc_count = len(documents)
    total_chars = 0
    total_words = 0
    
    # Track statistics per source
    source_stats = {}
    tag_stats = {}
    relationship_count = 0
    
    for doc in documents:
        content = doc.get("content", "")
        total_chars += len(content)
        total_words += len(content.split())
        
        # Source counting
        src = doc.get("source", "Unknown")
        source_stats[src] = source_stats.get(src, 0) + 1
        
        # Tags tracking
        for t in doc.get("tags", []):
            tag_stats[t] = tag_stats.get(t, 0) + 1
            
        # Relationship counting (e.g. searching lines starting with '-' in relationship sections)
        rels = re.findall(r'-\s+(?:USES|USED BY|EXPLOITS)', content)
        relationship_count += len(rels)

    # Token heuristic methods
    # Method 1: Heuristic based on 4.0 characters per token (typical English threat intel prose)
    tokens_char_ratio = int(total_chars / 4.0)
    
    # Method 2: Heuristic based on 1.3 tokens per word (more precise for acronym-heavy technical prose)
    tokens_word_ratio = int(total_words * 1.3)
    
    # Take conservative average
    final_token_estimate = int((tokens_char_ratio + tokens_word_ratio) / 2)

    # Output formatted report
    print(f"[+] Document Audit:")
    print(f"    - Total Documents: {doc_count:,}")
    print(f"    - Total Characters: {total_chars:,}")
    print(f"    - Total Word Count: {total_words:,}")
    print(f"    - Relationships Documented: {relationship_count:,}\n")
    
    print(f"[+] Token Estimation:")
    print(f"    - Char-ratio Heuristic (chars/4): {tokens_char_ratio:,} tokens")
    print(f"    - Word-ratio Heuristic (words*1.3): {tokens_word_ratio:,} tokens")
    print(f"    - Combined Estimate: {final_token_estimate:,} tokens")
    
    if final_token_estimate >= 2000000:
        print(f"\n🎉 SUCCESS: Consolidated dataset exceeds 2,000,000 token goal! ({final_token_estimate / 1000000:.2f}M tokens total)\n")
    else:
        print(f"\n⚠️ WARNING: Consolidated dataset has {final_token_estimate:,} tokens, short of the 2M goal.\n")
        
    print(f"[+] Sources Breakdown:")
    for src, count in source_stats.items():
        print(f"    - {src}: {count:,} documents")
        
    print(f"\n[+] Top Entity Tags in Dataset:")
    sorted_tags = sorted(tag_stats.items(), key=lambda x: x[1], reverse=True)[:15]
    for tag, count in sorted_tags:
        print(f"    - [{tag}]: {count:,} instances")

if __name__ == "__main__":
    count_tokens()
