"""
WSJ Journal PWA - 매일 데이터 생성
- Whisper 세그먼트 타임스탬프 기반 정확한 자막 싱크
- 광고 구간 자동 감지 및 제거
- ETF 포함 관련주 분석
"""

import os, re, json, tempfile, requests, feedparser, anthropic
from pathlib import Path
from datetime import datetime

WSJ_RSS = "https://video-api.wsj.com/podcast/rss/wsj/the-journal"
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OUTPUT_PATH = Path("docs/today.json")
OUTPUT_PATH.parent.mkdir(exist_ok=True)


def fetch_episode():
    print("📡 RSS 피드 가져오는 중...")
    feed = feedparser.parse(WSJ_RSS)
    e = feed.entries[0]
    audio_url = None
    for link in e.get("links", []):
        if "audio" in link.get("type","") or link.get("href","").endswith(".mp3"):
            audio_url = link["href"]; break
    if not audio_url and e.get("enclosures"):
        audio_url = e["enclosures"][0].get("url")
    print(f"✅ {e.title}")
    return {
        "title": e.title,
        "description": e.get("summary", ""),
        "published": e.get("published", ""),
        "audio_url": audio_url,
        "duration": e.get("itunes_duration", ""),
        "link": e.get("link", ""),
    }


def download_audio(url):
    print("⬇️  오디오 다운로드 중...")
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        for chunk in r.iter_content(8192): tmp.write(chunk)
    tmp.close()
    return tmp.name


def transcribe(path):
    print("🎙️  Whisper 음성 인식 중...")
    import whisper
    model = whisper.load_model("base")
    result = model.transcribe(path, language="en")
    segments = [
        {
            "text": s["text"].strip(),
            "start": round(s["start"], 2),
            "end": round(s["end"], 2)
        }
        for s in result.get("segments", [])
        if s["text"].strip()
    ]
    print(f"✅ {len(segments)}개 세그먼트 전사 완료")
    return result["text"], segments


# 광고 판별 키워드
AD_KEYWORDS = [
    "brought to you by", "sponsor", "promo code", "discount", "offer",
    "advertisement", "this episode is sponsored", "use code", "sign up",
    "free trial", "click the link", "download the app", "subscribe to",
    "visit us at", "go to", "dot com slash", ".com/", "percent off",
    "limited time", "exclusive offer", "coupon", "promocode",
    "audible", "squarespace", "betterhelp", "netsuite", "indeed",
    "linkedin", "shopify", "wix", "hubspot", "masterclass",
]

def is_ad_segment(text: str) -> bool:
    """광고 세그먼트 여부 판별"""
    t = text.lower()
    return sum(1 for kw in AD_KEYWORDS if kw in t) >= 2


def remove_ads(segments: list) -> tuple[list, list]:
    """
    광고 구간 감지 및 제거.
    연속으로 광고 판정된 세그먼트 블록을 제거하고,
    제거된 시간 범위를 반환.
    """
    # 각 세그먼트에 광고 여부 마킹
    marked = []
    for seg in segments:
        marked.append({**seg, "_ad": is_ad_segment(seg["text"])})

    # 슬라이딩 윈도우: 전후 문맥으로 광고 블록 확장
    # (광고 세그먼트가 3개 이상 연속이면 블록으로 처리)
    n = len(marked)
    ad_block = [False] * n
    window = 3
    for i in range(n):
        if marked[i]["_ad"]:
            # 앞뒤 window 범위를 광고로 표시
            for j in range(max(0, i-1), min(n, i+window+1)):
                if marked[j]["_ad"]:
                    ad_block[j] = True

    clean = [s for i, s in enumerate(marked) if not ad_block[i]]
    removed = [s for i, s in enumerate(marked) if ad_block[i]]

    print(f"🚫 광고 제거: {len(removed)}개 세그먼트 제거, {len(clean)}개 유지")
    return clean, removed


def translate_segments(segments: list, client) -> list:
    """
    세그먼트를 Claude로 번역 - 인덱스 1:1 매핑 보장
    세그먼트가 많으면 배치로 나눠서 처리
    """
    BATCH = 40  # 한 번에 처리할 세그먼트 수
    all_translated = []

    for batch_start in range(0, len(segments), BATCH):
        batch = segments[batch_start:batch_start+BATCH]
        texts = [s["text"] for s in batch]

        prompt = f"""아래 영어 문장 {len(texts)}개를 자연스러운 한국어로 번역하세요.

규칙:
1. 반드시 입력과 동일한 개수({len(texts)}개)의 항목을 출력하세요
2. 순수 JSON 배열만 출력하세요 (마크다운 없이)
3. 각 항목은 {{"en":"원문","ko":"번역"}} 형식

입력 문장들:
{json.dumps(texts, ensure_ascii=False)}"""

        res = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = re.sub(r"```json|```", "", res.content[0].text).strip()

        try:
            translated = json.loads(raw)
            # 개수가 맞지 않으면 원문으로 채움
            while len(translated) < len(batch):
                i = len(translated)
                translated.append({"en": texts[i], "ko": "(번역 실패)"})
            all_translated.extend(translated[:len(batch)])
        except:
            for text in texts:
                all_translated.append({"en": text, "ko": "(번역 실패)"})

        print(f"✅ 번역 {batch_start+len(batch)}/{len(segments)} 완료")

    return all_translated


def analyze(episode, transcript, client) -> dict:
    """에피소드 요약 + 관련주/ETF 분석"""
    prompt = f"""WSJ The Journal 팟캐스트를 분석하세요.

제목: {episode['title']}
설명: {episode['description']}
스크립트: {transcript[:4000]}

순수 JSON만 출력하세요 (마크다운 없이):
{{
  "summary_ko": "5-7문장 한국어 요약",
  "key_points": ["핵심 포인트 1", "핵심 포인트 2", "핵심 포인트 3"],
  "related_stocks": [
    {{
      "ticker": "AAPL",
      "name": "Apple Inc.",
      "reason": "관련 이유 (한국어)",
      "sentiment": "positive",
      "type": "stock"
    }}
  ],
  "related_etfs": [
    {{
      "ticker": "XLK",
      "name": "Technology Select Sector SPDR",
      "reason": "관련 이유 (한국어)",
      "sentiment": "positive",
      "type": "etf"
    }}
  ],
  "market_outlook": "2-3문장 시장 전망 (한국어)"
}}

관련주는 3-5개, ETF는 2-3개 추천하세요.
sentiment는 positive/neutral/negative 중 하나."""

    res = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = re.sub(r"```json|```", "", res.content[0].text).strip()
    try:
        data = json.loads(raw)
        # related_stocks + related_etfs 합치기
        stocks = data.get("related_stocks", [])
        etfs = data.get("related_etfs", [])
        data["related_stocks"] = stocks + etfs
        return data
    except:
        return {"summary_ko": "분석 실패", "key_points": [], "related_stocks": [], "market_outlook": ""}


def main():
    print("=" * 50)
    print(f"🗞️  WSJ PWA 데이터 생성 — {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 50)

    ep = fetch_episode()
    audio_path = download_audio(ep["audio_url"])

    try:
        # 1. 음성 인식
        transcript, segments = transcribe(audio_path)

        # 2. 광고 제거
        clean_segments, removed = remove_ads(segments)

        # 3. Claude 클라이언트
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        # 4. 번역 (광고 제거된 세그먼트만, 최대 80개)
        segs_to_translate = clean_segments[:80]
        translations = translate_segments(segs_to_translate, client)

        # 5. 자막 생성 (타임스탬프 포함, 1:1 매핑)
        subtitles = []
        for i, seg in enumerate(segs_to_translate):
            ko = translations[i]["ko"] if i < len(translations) else "(번역 실패)"
            subtitles.append({
                "en": seg["text"],
                "ko": ko,
                "start": seg["start"],
                "end": seg["end"]
            })

        # 6. 에피소드 분석
        print("🤖 에피소드 분석 중...")
        # 광고 제거된 텍스트로 분석
        clean_transcript = " ".join(s["text"] for s in clean_segments)
        analysis = analyze(ep, clean_transcript, client)

        # 7. 저장
        out = {
            **ep,
            "subtitles": subtitles,
            "analysis": analysis,
            "ad_segments_removed": len(removed),
            "generated_at": datetime.now().isoformat()
        }
        OUTPUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"\n✅ 저장 완료: {OUTPUT_PATH}")
        print(f"   자막: {len(subtitles)}개 | 광고 제거: {len(removed)}개 세그먼트")

    finally:
        Path(audio_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
