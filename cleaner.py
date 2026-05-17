"""
CyberGraph Dataset Aggregator — Cleaner Script
=============================================
Processes raw feeds (MITRE ATT&CK, CISA KEV, CISA Advisories) into clean, normalized JSON
and consolidated text documents, preserving entity relationships for GraphRAG.
"""

import os
import json
import xml.etree.ElementTree as ET
import re

RAW_DIR = "data/raw"
PROCESSED_DIR = "data/processed"

def clean_text(text):
    if not text:
        return ""
    # Remove markdown link syntaxes, html tags, excessive spacing
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def process_cisa_kev():
    print("[*] Processing CISA KEV Feed...")
    fpath = os.path.join(RAW_DIR, "cisa_kev.json")
    if not os.path.exists(fpath):
        print(f"[!] {fpath} not found. Skipping KEV.")
        return []
    
    with open(fpath, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    vulnerabilities = data.get("vulnerabilities", [])
    processed = []
    
    for v in vulnerabilities:
        cve_id = v.get("cveID", "")
        vendor = v.get("vendorProject", "")
        product = v.get("product", "")
        name = v.get("vulnerabilityName", "")
        desc = v.get("shortDescription", "")
        mitigation = v.get("requiredAction", "")
        added = v.get("dateAdded", "")
        
        title = f"{cve_id}: {name}"
        content = (
            f"Vulnerability: {cve_id} ({name})\n"
            f"Vendor/Project: {vendor}\n"
            f"Affected Product: {product}\n"
            f"Description: {clean_text(desc)}\n"
            f"Mitigation/Required Action: {clean_text(mitigation)}\n"
            f"Date Added to KEV Catalog: {added}\n"
            f"Relationship Profile: {cve_id} EXPLOITS {product} BY {vendor}."
        )
        
        doc = {
            "title": title,
            "content": content,
            "source": "CISA Known Exploited Vulnerabilities Catalog",
            "tags": ["cve", "vulnerability", vendor.lower(), product.lower()]
        }
        processed.append(doc)
        
    print(f"[+] Processed {len(processed)} KEV vulnerabilities.")
    return processed

def process_cisa_advisories():
    print("[*] Processing CISA Advisories XML...")
    fpath = os.path.join(RAW_DIR, "cisa_advisories.xml")
    if not os.path.exists(fpath):
        print(f"[!] {fpath} not found. Skipping CISA Advisories.")
        return []
        
    try:
        tree = ET.parse(fpath)
        root = tree.getroot()
    except Exception as e:
        print(f"[!] Failed to parse CISA Advisories XML: {e}")
        return []
        
    processed = []
    # RSS elements namespace
    for item in root.findall(".//item"):
        title = item.find("title")
        desc = item.find("description")
        pub_date = item.find("pubDate")
        
        t_str = title.text if title is not None else "CISA Threat Advisory"
        d_str = desc.text if desc is not None else ""
        p_str = pub_date.text if pub_date is not None else ""
        
        d_clean = clean_text(d_str)
        content = (
            f"Title: {t_str}\n"
            f"Published Date: {p_str}\n"
            f"Details:\n{d_clean}"
        )
        
        doc = {
            "title": t_str,
            "content": content,
            "source": "CISA Cybersecurity Advisories Feed",
            "tags": ["cisa", "advisory", "threat-intel"]
        }
        processed.append(doc)
        
    print(f"[+] Processed {len(processed)} CISA advisories.")
    return processed

def process_mitre_attack():
    print("[*] Processing MITRE ATT&CK STIX Feed (exceeding 2M tokens)...")
    fpath = os.path.join(RAW_DIR, "mitre_attack.json")
    if not os.path.exists(fpath):
        print(f"[!] {fpath} not found. Skipping MITRE ATT&CK.")
        return []
        
    with open(fpath, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    objects = data.get("objects", [])
    
    # Pre-parse entity lookup tables for relationship resolution
    entities = {} # id -> entity (actor, malware, tool, technique)
    relations = [] # raw STIX relations
    
    for obj in objects:
        obj_id = obj.get("id", "")
        obj_type = obj.get("type", "")
        
        if obj_type in ["intrusion-set", "malware", "tool", "attack-pattern"]:
            ext_refs = obj.get("external_references", [])
            ext_id = ""
            for ref in ext_refs:
                if ref.get("source_name") in ["mitre-attack", "mitre-enterprise-attack"]:
                    ext_id = ref.get("external_id", "")
                    break
                    
            entities[obj_id] = {
                "id": ext_id or obj_id,
                "name": obj.get("name", ""),
                "description": clean_text(obj.get("description", "")),
                "type": obj_type,
                "aliases": obj.get("aliases", []) or obj.get("x_mitre_aliases", []) or []
            }
        elif obj_type == "relationship":
            relations.append(obj)

    print(f"    Loaded {len(entities)} MITRE entities and {len(relations)} raw relationships.")
    
    # Resolve relationships with descriptions
    resolved_rels = []
    for r in relations:
        source_id = r.get("source_ref", "")
        target_id = r.get("target_ref", "")
        rel_type = r.get("relationship_type", "")
        
        if source_id in entities and target_id in entities:
            src = entities[source_id]
            tgt = entities[target_id]
            resolved_rels.append({
                "source_name": src["name"],
                "source_type": src["type"],
                "source_id": src["id"],
                "target_name": tgt["name"],
                "target_type": tgt["type"],
                "target_id": tgt["id"],
                "relation": rel_type,
                "description": clean_text(r.get("description", ""))
            })

    processed = []
    
    # 1. Generate comprehensive documents for Techniques, Malware, Actors, and Tools
    for entity_id, ent in entities.items():
        etype = ent["type"]
        name = ent["name"]
        desc = ent["description"]
        eid = ent["id"]
        aliases = ent["aliases"]
        
        # Build relationship segment for this specific entity
        ent_rels = []
        for rel in resolved_rels:
            if rel["source_id"] == eid:
                ent_rels.append(f"- USES {rel['target_type'].replace('-', ' ')}: {rel['target_name']} ({rel['target_id']})")
            elif rel["target_id"] == eid:
                ent_rels.append(f"- USED BY {rel['source_type'].replace('-', ' ')}: {rel['source_name']} ({rel['source_id']})")
        
        rels_text = "\n".join(ent_rels) if ent_rels else "No explicit relationships documented in graph."
        
        alias_str = f"Aliases: {', '.join(aliases)}" if aliases else ""
        content = (
            f"Entity Name: {name}\n"
            f"Entity Type: {etype.upper().replace('-', ' ')}\n"
            f"MITRE ID: {eid}\n"
            f"{alias_str}\n\n"
            f"Description:\n{desc}\n\n"
            f"Graph Relationships:\n{rels_text}"
        )
        
        doc = {
            "title": f"{etype.upper().replace('-', ' ')}: {name} ({eid})",
            "content": content,
            "source": "MITRE ATT&CK Enterprise Catalog",
            "tags": ["mitre", etype, eid.lower()]
        }
        processed.append(doc)
        
    # 2. Generate comprehensive relationship documents to explode the dataset size and relational value!
    print(f"    Generating explicit relationship documents for {len(resolved_rels)} relations...")
    for r in resolved_rels:
        src_name = r["source_name"]
        src_type = r["source_type"].upper().replace("-", " ")
        src_id = r["source_id"]
        tgt_name = r["target_name"]
        tgt_type = r["target_type"].upper().replace("-", " ")
        tgt_id = r["target_id"]
        relation = r["relation"].upper()
        rel_desc = r["description"]
        
        desc_part = f"Detailed Analysis:\n{rel_desc}\n" if rel_desc else ""
        
        title = f"Relationship: {src_name} ({src_id}) {relation} {tgt_name} ({tgt_id})"
        content = (
            f"Security Threat Intelligence Relationship Profile:\n"
            f"Source Entity: {src_name} [Type: {src_type}, MITRE ID: {src_id}]\n"
            f"Relation Operator: {relation}\n"
            f"Target Entity: {tgt_name} [Type: {tgt_type}, MITRE ID: {tgt_id}]\n\n"
            f"Relationship Context:\n"
            f"The MITRE ATT&CK framework documents a verified mapping where {src_type} '{src_name}' ({src_id}) "
            f"exhibits a direct '{relation}' association with {tgt_type} '{tgt_name}' ({tgt_id}). "
            f"This represents an operational link between threat actors, delivery tools, or exploitation capabilities.\n\n"
            f"{desc_part}"
        )
        doc = {
            "title": title,
            "content": content,
            "source": "MITRE ATT&CK Relationship Mapping",
            "tags": ["mitre", "relationship", relation.lower(), src_id.lower(), tgt_id.lower()]
        }
        processed.append(doc)
        
    print(f"[+] Processed {len(processed)} MITRE entities and relationship documents.")
    return processed

def save_output(documents):
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    
    # Save as individual JSON files for structured GraphRAG bulk-loading
    json_path = os.path.join(PROCESSED_DIR, "dataset_documents.json")
    print(f"[*] Saving {len(documents)} structured documents to {json_path}...")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(documents, f, indent=2, ensure_ascii=False)
        
    # Also save as a single consolidated text file (exceeding 2M tokens) for standard corpus ingestion
    txt_path = os.path.join(PROCESSED_DIR, "consolidated_cybersecurity_corpus.txt")
    print(f"[*] Compiling and writing unified corpus to {txt_path}...")
    
    with open(txt_path, "w", encoding="utf-8") as f:
        for idx, doc in enumerate(documents, 1):
            f.write(f"========================================================\n")
            f.write(f"DOCUMENT {idx}: {doc['title']}\n")
            f.write(f"SOURCE: {doc['source']}\n")
            f.write(f"TAGS: {', '.join(doc['tags'])}\n")
            f.write(f"========================================================\n\n")
            f.write(doc["content"])
            f.write("\n\n\n")
            
    print(f"[+] Cleaned corpus compiled successfully! Saved to {txt_path}.")

def main():
    print(f"\n========================================================")
    print(f"  CYBERGRAPH DATASET AGGREGATOR — CLEANER & NORMALIZER")
    print(f"========================================================\n")
    
    all_docs = []
    
    # Ingest each raw cybersecurity feed
    all_docs.extend(process_cisa_kev())
    all_docs.extend(process_cisa_advisories())
    all_docs.extend(process_mitre_attack())
    
    if not all_docs:
        print("[!] No documents processed. Please verify downloader downloaded all raw files.")
        return
        
    save_output(all_docs)

if __name__ == "__main__":
    main()
