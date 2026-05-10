"""
큐 텍스트에서 등장 종목/ETF 티커 자동 추출
- Claude가 (애플, 테슬라 등) 회사명 → 정확한 티커 매핑
- 결과를 cue.tickers 필드에 저장
"""
import os, json, urllib.request, re
from pathlib import Path

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

SYSTEM = """You extract US stock/ETF tickers from podcast sentences.
Rules:
- Only NYSE/NASDAQ/AMEX listed.
- If a company is mentioned (e.g., "Apple"), return its ticker ("AAPL").
- ETFs allowed (SPY, QQQ).
- Skip foreign-only listings.
- Return JSON array, same length as input, each item: array of tickers (uppercase, no $).
Example input: ["Apple beat earnings", "Bitcoin rallied"]
Example output: [["AAPL"], []]"""

def claude(user):
    body = json.dumps({
        "model": MODEL, "max_tokens": 4000, "system": SYSTEM,
        "messages":[{"role":"user","content":user}],
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

def main():
    p = Path("data/episodes/latest/sync.json")
    sync = json.loads(p.read_text())
    cues = sync["cues"]
    
    # 배치 처리
    BATCH = 30
    for i in range(0, len(cues), BATCH):
        chunk = [c["en"] for c in cues[i:i+BATCH]]
        result = claude(json.dumps(chunk, ensure_ascii=False))
        for j, tickers in enumerate(result):
            cues[i+j]["tickers"] = list(dict.fromkeys(tickers))  # dedupe
    
    p.write_text(json.dumps(sync, ensure_ascii=False, indent=2))
    print(f"✓ tickers extracted")

if __name__ == "__main__":
    main()