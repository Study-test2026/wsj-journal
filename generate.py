"""
매일 실행: WSJ 에피소드 → Whisper 전사 → Claude 번역/분석 → docs/today.json 저장
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
        {"text": s["text"].strip(), "start": round(s["start"], 2), "end": round(s["end"], 2)}
        for s in result.get("segments", [])
    ]
    print(f"✅ {len(result['text'])}자 / {len(segments)}개 세그먼트")
    return result["text"], segments


def analyze(episode, transcript, segments):
    print("🤖 Claude 분석 중...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    segs = segments[:60]
    seg_texts = [s["text"] for s in segs]

    tr_res = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4000,
        messages=[{"role":"user","content":f"""아래 영어 문장들을 자연스러운 한국어로 번역하세요.
반드시 순수 JSON 배열만 출력하세요. 마크다운 없이.
형식: [{{"en":"...","ko":"..."}}]
문장들:
{chr(10).join(seg_texts)}"""}]
    )
    raw = re.sub(r"```json|```","",tr_res.content[0].text).strip()
    try:
        translations = json.loads(raw)
    except:
        translations = [{"en":s,"ko":"(번역 실패)"} for s in seg_texts]

    subtitles = []
    for i, seg in enumerate(segs):
        ko = translations[i]["ko"] if i < len(translations) else "(번역 실패)"
        subtitles.append({"en": seg["text"], "ko": ko, "start": seg["start"], "end": seg["end"]})

    an_res = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        messages=[{"role":"user","content":f"""WSJ The Journal 팟캐스트 분석:
제목: {episode['title']}
설명: {episode['description']}
스크립트: {transcript[:3000]}

순수 JSON만 출력하세요:
{{"summary_ko":"5-7문장 한국어 요약","key_points":["포인트1","포인트2","포인트3"],"related_stocks":[{{"ticker":"AAPL","name":"Apple Inc.","reason":"이유","sentiment":"positive/neutral/negative"}}],"market_outlook":"2-3문장 시장 전망 (한국어)"}}"""}]
    )
    raw2 = re.sub(r"```json|```","",an_res.content[0].text).strip()
    try:
        analysis = json.loads(raw2)
    except:
        analysis = {"summary_ko":"분석 실패","key_points":[],"related_stocks":[],"market_outlook":""}

    return subtitles, analysis


def main():
    print("="*50)
    print(f"🗞️  WSJ PWA 데이터 생성 — {datetime.now():%Y-%m-%d %H:%M}")
    print("="*50)

    ep = fetch_episode()
    audio_path = download_audio(ep["audio_url"])

    try:
        transcript, segments = transcribe(audio_path)
        subtitles, analysis = analyze(ep, transcript, segments)
        out = {**ep, "subtitles": subtitles, "analysis": analysis,
               "generated_at": datetime.now().isoformat()}
        OUTPUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"\n✅ 저장 완료: {OUTPUT_PATH}")
    finally:
        Path(audio_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
