"""
큐별 한글 번역 + 에피소드 요약 생성
- Claude로 일관된 어조 유지 (배치)
"""
import os, json, urllib.request
from pathlib import Path

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

SYSTEM_TR = """You translate WSJ podcast cues into natural Korean.
Rules:
- Preserve numbers, tickers ($AAPL), proper nouns.
- Conversational, news-anchor tone (해요체 X, ~다 ~했다 X → 자연스러운 뉴스체).
- Output ONLY JSON array of strings, same length as input."""

SYSTEM_SUM = """Summarize the WSJ Journal episode for Korean listeners.
Return JSON: { summary_en, summary_ko, key_points_ko: [..5..] }"""

def claude(system, user, max_tokens=4000):
    body = json.dumps({
        "model": MODEL, "max_tokens": max_tokens, "system": system,
        "messages": [{"role":"user","content":user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version":"2023-06-01",
            "content-type":"application/json",
        })
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
    t = d["content"][0]["text"].strip()
    if t.startswith("```"):
        t = t.split("```")[1].lstrip("json").strip()
    return json.loads(t)

def translate_cues(cues, batch=40):
    out = [None] * len(cues)
    for i in range(0, len(cues), batch):
        chunk = [c["en"] for c in cues[i:i+batch]]
        ko = claude(SYSTEM_TR, json.dumps(chunk, ensure_ascii=False))
        for j, k in enumerate(ko):
            out[i+j] = k
    return out

def main():
    sync_path = Path("data/episodes/latest/sync.json")
    sync = json.loads(sync_path.read_text())

    # 1) 자막 번역
    kos = translate_cues(sync["cues"])
    for c, k in zip(sync["cues"], kos):
        c["ko"] = k

    # 2) 요약
    full_en = " ".join(c["en"] for c in sync["cues"])[:8000]
    s = claude(SYSTEM_SUM, full_en)
    sync["episode"].update(s)

    sync_path.write_text(json.dumps(sync, ensure_ascii=False, indent=2))
    print(f"✓ translated {len(kos)} cues")

if __name__ == "__main__":
    main()