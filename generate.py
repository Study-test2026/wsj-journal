"""
WSJ Journal PWA - 매일 데이터 생성
- 날짜별 JSON 저장 (docs/episodes/YYYY-MM-DD.json)
- docs/index_list.json 에 에피소드 목록 유지 (최근 30일)
- 광고 구간 자동 감지 및 제거
- ETF 포함 관련주 분석
"""

import os, re, json, tempfile, requests, feedparser, anthropic
from pathlib import Path
from datetime import datetime, timedelta

WSJ_RSS = "https://video-api.wsj.com/podcast/rss/wsj/the-journal"
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
EPISODES_DIR = Path("docs/episodes")
EPISODES_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH  = Path("docs/index_list.json")
TODAY_PATH  = Path("docs/today.json")  # 최신 에피소드 (앱 기본 로드용)

AD_KEYWORDS = [
    "brought to you by","sponsor","promo code","discount","offer",
    "advertisement","this episode is sponsored","use code","sign up",
    "free trial","click the link","download the app","percent off",
    "limited time","exclusive offer","audible","squarespace","betterhelp",
    "netsuite","indeed","linkedin","shopify","wix","hubspot","masterclass",
    "dot com slash",".com/","coupon","promocode","visit us at",
]

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
        "description": e.get("summary",""),
        "published": e.get("published",""),
        "audio_url": audio_url,
        "duration": e.get("itunes_duration",""),
        "link": e.get("link",""),
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
        {"text": s["text"].strip(), "start": round(s["start"],2), "end": round(s["end"],2)}
        for s in result.get("segments",[]) if s["text"].strip()
    ]
    print(f"✅ {len(segments)}개 세그먼트")
    return result["text"], segments

def is_ad(text):
    t = text.lower()
    return sum(1 for kw in AD_KEYWORDS if kw in t) >= 2

def remove_ads(segments):
    marked = [{**s, "_ad": is_ad(s["text"])} for s in segments]
    n = len(marked)
    ad_block = [False]*n
    for i in range(n):
        if marked[i]["_ad"]:
            for j in range(max(0,i-1), min(n,i+4)):
                if marked[j]["_ad"]: ad_block[j] = True
    clean   = [s for i,s in enumerate(marked) if not ad_block[i]]
    removed = [s for i,s in enumerate(marked) if ad_block[i]]
    print(f"🚫 광고 제거: {len(removed)}개 세그먼트 제거, {len(clean)}개 유지")
    return clean, removed

def translate_segments(segments, client):
    BATCH = 40
    all_translated = []
    for start in range(0, len(segments), BATCH):
        batch = segments[start:start+BATCH]
        texts = [s["text"] for s in batch]
        res = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=8000,
            messages=[{"role":"user","content":
                f"""아래 영어 문장 {len(texts)}개를 자연스러운 한국어로 번역하세요.
반드시 {len(texts)}개 항목을 포함한 순수 JSON 배열만 출력하세요.
형식: [{{"en":"원문","ko":"번역"}}]
입력: {json.dumps(texts, ensure_ascii=False)}"""}]
        )
        raw = re.sub(r"```json|```","",res.content[0].text).strip()
        try:
            tr = json.loads(raw)
            while len(tr) < len(batch):
                tr.append({"en": texts[len(tr)], "ko":"(번역 실패)"})
            all_translated.extend(tr[:len(batch)])
        except:
            all_translated.extend([{"en":t,"ko":"(번역 실패)"} for t in texts])
        print(f"  번역 {start+len(batch)}/{len(segments)}")
    return all_translated

def analyze(episode, transcript, client):
    res = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=2000,
        messages=[{"role":"user","content":
            f"""WSJ The Journal 팟캐스트 분석:
제목: {episode['title']}
설명: {episode['description']}
스크립트: {transcript[:4000]}

순수 JSON만 출력 (마크다운 없이):
{{"summary_ko":"5-7문장 요약","key_points":["포인트1","포인트2","포인트3"],
"related_stocks":[{{"ticker":"AAPL","name":"Apple Inc.","reason":"이유","sentiment":"positive","type":"stock"}}],
"related_etfs":[{{"ticker":"XLK","name":"Tech ETF","reason":"이유","sentiment":"neutral","type":"etf"}}],
"market_outlook":"2-3문장 시장 전망"}}
관련주 3-5개, ETF 2-3개."""}]
    )
    raw = re.sub(r"```json|```","",res.content[0].text).strip()
    try:
        data = json.loads(raw)
        data["related_stocks"] = data.get("related_stocks",[]) + data.get("related_etfs",[])
        return data
    except:
        return {"summary_ko":"분석 실패","key_points":[],"related_stocks":[],"market_outlook":""}

def update_index(date_str, episode, filepath):
    """index_list.json 업데이트 (최근 5개 유지 - 월~금 1주일치)"""
    try:
        index = json.loads(INDEX_PATH.read_text()) if INDEX_PATH.exists() else []
    except:
        index = []

    # 같은 날짜 있으면 교체
    index = [e for e in index if e["date"] != date_str]
    index.insert(0, {
        "date": date_str,
        "title": episode["title"],
        "published": episode["published"],
        "duration": episode["duration"],
        "file": f"episodes/{date_str}.json"
    })

    # 5개 초과분 삭제 (파일도 함께 삭제)
    if len(index) > 5:
        for old in index[5:]:
            old_file = Path("docs") / old["file"]
            old_file.unlink(missing_ok=True)
            print(f"🗑️  삭제: {old['file']}")
    index = index[:5]

    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2))
    print(f"📋 index_list.json: {len(index)}개 에피소드 유지")

def main():
    print("="*50)
    print(f"🗞️  WSJ PWA 데이터 생성 — {datetime.now():%Y-%m-%d %H:%M}")
    print("="*50)

    date_str  = datetime.now().strftime("%Y-%m-%d")
    out_path  = EPISODES_DIR / f"{date_str}.json"

    ep = fetch_episode()
    audio_path = download_audio(ep["audio_url"])

    try:
        transcript, segments = transcribe(audio_path)
        clean_segs, removed  = remove_ads(segments)
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        segs_to_translate = clean_segs  # 전체 세그먼트 번역 (제한 없음)
        translations = translate_segments(segs_to_translate, client)

        subtitles = []
        for i, seg in enumerate(segs_to_translate):
            ko = translations[i]["ko"] if i < len(translations) else "(번역 실패)"
            subtitles.append({"en":seg["text"],"ko":ko,"start":seg["start"],"end":seg["end"]})

        print("🤖 에피소드 분석 중...")
        clean_transcript = " ".join(s["text"] for s in clean_segs)
        analysis = analyze(ep, clean_transcript, client)

        out = {
            **ep,
            "date": date_str,
            "subtitles": subtitles,
            "analysis": analysis,
            "ad_segments_removed": len(removed),
            "generated_at": datetime.now().isoformat()
        }

        # 날짜별 저장
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        # today.json 도 업데이트 (앱 첫 로드용)
        TODAY_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        # 인덱스 업데이트
        update_index(date_str, ep, out_path)

        print(f"\n✅ 저장: {out_path}")
        print(f"   자막: {len(subtitles)}개 | 광고 제거: {len(removed)}개")

    finally:
        Path(audio_path).unlink(missing_ok=True)

if __name__ == "__main__":
    main()
