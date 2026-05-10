"""
Claude로 종목/ETF 매수/매도/중립 의견 생성
- 환경변수: ANTHROPIC_API_KEY
- Vercel Python Runtime 또는 GitHub Pages용 Cloudflare Worker로 배포
"""
import os, json
from http.server import BaseHTTPRequestHandler
import urllib.request

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
# 사용자가 'Claude 4.7'로 명시한 경우, 환경변수로 교체 가능

SYSTEM = """You are a sober financial analyst.
Given a US-listed stock/ETF ticker and a short news context from the WSJ podcast,
return a JSON object with:
  - rating: "BUY" | "SELL" | "HOLD"
  - confidence: 0~100
  - opinion: 2-3 sentences English explanation
  - opinion_ko: 2-3 문장 한국어 설명
  - risks_ko: 한 줄 위험 요인
You must always include a clear disclaimer that this is not investment advice.
Use only general public knowledge; do not fabricate prices or numbers.
Return ONLY valid JSON, no markdown."""

def call_claude(symbol: str, ctx_en: str, ctx_ko: str) -> dict:
    user = (
        f"Ticker: {symbol}\n"
        f"WSJ context (EN): {ctx_en}\n"
        f"WSJ context (KO): {ctx_ko}\n"
        f"Provide JSON now."
    )
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 600,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    text = data["content"][0]["text"].strip()
    # 안전 파싱
    if text.startswith("```"):
        text = text.split("```")[1].lstrip("json").strip()
    return json.loads(text)


# Vercel 핸들러
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        try:
            result = call_claude(
                payload.get("symbol", ""),
                payload.get("context_en", ""),
                payload.get("context_ko", ""),
            )
        except Exception as e:
            result = {
                "rating": "HOLD",
                "opinion_ko": f"AI 분석 호출 실패: {e}",
                "opinion": "AI call failed.",
            }
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("access-control-allow-origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result, ensure_ascii=False).encode())